"""The merge check and the main merge (A11 lane 4: AC-27, AC-83 to AC-85; amendments 13, 14, 22 and 29).

When a git project's last stage is approved and every work package is merged into `ygg/<project>/_integration`
(cancelled packages aside), the conductor's `merge_check` step starts a merge check for the pair (main SHA,
integration SHA) as a background job, outside the journal lock. The job makes a detached temporary worktree of
`main`, stages the merge of the integration head with trusted git (`git merge --no-commit --no-ff`), runs the
optional `merge_check_setup` command from the approved PID, then the home's own pre-commit taken from main's
tree (its repository-level `core.hooksPath` as main has it, else `.git/hooks/pre-commit`), with the profile
timezone and the PATH in `merge_check_path`. This is the one place Yggdrasil runs a repository hook (ruling
132). A pass belongs to its pair. On a pass the project enters `ready-to-merge` and raises `ygg-merge-ready`;
on a failure the PM gets a message with the output's last 40 lines and the user one `ygg-merge-check-failed`.
`ygg merge main` (user-only) merges the integration branch into local `main` after a pass for the current pair;
the project closes once main contains the integration head.

A11.1 (rulings 137 and 138): a project leaves `ready-to-merge` when main or the integration head moves past
the pair that passed, or a later check fails (AC-07), and `ygg-merge-ready` isn't repeated while it is the
project's last merge alert; a merge that clashes with main fails without running the project's checks, owes
the PM nothing and tells the user plainly (AC-08); a lane merges only at its newest accepted head or through
recorded syncs past it, after lane 1's re-check (AC-04, AC-12), and not while its lane has an interrupted
cancel (AC-13).
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from . import config, state, tracker, util
from .util import YggError

GIT_KINDS = ("git-repo", "shared-tree")
TAIL_LINES = 40
RECORD_LOCK = "merge-check"  # taken before the journal lock, never inside it
FAILED_TEXT = "The project's own checks failed before the merge into main. The team is fixing it."
CLASH = "main clashes with the work"
CLASH_NEXT_STEP = ("Ask your main session to combine main with the project's work by hand; the checks then run "
                   "again by themselves.")


# ---------------------------------------------------------------- records

def folder() -> Path:
    return state.root() / "merge-checks"


def record_path(project_id: str) -> Path:
    return folder() / (project_id + ".json")


def record(project_id: str) -> dict:
    data = util.read_json(record_path(project_id), {})
    return data if isinstance(data, dict) else {}


def save_record(project_id: str, data: dict, *, completes_effects: bool = False) -> None:
    """Write the project's check record, bumping its `revision`. Pending failure effects are cleared only by
    their completion (`completes_effects`), never by another check's record or a same-check overwrite."""
    with util.locked(RECORD_LOCK):  # one lock for every ownership check, replacement and completion
        existing = record(project_id)
        if existing.get("effects_pending") and not data.get("effects_pending") and not completes_effects:
            raise util.refuse("the failed merge check " + str(existing.get("id")) + " of " + project_id +
                              " hasn't finished telling the PM and the user yet; the next tick finishes it")
        data["revision"] = int(existing.get("revision") or 0) + 1
        util.ensure_dir(folder())
        util.write_json(record_path(project_id), data)


def save_if_owner(project_id: str, expected_id: str | None, data: dict, *, revision: int | None = None,
                  completes_effects: bool = False) -> bool:
    """A compare-and-set transition under the record lock: write `data` only while the record still belongs to
    check `expected_id` and, when given, is still at `revision`; otherwise skip it and log why."""
    with util.locked(RECORD_LOCK):
        current = record(project_id)
        stale = current.get("id") != expected_id or \
            (revision is not None and int(current.get("revision") or 0) != int(revision))
        if not stale and current.get("effects_pending") and not data.get("effects_pending") and \
                not completes_effects:
            stale = True  # only the completion transition clears pending effects
        if stale:
            util.append_line(folder() / "skipped.jsonl", {"ts": util.iso(), "project": project_id,
                                                         "expected": expected_id, "revision": revision,
                                                         "owner": current.get("id"),
                                                         "owner_revision": current.get("revision"),
                                                         "state": data.get("state")})
            return False
        save_record(project_id, data, completes_effects=completes_effects)
        return True


def result_path(check_id: str) -> Path:
    return folder() / (check_id + ".result.json")


def git(repo, *args):
    return util.trusted_git("-C", repo, *args)


def rev(repo, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}").stdout.strip()


def contains(repo, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer).returncode == 0


# ---------------------------------------------------------------- project facts

def approved_repo_paths(project_id: str) -> list[str]:
    from .commands import approved_pid
    approved = approved_pid(project_id) or {}
    repos = (approved.get("guarded") or {}).get("repos") or []
    return [str(entry["path"]) for entry in repos if isinstance(entry, dict) and entry.get("path")]


def approved_setup(project_id: str):
    """`merge_check_setup` as the user approved it, from the approved PID snapshot only (AC-35): the guarded
    fields `ygg pid approve` stores in pid-approved.json. No config layer, programme, package or PID document
    record counts, so a value nobody approved never runs (fail closed while the snapshot lacks the key)."""
    from .commands import approved_pid
    approved = approved_pid(project_id) or {}
    value = (approved.get("guarded") or {}).get("merge_check_setup")
    if isinstance(value, list) and value and all(isinstance(part, str) and part for part in value):
        return value
    return None


def is_git_project(project: dict) -> bool:
    programme = tracker.programme(project.get("programme") or "") or {}
    return programme.get("kind") in GIT_KINDS


def last_stage_approved(project_id: str) -> bool:
    """Every stage approved: lane 2 stamps `all_approved_at` on the milestones record at a git project's last
    approval and keeps the project executing (amendment 13); a stage reopened since then counts as not approved."""
    data = tracker.milestones(project_id)
    stages = data.get("stages") or []
    return bool(data.get("all_approved_at")) and bool(stages) and \
        all(stage.get("state") == "approved" for stage in stages)


def packages_merged(project_id: str) -> bool:
    from .states import CLOSED_PACKAGE_STATES
    packages = [issue for issue in tracker.issues(project_id) if issue.get("kind") == "work-package"]
    if not any(issue.get("state") == "merged" for issue in packages):
        return False
    return all(issue.get("state", "planned") in CLOSED_PACKAGE_STATES for issue in packages)


def pair(repo: str, project_id: str) -> tuple[str, str]:
    return rev(repo, "refs/heads/main"), rev(repo, "refs/heads/ygg/" + project_id + "/_integration")


def resolve_repo(project_id: str, repo: str | None) -> str:
    """The repo a merge verb acts on: `--repo` when it names the approved PID's repo, else the PID's one repo."""
    paths = approved_repo_paths(project_id)
    if repo is None:
        if len(paths) != 1:
            raise util.refuse("gate: " + project_id + " has no single approved repo; name it with --repo")
        return paths[0]
    from . import quarantine
    quarantine.refuse_box_repo(repo, (tracker.project(project_id) or {}).get("programme"))
    # Match the caller's path to an approved repo by its resolved path first, without running git through it.
    # A relayed call (a Codex agent's) must match that way; only a direct caller falls back to the git-based
    # match, which also accepts a worktree of the approved repo. Either way only the approved path is returned,
    # never the caller's spelling, which can be a link the caller retargets once this check has passed.
    real = os.path.realpath(str(repo))
    for path in paths:
        if os.path.realpath(path) == real:
            return path
    if not util.env("YGG_VIA_RELAY"):
        from .gitops import repo_home
        home = repo_home(str(repo))
        for path in paths:
            if repo_home(path) == home:
                return path
    raise util.refuse("gate: no approved PID of " + project_id + " names " + str(repo))


def passed_for(project_id: str, current: tuple[str, str]) -> dict | None:
    data = record(project_id)
    if data.get("state") == "passed" and (data.get("main"), data.get("integration")) == current:
        return data
    return None


def refuse_repo_count(project_id: str, header: dict) -> None:
    """Amendment 29 (ruling 135): a git-repo or shared-tree PID names exactly one repo in v1.0. Called at
    `ygg pid approve` before anything is written."""
    project = tracker.project(project_id) or {}
    if not is_git_project(project):
        return
    repos = [entry for entry in (header.get("repos") or []) if isinstance(entry, dict)]
    if len(repos) > 1:
        raise util.refuse("projects with more than one repo are not in this release")
    if not repos:
        raise util.refuse("a git project needs one repo")


# ---------------------------------------------------------------- the check itself

def _hook_source(repo: str, main_sha: str) -> tuple[str | None, str]:
    """The home's own pre-commit as main has it: (script text or None, where it came from)."""
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    if not common:
        return None, "no repository"
    configured = subprocess.run(["git", "config", "--file", os.path.join(common, "config"), "--get",
                                 "core.hooksPath"], capture_output=True, text=True, env=util.git_env(), check=False).stdout.strip()
    if configured:
        if configured.startswith("~/"):
            configured = os.path.join(os.path.expanduser("~"), configured[2:])
        if os.path.isabs(configured):
            path = Path(configured) / "pre-commit"
            return (util.read_text(path) if path.is_file() and os.access(path, os.X_OK) else None), str(path)
        name = configured.rstrip("/") + "/pre-commit"
        blob = git(repo, "cat-file", "-p", main_sha + ":" + name)
        return (blob.stdout if blob.returncode == 0 else None), "main:" + name
    path = Path(common) / "hooks" / "pre-commit"
    return (util.read_text(path) if path.is_file() and os.access(path, os.X_OK) else None), str(path)


def _environment(project_id: str) -> dict:
    keep = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SHELL", "TERM")
    environment = {key: os.environ[key] for key in keep if key in os.environ}
    environment["PATH"] = str(config.get("merge_check_path") or os.environ.get("PATH", os.defpath))
    zone = config.get("timezone") or (config.profile() or {}).get("timezone")
    if zone:
        environment["TZ"] = str(zone)
    elif os.environ.get("TZ"):
        environment["TZ"] = os.environ["TZ"]
    return environment


def _tail(text: str) -> str:
    return "\n".join(util.redact(text).splitlines()[-TAIL_LINES:])


def _timeout_seconds() -> float:
    return float(config.get("merge_check_timeout_minutes", 60) or 60) * 60


def _timeout_reason() -> str:
    minutes = int(config.get("merge_check_timeout_minutes", 60) or 60)
    return "timed out after " + str(minutes) + (" minute" if minutes == 1 else " minutes")


def _run(argv: list[str], cwd: str, environment: dict, timeout: float) -> tuple[int | None, str]:
    """Run one step in its own process group; (exit code or None on a timeout, combined output)."""
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   start_new_session=True)
    except OSError as error:
        return 127, str(error)
    try:
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        output, _ = process.communicate()
        return None, output or ""


def run_check(project_id: str, repo: str, main_sha: str, integration_sha: str) -> dict:
    """Run one merge check for the pair and return its result (state `passed` or `failed`, a reason, the
    output tail). Takes no Yggdrasil lock."""
    result = {"main": main_sha, "integration": integration_sha, "finished_at": None}

    def done(outcome: str, reason: str, output: str = "") -> dict:
        result.update(state=outcome, reason=reason, tail=_tail(output), finished_at=util.iso())
        return result

    util.helper_refusal(repo, "gate")
    work = Path(tempfile.mkdtemp(prefix="merge-check-", dir=str(util.ensure_dir(folder()))))
    tree = work / "tree"
    added = git(repo, "worktree", "add", "--detach", str(tree), main_sha)
    if added.returncode != 0:
        return done("failed", "the temporary checkout of main couldn't be made", added.stderr)
    try:
        merged = git(tree, "merge", "--no-commit", "--no-ff", integration_sha)
        if merged.returncode != 0:
            return done("failed", CLASH, merged.stdout + merged.stderr)  # A11.1 AC-08: no project checks run
        # The staged merge the pass is bound to: setup and the hook must leave it exactly so (AC-27, AC-83).
        expected = git(tree, "write-tree").stdout.strip()

        def staged_work_changed() -> bool:
            return git(tree, "write-tree").stdout.strip() != expected or \
                git(tree, "diff", "--quiet").returncode != 0
        script, _ = _hook_source(repo, main_sha)
        environment = _environment(project_id)
        timeout = _timeout_seconds()
        setup = approved_setup(project_id)
        if isinstance(setup, list) and setup:
            code, output = _run([str(part) for part in setup], str(tree), environment, timeout)
            if code is None:
                return done("failed", _timeout_reason(), output)
            if code != 0:
                return done("failed", "setup failed", output)
            if staged_work_changed():
                return done("failed", "the project's checks changed files", output)
        if script is None:
            return done("passed", "no project checks")
        hook = work / "pre-commit"
        util.atomic_write(hook, script, mode=0o700)
        code, output = _run([str(hook)], str(tree), environment, timeout)
        if code is None:
            return done("failed", _timeout_reason(), output)
        if code != 0:
            return done("failed", "the project's checks failed", output)
        if staged_work_changed():
            return done("failed", "the project's checks changed files", output)
        return done("passed", "", output)
    finally:
        git(repo, "worktree", "remove", "--force", str(tree))
        git(repo, "worktree", "prune")
        for path in (work / "pre-commit",):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            work.rmdir()
        except OSError:
            pass


def job_main(argv: list[str]) -> int:
    """The background job: `python -m yggdrasil.mergecheck <project> <repo> <main> <integration> <id>`."""
    project_id, repo, main_sha, integration_sha, check_id = argv[:5]
    if not _named_by_record(project_id, check_id):
        return 0  # v1.1: no record names this job (its start lost the claim): it does nothing, leaves no result
    try:
        outcome = run_check(project_id, repo, main_sha, integration_sha)
    except YggError as error:
        outcome = {"main": main_sha, "integration": integration_sha, "state": "failed",
                   "reason": error.message.replace("REFUSED: ", ""), "tail": "", "finished_at": util.iso()}
    except Exception as error:  # noqa: BLE001 (a crash still ends the check with a recorded failure)
        outcome = {"main": main_sha, "integration": integration_sha, "state": "failed",
                   "reason": "the check crashed", "tail": _tail(repr(error)), "finished_at": util.iso()}
    outcome["id"] = check_id
    util.write_json(result_path(check_id), outcome)
    return 0


JOB_RECORD_SECONDS = 30


def _named_by_record(project_id: str, check_id: str) -> bool:
    """v1.1: a job works only once its record names it (pid and all); a claim that is still `starting` is
    waited for, a bounded time."""
    deadline = time.monotonic() + JOB_RECORD_SECONDS
    while True:
        data = record(project_id)
        if data.get("id") != check_id or data.get("state") not in ("starting", "running"):
            return False
        if data.get("pid") == os.getpid():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)


def _start(project_id: str, repo: str, current: tuple[str, str], seen: dict | None = None) -> dict | None:
    """Start a background check, claiming the record only while it is still what the caller saw (owner and
    revision); otherwise skip (another writer moved it) and log."""
    seen = seen or {}
    check_id = project_id + "-" + uuid.uuid4().hex[:10]
    claim = {"id": check_id, "state": "starting"}
    if not save_if_owner(project_id, seen.get("id"), claim, revision=int(seen.get("revision") or 0)):
        return None
    util.ensure_dir(folder())
    # v1.1: the job starts under the record lock, only while the claim is still this check's, and its pid and
    # start identity are published in the same locked write right after Popen returns, so no job runs without a
    # record naming it (the job itself waits to be named, _named_by_record).
    with util.locked(RECORD_LOCK):
        owner = record(project_id)
        if owner.get("id") != check_id or int(owner.get("revision") or 0) != int(claim.get("revision") or 0):
            return None
        with open(folder() / (check_id + ".log"), "ab") as log:
            process = subprocess.Popen([sys.executable, "-m", "yggdrasil.mergecheck", project_id, repo,
                                        current[0], current[1], check_id], stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=log, start_new_session=True, env=dict(os.environ))
        # The job's start identity, read while it is still this process's unreaped child (its pid can't be
        # reused).
        data = {"id": check_id, "project": project_id, "repo": repo, "main": current[0],
                "integration": current[1], "state": "running", "pid": process.pid,
                "process_started_at": util.process_start(process.pid), "started_at": util.iso()}
        if not save_if_owner(project_id, check_id, data, revision=claim.get("revision")):
            _stop_job(data)  # a newer check took the record while this one started: this job's result is moot
            _tidy(check_id)
    return data


def _stop_job(data: dict) -> None:
    pid = data.get("pid")
    if pid:
        try:
            os.killpg(int(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            pass


def _cleanup_worktrees(repo: str) -> None:
    git(repo, "worktree", "prune")


# ---------------------------------------------------------------- outcomes

def _finish(project: dict, data: dict, outcome: dict) -> None:
    """Record a check's outcome. A failure's effects (the project back to executing, the PM's message, the
    user's alert) are saved as pending with the outcome and completed idempotently, so a crash in between
    loses none of them: the next tick finishes them (A7 I4)."""
    project_id = project["id"]
    data.update(state=outcome.get("state", "failed"), reason=outcome.get("reason", ""),
                finished_at=outcome.get("finished_at") or util.iso())
    data.pop("pid", None)
    data.pop("process_started_at", None)
    if data["state"] != "passed":
        data.update(effects_pending=True, tail=outcome.get("tail") or "")
    owner = save_if_owner(project_id, data.get("id"), data, revision=data.get("revision"))
    _tidy(str(data.get("id")))
    if not owner:
        return  # superseded by a newer check (main or the integration head moved): its result is moot
    if data["state"] != "passed":
        _failure_effects(project, data)


def _failure_effects(project: dict, data: dict) -> None:
    """Complete a failed check's pending effects once: under the merge-check lock, from the record as it is
    now, and keyed by the check id (the PM message carries `(check <id>)`, the alert `check: <id>`), so a
    foreground check and a tick racing here send nothing twice."""
    with util.locked(RECORD_LOCK):
        current = record(project["id"])
        if current.get("id") != data.get("id") or not current.get("effects_pending"):
            return
        _failure_effects_locked(project, current)


def _failure_effects_locked(project: dict, data: dict) -> None:
    project_id = project["id"]
    check_id = str(data.get("id"))
    _back_to_executing(project_id)  # a later check that fails ends ready-to-merge (A11.1 AC-07)
    reason = data.get("reason") or "the project's checks failed"
    raised = any(row.get("kind") == "ygg-merge-check-failed" and row.get("check") == check_id
                 for row in state.alerts())
    if reason == CLASH:
        # A11.1 AC-08: the team can't take main in, so nothing is owed to the PM; the user is told plainly.
        if not raised:
            state.alert("ygg-merge-check-failed", project_id + "'s work clashes with recent changes on main, and "
                        "the team can't settle that in this release.", project=project_id, reason=reason,
                        main=data["main"], integration=data["integration"], check=check_id,
                        next_step=CLASH_NEXT_STEP)
    else:
        marker = "(check " + check_id + ")"
        body = "The merge check of " + project_id + " failed (" + reason + ") for main " + \
            str(data["main"])[:7] + " and the integration head " + str(data["integration"])[:7] + " " + \
            marker + ". Fix it on the lanes, reopen the package with ygg wp reopen, and merge it again; a " \
            "new check then runs by itself."
        tail = data.get("tail") or ""
        if tail.strip():
            body += "\n\nLast lines of the output:\n" + tail
        from . import ratatoskr
        # A stable dispatch id per check: ratatoskr dedupes on the durable inbox journal entry, so a replay after
        # a crash between the journal write and the local inbox sends nothing twice.
        ratatoskr.send("l1", project["pm"], "merge-check-failed", body, internal=True, dispatch=dispatch_id(check_id))
        if not raised:
            state.alert("ygg-merge-check-failed", FAILED_TEXT, project=project_id, reason=reason,
                        main=data["main"], integration=data["integration"], check=check_id)
    data["effects_pending"] = False
    data.pop("tail", None)
    save_if_owner(project_id, data.get("id"), data, completes_effects=True)


def dispatch_id(check_id: str) -> str:
    return "merge-check-" + check_id


def _tidy(check_id: str) -> None:
    """Drop a finished check's result file, and its log when the job wrote nothing to it."""
    result_path(check_id).unlink(missing_ok=True)
    log = folder() / (check_id + ".log")
    try:
        if log.stat().st_size == 0:
            log.unlink()
    except OSError:
        pass


def _enter_ready_to_merge(project_id: str, data: dict) -> None:
    with util.locked():
        tracker.ensure_writable()
        project = tracker.project(project_id) or {}
        if project.get("state") != "executing":
            return
        project["state"] = "ready-to-merge"
        project["ready_to_merge"] = {"main": data["main"], "integration": data["integration"], "at": util.iso()}
        tracker.save_project(project)
    _ensure_ready_alert(project_id, data)


def _ensure_ready_alert(project_id: str, data: dict) -> None:
    """One `ygg-merge-ready` per entry into ready-to-merge, unless it is already the project's last merge
    alert (A11.1 AC-07). Run on every tick while the project is ready-to-merge, so a crash between the
    state write and the alert still raises it."""
    if _last_merge_alert(project_id) == "ygg-merge-ready":
        return
    state.alert("ygg-merge-ready", project_id + " passed its checks and is ready to go into main. "
                "Tell your main session \"merge this\" when you're ready.", project=project_id,
                integration=data["integration"])


def _last_merge_alert(project_id: str) -> str | None:
    last = None
    for row in state.alerts():
        if row.get("project") == project_id and row.get("kind") in ("ygg-merge-ready", "ygg-merge-check-failed"):
            last = row.get("kind")
    return last


def _close(project_id: str) -> None:
    from .gitops import finish_publications
    for path in approved_repo_paths(project_id):
        finish_publications(path)  # this project's interrupted main merge is finished (or refused) first
    with util.locked():
        tracker.ensure_writable()
        project = tracker.project(project_id) or {}
        if project.get("state") not in ("ready-to-merge", "executing"):
            return
        # Read again under the journal lock wp_reopen takes: a package reopened after the merge into main, or a
        # stage no longer approved, keeps the project open, so its work stays schedulable.
        if not packages_merged(project_id) or not last_stage_approved(project_id):
            return
        project["state"] = "closed"
        project["closed_at"] = util.iso()
        project["closed_note"] = "merged into main"
        project["closed_premature"] = False
        project["hold"] = None
        tracker.save_project(project)
    state.alert("ygg-project-closed", project_id + " is finished: its work is in main.", project=project_id)
    from . import t3
    try:
        t3.close_project(project_id)
    except YggError:
        pass


def _back_to_executing(project_id: str) -> None:
    """A ready-to-merge project whose integration head or main moved past its pass waits again."""
    with util.locked():
        tracker.ensure_writable()
        project = tracker.project(project_id) or {}
        if project.get("state") != "ready-to-merge":
            return
        project["state"] = "executing"
        project.pop("ready_to_merge", None)
        tracker.save_project(project)


# ---------------------------------------------------------------- conductor step

def _project_step(project: dict) -> None:
    project_id = project["id"]
    pending = record(project_id)
    if pending.get("state") == "failed" and pending.get("effects_pending"):
        _failure_effects(project, pending)  # finish what a crash cut short
        project = tracker.project(project_id) or project
    if project.get("state") not in ("executing", "ready-to-merge") or not is_git_project(project):
        return
    paths = approved_repo_paths(project_id)
    if len(paths) != 1:
        return
    repo = paths[0]
    if not last_stage_approved(project_id):
        return
    current = pair(repo, project_id)
    if not all(current):
        return
    data = record(project_id)
    seen = dict(data)  # the record this decision is based on: _start claims only while it is unchanged
    merged = packages_merged(project_id)
    if merged and contains(repo, current[1], current[0]) and (project.get("state") == "ready-to-merge" or
                                                              data.get("state") == "passed"):
        _close(project_id)  # rechecks under the lock
        return
    if not merged:
        if data.get("state") == "running":
            _stop_job(data)
            cancelled = dict(data, state="cancelled", finished_at=util.iso())
            cancelled.pop("pid", None)
            cancelled.pop("process_started_at", None)
            save_if_owner(project_id, data.get("id"), cancelled, revision=data.get("revision"))
        if project.get("state") == "ready-to-merge":
            _back_to_executing(project_id)
        return
    same = (data.get("main"), data.get("integration")) == current
    if data.get("state") == "running":
        outcome = util.read_json(result_path(str(data.get("id"))), None)
        if isinstance(outcome, dict) and outcome.get("state"):
            _finish(project, data, outcome)
            data = record(project_id)
            seen = dict(data)
        elif same:
            started = data.get("started_at")
            try:
                age = (util.now() - util.parse_time(str(started))).total_seconds() if started else 0.0
            except ValueError:
                age = 0.0
            alive = util.pid_alive(data.get("pid"))
            if age > _timeout_seconds() or not alive:
                _stop_job(data)
                _cleanup_worktrees(repo)
                outcome = util.read_json(result_path(str(data.get("id"))), None)
                if not (isinstance(outcome, dict) and outcome.get("state")):
                    outcome = {"state": "failed", "reason": _timeout_reason() if alive or age > _timeout_seconds()
                               else "the check stopped without a result", "finished_at": util.iso()}
                _finish(project, data, outcome)
            return
        else:
            _stop_job(data)  # superseded: main or the integration head moved while it ran
            _cleanup_worktrees(repo)
            data = {}
    if (data.get("main"), data.get("integration")) != current:
        if project.get("state") == "ready-to-merge":
            _back_to_executing(project_id)
        _start(project_id, repo, current, seen)
        return
    if data.get("state") == "passed" and project.get("state") == "executing":
        _enter_ready_to_merge(project_id, data)
    elif data.get("state") == "passed" and project.get("state") == "ready-to-merge":
        _ensure_ready_alert(project_id, data)


def tick_step() -> None:
    """The conductor's `merge_check` step (BEFORE_LAUNCH_STEPS). An interrupted main merge is finished first;
    while one can't be, no project closes (the close runs the same recovery and refuses)."""
    from .gitops import finish_publications
    try:
        finish_publications()
    except YggError:
        pass
    for project in tracker.projects():
        try:
            _project_step(project)
        except YggError:
            continue


# ---------------------------------------------------------------- lane merge gate (AC-85 rows 2 and 3)

def _valid_acceptances(issue, seniors) -> list[dict]:
    return [r for r in issue.records if r.get("kind") == "acceptance" and r.get("actor") == "ygg-app"
            and r.get("node") in seniors]


def lane_gate(project_id: str, lane: str, repo: str, head: str) -> None:
    """A lane merges only when every package on it (merged and cancelled ones aside) is accepted, the lane head
    is the newest accepted commit or only recorded sync merges past it (lane 1's `head_holds_only_syncs`), and
    the trusted re-check (lane 1's `recheck`, D5) finds nothing in the packages' commits."""
    from . import lanecheck
    from .commands import paired_seniors
    from .states import CLOSED_PACKAGE_STATES
    _refuse_interrupted_cancel(project_id, lane)
    seniors = paired_seniors(lane, state.all_nodes())
    pending: list[tuple[str, str, str]] = []  # (package, accepted sha, acceptance time)
    for issue in sorted(tracker.issues(project_id), key=lambda i: _package_number(i.id)):
        if issue.get("kind") != "work-package" or issue.get("lane") != lane:
            continue
        if issue.get("state", "planned") in CLOSED_PACKAGE_STATES:
            continue
        valid = _valid_acceptances(issue, seniors)
        if issue.get("state") != "accepted" or not valid or valid[-1].get("verdict") != "accept":
            # Both signed texts: the core row's `no acceptance for <head>` first, then A11 AC-85 row 2's and
            # A11.1 AC-03's `<package> on lane <lane> is not accepted`.
            raise util.refuse("no acceptance for " + head + " on ygg/" + project_id + "/lane/" + lane + ": " +
                              issue.id + " on lane " + lane + " is not accepted")
        pending.append((issue.id, str(valid[-1].get("sha")), str(valid[-1].get("at") or "")))
    if not pending:
        raise util.refuse("no acceptance for " + head + " on ygg/" + project_id + "/lane/" + lane)
    newest = _newest(repo, pending)
    holds = getattr(lanecheck, "head_holds_only_syncs", None)
    fine = holds(project_id, repo, newest, head) if holds is not None else newest == head
    if not fine:
        raise util.refuse("no acceptance for " + head + " on ygg/" + project_id + "/lane/" + lane +
                          ": the lane head isn't the accepted commit")
    check = getattr(lanecheck, "recheck", None)
    if check is not None:
        # One package at a time, as at `ygg wp ready`: each package's commits are checked against the other
        # accepted packages, so a change to a file another accepted package owns is caught (AC-85 row 3).
        for package, _, _ in pending:
            check(project_id, lane, repo, [package], head)
        return
    try:
        problems = lanecheck.range_problems(repo, "refs/heads/main", head)
    except lanecheck.CheckError as error:
        problems = [str(error)]
    if problems:
        raise util.refuse("re-check refused: " + "; ".join(problems))


INTERRUPTED_CANCEL = "a cancel on this lane was interrupted; ask your main session to finish it"


def _refuse_interrupted_cancel(project_id: str, lane: str) -> None:
    """A11.1 AC-13 row 2: a lane with an interrupted cancel doesn't merge until the user finishes the cancel.
    Lane 2's `cancel.check_lane` decides and refuses; before it is merged in, lane 1's `lanedrop.interrupted`."""
    import importlib
    try:
        cancel = importlib.import_module("yggdrasil.cancel")  # cancel.check_lane raises the refusal
    except ImportError:
        cancel = None
    if cancel is not None and hasattr(cancel, "check_lane"):
        cancel.check_lane(project_id, lane)
        return
    try:
        module = importlib.import_module("yggdrasil.lanedrop")
    except ImportError:
        return
    check = getattr(module, "interrupted", None)
    if check is not None and check(project_id, lane):
        raise util.refuse(INTERRUPTED_CANCEL)


def _newest(repo: str, pending: list[tuple[str, str, str]]) -> str:
    """The accepted commit that holds every other acceptance on the lane (ruling 137: earlier acceptances are
    contained in the newest). Refused when none does, since merging would mark a package merged whose work the
    merged head lacks."""
    shas = [sha for _, sha, _ in pending]
    for sha in shas:
        if all(other == sha or contains(repo, other, sha) for other in shas):
            return sha
    newest = max(pending, key=lambda row: row[2])
    missing = [package for package, sha, _ in pending if sha != newest[1] and not contains(repo, sha, newest[1])]
    raise util.refuse("the lane head isn't the accepted commit: " + newest[0] + "'s accepted head doesn't hold "
                      "the accepted work of " + ", ".join(missing))


def _package_number(identifier: str) -> int:
    try:
        return int(str(identifier).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


# ---------------------------------------------------------------- verbs

def status_text(project_id: str, data: dict | None = None) -> str | None:
    """The status line's merge-check part: `merge check: running`, `merge check: passed (<head>)`,
    `merge check: passed (no project checks)` or `merge check: failed (<reason>)`; from `data` when the caller
    already holds the record snapshot it decided on."""
    data = record(project_id) if data is None else data
    kind = data.get("state")
    if kind == "running":
        return "merge check: running"
    if kind == "passed":
        if data.get("reason") == "no project checks":
            return "merge check: passed (no project checks)"
        return "merge check: passed (" + str(data.get("integration"))[:7] + ")"
    if kind == "failed":
        return "merge check: failed (" + str(data.get("reason") or "the project's checks failed") + ")"
    return None


def check_verb(project_id: str, repo: str | None) -> str:
    """`ygg merge check <project> [--repo <repo>]`, run by the user or the PM: run the check now, in the
    foreground, and record it for the current pair."""
    from .commands import _project_for
    from .gitops import _caller_is_pm
    project = _project_for(project_id)
    _caller_is_pm(project)
    path = resolve_repo(project_id, repo)
    current = pair(path, project_id)
    if not all(current):
        raise util.refuse("gate: " + project_id + " has no main or integration branch in " + path)
    from . import rehydrate
    from .gitops import finish_publications
    finish_publications(path)
    rehydrate.replay_journal()  # journal recovery before any failure effects are replayed
    check_id = project_id + "-" + uuid.uuid4().hex[:10]
    data = {"id": check_id, "project": project_id, "repo": path, "main": current[0], "integration": current[1],
            "state": "running", "pid": os.getpid(), "process_started_at": util.process_start(os.getpid()),
            "started_at": util.iso()}
    with util.locked(RECORD_LOCK):
        existing = record(project_id)
        if existing.get("effects_pending"):
            _failure_effects(project, existing)  # a failure's message and alert are never lost to a new check
        if existing.get("state") == "running":
            _stop_job(existing)
        save_record(project_id, data)
    outcome = run_check(project_id, path, current[0], current[1])
    _finish(project, data, outcome)
    snapshot = record(project_id)  # one read decides ownership and formats the answer
    if snapshot.get("id") != check_id:
        return "merge check: superseded by a newer check\n"  # whatever state the newer check is in
    return (status_text(project_id, snapshot) or "merge check: superseded by a newer check") + "\n"


def merge_main(project_id: str, repo: str | None, print_only: bool = False) -> str:
    """`ygg merge main <project> [--repo <repo>] [--print]` (amendment 14): user-only. After a pass for the current
    (main, integration) pair it merges the integration branch into local main in either merge mode; `--print`
    prints the prepare-mode git line instead and leaves main alone. The pair and its pass are checked under the
    journal lock, and the merge goes ahead only while main is still the commit that passed."""
    state.check_user_only("merge main")
    from .commands import _project_for
    from .gitops import _merge_into, finish_publications
    _project_for(project_id)
    path = resolve_repo(project_id, repo)
    finish_publications(path)
    util.helper_refusal(path, "gate")
    integration = "ygg/" + project_id + "/_integration"
    if print_only:
        _passed_pair(project_id, path, repo)
        from .gitops import merge_mode
        # Today's prepare-mode output, unchanged, so the pull-request route reads as before.
        return ("Merge mode " + merge_mode(project_id) + ": main is unchanged. Open a pull request from " +
                integration + " to main, or run:\n  " +
                shlex.join(["git", "-C", str(path), "merge", "--no-ff", integration]) + "\n")
    with util.locked():
        current = _passed_pair(project_id, path, repo)
        new = _merge_into(path, "main", current[1], "Merge " + integration + " into main", no_ff=True,
                          expect=current[0])
    return "merged " + integration + " into local main at " + new + "\n"


def _passed_pair(project_id: str, path: str, repo: str | None) -> tuple[str, str]:
    """The current (main, integration) pair, refused unless a check passed for exactly that pair."""
    integration = "ygg/" + project_id + "/_integration"
    current = pair(path, project_id)
    if not current[1]:
        raise util.usage("integration branch " + integration + " does not exist")
    first = "run the merge check first: ygg merge check " + project_id + " --repo " + str(repo if repo else path)
    data = record(project_id)
    if (data.get("main"), data.get("integration")) == current and data.get("state") == "failed":
        if data.get("reason") == CLASH:
            raise util.refuse("the merge check failed: " + CLASH)
        raise util.refuse("the merge check failed; the PM is fixing it")
    if (data.get("main"), data.get("integration")) == current and data.get("state") == "running":
        raise util.refuse("the merge check is still running; " + first)
    if passed_for(project_id, current) is None:
        raise util.refuse(first)
    return current


if __name__ == "__main__":
    sys.exit(job_main(sys.argv[1:]))
