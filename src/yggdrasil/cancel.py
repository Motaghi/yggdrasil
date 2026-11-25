"""`ygg wp cancel`: a cancel resets the lane to its newest accepted head (A11.1 AC-05, AC-06, AC-11, AC-13, AC-14).

A cancel names one or more packages on one lane, each `authorised`, `rejected`, `stopped`, `review-exhausted` or
`suspended`. Under the launch lock it checks that no activation in the lane's worktree is unfinished, the worktree
is clean, and no other package on the lane is unfinished; it finds the drop point (the newest accepted head on the
lane, else the commit the lane was created from, moved forward along recorded lane syncs already in the integration
branch), refuses when a lane commit it would drop is already in the integration branch or in main, writes its drop
record, resets the lane with trusted git, records the packages `cancelled`, settles their owed items item by item
and writes the completion fact. A drop record with no completion fact is an interrupted cancel: the lane waits for
the user-only `ygg wp cancel <project> --finish <lane-node>` (a preview with a code, then `--yes <code>`).

Lane 1's `lanedrop` owns the lane side: the plan (drop point and candidates, with the already-landed refusals), the
drop record in `<state root>.safety/lanes/<project>.json`, the reset with trusted git and the completion fact.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
from pathlib import Path

from . import state, tracker, util
from .util import YggError

CANCEL_STATES = ("authorised", "rejected", "stopped", "review-exhausted", "suspended")
FINISHED = ("accepted", "merged", "cancelled")
INTERRUPTED = "a cancel on this lane was interrupted; ask your main session to finish it"
OWN_FILES = (".claude/settings.json", ".ygg-managed")


# ---------------------------------------------------------------- records (the lane-drop module's)

def _lanedrop():
    from . import lanedrop
    return lanedrop


def interrupted(project: str | None = None, lane: str | None = None) -> list[dict]:
    """Drop records with no completion fact (lane 1 keeps them in `<state root>.safety/lanes/<project>.json`)."""
    found = []
    projects = [project] if project else [p["id"] for p in tracker.projects()]
    for name in projects:
        try:
            records = _lanedrop().drops(name)
        except (ImportError, YggError):
            continue
        found += [dict(r, project=name) for r in records if not r.get("completed_at")
                  and (lane is None or r.get("lane") == lane)]
    return found


def _home(project: str) -> str:
    record = tracker.project(project) or {}
    home = tracker.programme_home(tracker.programme(record.get("programme")) or {})
    if not home:
        raise util.refuse("gate: project " + project + " has no home repository")
    return home


def _worktree(home: str, project: str, lane: str) -> Path:
    return Path(home) / ".ygg-worktrees" / project / "lane" / lane


# ---------------------------------------------------------------- the lane's activations and worktree

def lane_nodes(lane: str, nodes: dict) -> list[str]:
    return [lane] + sorted(n for n, node in nodes.items() if node.get("role") == "worker" and node.get("parent") == lane)


def _running_refusal(lane: str, nodes: dict) -> str | None:
    from . import lifecycle
    for node_id in lane_nodes(lane, nodes):
        key = lifecycle.open_activation(node_id)
        runtime = state.runtime(node_id)
        held = runtime.get("outcome") == "uncertain" or (key is not None and (
            key in lifecycle.held_activations([node_id]) or lifecycle.open_evidence(node_id, key) == "unknown"))
        if runtime.get("activation") and runtime["activation"] in lifecycle.held_activations([node_id]):
            key, held = key or runtime["activation"], True
        if key is not None and not held:
            # A run whose supervisor is gone can never record its exit: whatever the agent does now, the user stops
            # it and settles the activation with a release (A11.1 AC-11 row 1, third run).
            from . import launcher
            record = launcher.receipt(str(key)) or {}
            supervisor = record.get("supervisor_pid")
            if record.get("exited_at") is None and isinstance(supervisor, int) and not util.pid_alive(supervisor):
                held = True
        if key is None and runtime.get("outcome") == "pending":
            key = runtime.get("activation")
        if key is None:
            continue
        if held:
            return ("the lane still has a running agent (" + node_id + "); stop it, then ygg node release " +
                    node_id + " --as aborted|failed")
        return "the lane still has a running agent (" + node_id + "); stop it first"
    return None


def _dirty(worktree: Path) -> bool:
    if not worktree.is_dir():
        return False
    result = util.trusted_git("-C", str(worktree), "status", "--porcelain", "--untracked-files=all")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    lines = [line for line in lines if line[3:].strip() not in OWN_FILES]
    return bool(lines)


# ---------------------------------------------------------------- the verb

def _crash_point(name: str) -> None:
    """A11.1 AC-13 row 2 (`driver.crash_at`): test mode only, the command kills itself at the named point."""
    if util.test_mode() and os.environ.get("YGG_FAULT_AT") == "boundary:" + name:
        os.kill(os.getpid(), signal.SIGKILL)


def _identity(project: dict, project_name: str) -> str | None:
    """The PM by its node identity, or the user; a managed session without its node identity is never the user."""
    caller = util.current_node()
    if caller is None and state.managed_session():
        raise util.refuse("identity: only the PM of " + project_name + " or the user cancels a package; this managed "
                          "session has no node identity")
    if caller is not None and caller != project["pm"]:
        raise util.refuse("identity: only the PM of " + project_name + " or the user cancels a package")
    return caller


def cancel(project_name: str, packages: list[str], finish: str | None = None, code: str | None = None) -> str:
    util.check_id(project_name, "project")
    project = tracker.project(project_name)
    if project is None:
        raise util.usage("unknown project '" + project_name + "'")
    if finish is not None:
        return finish_cancel(project_name, finish, code)
    _identity(project, project_name)
    if not packages:
        raise util.usage("name at least one --package")
    from . import review
    try:
        review.apply_cap_answers()  # a cap answered `stop` takes effect before the cancel reads the state
    except YggError:
        pass
    for package in packages:
        util.check_record_id(package)
    drop = _lanedrop()
    # Every check runs under the lock, held from the first check through the drop record, the reset, the state writes
    # and the completion fact, so no reservation, ready or other cancel can come in between (A11.1 AC-13).
    with util.locked():
        tracker.ensure_writable()
        nodes = state.all_nodes()
        issues = []
        for package in packages:
            issue = tracker.read_issue(project_name, package)
            if issue is None or issue.get("kind") != "work-package":
                raise util.usage("unknown work package '" + package + "'")
            issues.append(issue)
        lanes = {str(i.get("lane")) for i in issues}
        if len(lanes) > 1:
            first, other = issues[0], next(i for i in issues if i.get("lane") != issues[0].get("lane"))
            raise util.refuse(first.id + " and " + other.id + " are on different lanes; cancel one lane at a time")
        for issue in issues:
            if issue.get("state") not in CANCEL_STATES:
                raise util.refuse(issue.id + " is " + str(issue.get("state")) + "; cancel takes only authorised, "
                                  "rejected, stopped, review-exhausted or suspended packages")
        lane = str(issues[0].get("lane"))
        if interrupted(project_name, lane):
            raise util.refuse(INTERRUPTED)
        home = _home(project_name)
        worktree = _worktree(home, project_name, lane)
        refusal = _running_refusal(lane, nodes)
        if refusal:
            raise util.refuse(refusal)
        if _dirty(worktree):
            raise util.refuse("the lane has uncommitted changes")
        named = set(packages)
        for issue in tracker.issues(project_name):
            if issue.get("kind") == "work-package" and issue.get("lane") == lane and issue.id not in named and \
                    issue.get("state") not in FINISHED and issue.get("state") != "planned":
                raise util.refuse("finish or cancel " + issue.id + " on this lane first")
        plan = drop.plan(project_name, lane, packages, home)  # refuses when a dropped commit already landed
        identifier = drop.record(project_name, plan)
        _crash_point("cancel-after-drop-record")
        drop.reset(home, project_name, lane, plan["drop_point"])
        _finish_state(project_name, packages, lane, nodes)
        drop.complete(project_name, identifier)
    return drop.summary(plan)


def _finish_state(project: str, packages: list[str], lane: str, nodes: dict) -> None:
    for package in packages:
        issue = tracker.read_issue(project, package)
        if issue is None:
            continue
        issue.header["state"] = "cancelled"
        issue.header["owed"] = []  # a record that belongs to this package only is always cleared
        issue.records.append(tracker.record("cancelled", util.current_node(),
                                            actor="user" if util.current_node() is None else "ygg-app",
                                            journal_position=journal_position(project)))
        issue.save()
    settle_messages(project)


def settle_messages(project: str) -> None:
    """A message is settled only when every package it names is cancelled (A11.1 AC-06's item-by-item rule)."""
    from . import journal, workahead
    cancelled = {i.id for i in tracker.issues(project) if i.get("kind") == "work-package"
                 and i.get("state") == "cancelled"}
    if not cancelled:
        return
    for node_id, node in state.all_nodes().items():
        if node.get("project") != project:
            continue
        items = state.inbox(node_id)
        cleared = []
        for item in items:
            if item.get("acked_at"):
                continue
            named = workahead.message_packages(item)
            if named and all(p in cancelled for p in named):
                item["acked_at"] = util.iso()
                cleared.append(item["msg_id"])
        if cleared:
            journal.clear_where(node.get("programme"), lambda e, n=node_id, c=tuple(cleared):
                                e.get("kind") == "inbox" and e.get("node") == n and e.get("msg_id") in c)
            state.save_inbox(node_id, items)


# ---------------------------------------------------------------- the launcher's checks (its slots)

def reserve_check(node: dict) -> str | None:
    """launcher.RESERVE_CHECKS: no reservation in a lane with an interrupted cancel."""
    lane = _lane_of(node)
    if lane and node.get("project") and interrupted(node["project"], lane):
        return INTERRUPTED
    return None


def _lane_of(node: dict) -> str | None:
    if node.get("role") == "tl":
        return node["id"]
    if node.get("role") == "worker":
        parent = state.get_node(node.get("parent")) or {}
        return parent.get("id") if parent.get("role") == "tl" else None
    return None


def start_check(node: dict, activation: str) -> str | None:
    """launcher.START_CHECKS: abort a start when a work-set package was cancelled after this reservation, or a
    cancel on its lane is in progress whose drop record came after it; settle the owed items item by item.

    A team lead's reservation records its work set's packages through the work-ahead screen, which leaves cancelled
    packages out, so a listed package that is cancelled now was cancelled after the reservation. For every other role
    the packages are derived from the reservation itself (a senior user's testing package; a worker's records and
    messages), and a package counts when its cancel record is not older than the reservation (the safe direction).
    No reservation is made while a lane has an interrupted cancel (`reserve_check`), so one found now came after it."""
    project = node.get("project")
    if not project:
        return None
    from . import launcher
    data = launcher.receipt(activation) or {}
    screened = _activation_packages(activation)
    packages = screened or _derived_packages(project, data)
    _record_packages(activation, packages)
    lane = _lane_of(node)
    if lane and interrupted(project, lane):
        return "cancelled: a cancel on this lane is in progress"
    position = _reservation_position(activation)
    reserved = str(data.get("reserved_at") or "")
    gone = []
    for package in packages:
        issue = tracker.read_issue(project, package)
        if issue is None or issue.get("state") != "cancelled":
            continue
        if not screened:
            stamp = next((r for r in reversed(issue.records) if r.get("kind") in ("cancelled", "test-cancelled")), None)
            if stamp is not None and _cancelled_before(stamp, position, reserved):
                continue  # cancelled before this reservation: not this start's concern
        gone.append(package)
    if not gone:
        return None
    for package in gone:
        with util.locked():
            issue = tracker.read_issue(project, package)
            if issue is not None and issue.header.get("owed"):
                issue.header["owed"] = []
                issue.save()
    settle_messages(project)
    return "cancelled: " + ", ".join(gone) + " cancelled after this reservation"


def work_packages(node: dict, given: list[dict], work: dict, testing_package: str | None) -> list[str]:
    """The packages a non-lead reservation's work set is about, for its journal reservation fact: its testing
    package, the packages its record stamps are on, the packages a retried or capacity-held activation was about,
    and the packages its messages name. Packages already cancelled are left out (A11.1 AC-06, AC-13), so a listed
    package found cancelled at the start was cancelled after the reservation."""
    from . import workahead
    project = node.get("project")
    if not project:
        return []
    found = []

    def note(package):
        if package and str(package) not in found:
            found.append(str(package))

    note(testing_package)
    for item in given:
        if item.get("kind") == "record":
            note(item.get("package"))
        elif item.get("kind") in ("retry", "capacity"):
            for package in _activation_packages(str(item.get("id"))):
                note(package)
    messages = set((work or {}).get("messages") or [])
    if messages:
        for item in state.inbox(node["id"]):
            if item.get("msg_id") in messages:
                for package in workahead.message_packages(item):
                    note(package)
    kept = []
    for package in found:
        issue = tracker.read_issue(project, package)
        if issue is not None and issue.get("kind") == "work-package" and issue.get("state") != "cancelled":
            kept.append(package)
    return kept


def journal_position(project: str) -> int | None:
    """Where the journal of the project's programme stands now: the number of its trusted entries. Taken under the
    journal lock with the cancel's record, so a reservation fact whose fold sequence is below it came before the
    cancel, and one at or above it came after (A11.1 AC-13: journal order, not timestamps)."""
    from . import journal
    programme = (tracker.project(project) or {}).get("programme")
    if not programme:
        return None
    with util.locked():
        return len(journal.trusted_entries(programme)[0])


def _reservation_position(activation: str) -> int | None:
    from . import reducer
    for programme_fold in reducer.folds().values():
        record = programme_fold["activations"].get(activation)
        if record is not None and "reserved" in (record.get("phases") or []):
            return int(record.get("sequence") or 0)
    return None


def _cancelled_before(stamp: dict, position: int | None, reserved: str) -> bool:
    """Whether a cancel record came before the reservation: by journal order when both positions are known, else
    (a record written before journal positions) by its timestamp, strictly older, as before."""
    recorded = stamp.get("journal_position")
    if isinstance(recorded, int) and not isinstance(recorded, bool) and position is not None:
        return recorded <= position
    return str(stamp.get("at") or "") < reserved


def _derived_packages(project: str, data: dict) -> list[str]:
    """The packages a non-lead reservation's work set is about: a senior user's testing package, and the packages its
    records (stamps on package records) and messages (their bodies) name."""
    from . import workahead
    found = []

    def note(package):
        if package and package not in found:
            found.append(str(package))

    note(data.get("testing_package") or (data.get("testing") or {}).get("package"))
    given = data.get("consumed") or {}
    stamps = set(given.get("records") or [])
    if stamps:
        for issue in tracker.issues(project):
            if issue.get("kind") == "work-package" and stamps & {s.get("id") for s in issue.header.get("owed") or []
                                                                 if isinstance(s, dict)}:
                note(issue.id)
    messages = set(given.get("messages") or [])
    if messages and data.get("node"):
        for item in state.inbox(str(data["node"])):
            if item.get("msg_id") in messages:
                for package in workahead.message_packages(item):
                    note(package)
    return found


def _record_packages(activation: str, packages: list[str]) -> None:
    """The work set's `packages`, as the reservation fixed them, on the receipt beside the rest of the work set."""
    from . import launcher
    data = launcher.receipt(activation) or {}
    given = dict(data.get("consumed") or {})
    if not data or given.get("packages") == packages:
        return
    given["packages"] = list(packages)
    launcher.update_receipt(activation, consumed=given)


def _activation_packages(activation: str) -> list[str]:
    from . import reducer
    for programme_fold in reducer.folds().values():
        record = programme_fold["activations"].get(activation)
        if record is not None:
            return [p for p in record.get("packages") or [] if isinstance(p, str)]
    return []


def seed(project: str, package: str) -> str:
    """`ygg test cancel <project> <package>` (driver.seed_cancel, test mode only): records the package `cancelled`
    with no check and no reset."""
    if not util.test_mode() or state.managed_session():
        raise util.refuse("test-mode: fixture commands need YGG_TEST_MODE=1 and the user's own session")
    util.check_record_id(package)
    with util.locked():
        issue = tracker.read_issue(project, package)
        if issue is None:
            raise util.usage("unknown work package '" + package + "'")
        issue.header["state"] = "cancelled"
        issue.records.append(tracker.record("test-cancelled", None, actor="user",
                                            journal_position=journal_position(project)))
        issue.save()
    return "cancel seeded on " + package + "\n"


# ---------------------------------------------------------------- the conductor step and status

def step() -> None:
    """One class-user `ygg-cancel-interrupted` alert per drop record with no completion fact."""
    found = interrupted()
    if not found:
        return
    path = state.root() / "cancel-alerts.json"
    with util.locked():
        known = util.read_json(path, []) or []
        fresh = [r for r in found if r.get("id") not in known]
        if not fresh:
            return
        util.write_json(path, known + [r["id"] for r in fresh])
    for record in fresh:
        state.alert("ygg-cancel-interrupted", "A cancel on lane " + record["lane"] + " of " + record["project"] +
                    " was interrupted", node=record["lane"], project=record["project"], cancel=record["id"])


def status_lines(visible_projects: set) -> list[str]:
    lines = ["lane " + r["lane"] + ": an interrupted cancel needs finishing" for r in interrupted()
             if r.get("project") in visible_projects]
    for project in sorted(visible_projects):
        lines += remote_warnings(project)
    return lines


def remote_warnings(project: str) -> list[str]:
    """A11.1 AC-06: while a lane's remote copy still holds a commit a cancel dropped, status says so (v1.0 doesn't
    rewrite a pushed lane, and the pre-push guard keeps refusing the non-fast-forward push the reset needs)."""
    try:
        drops = _lanedrop().drops(project)
    except (ImportError, YggError):
        return []
    dropped: dict = {}
    for record in drops:
        if record.get("lane") and record.get("commits"):
            dropped.setdefault(record["lane"], set()).update(str(c) for c in record["commits"])
    if not dropped:
        return []
    try:
        home = _home(project)
    except YggError:
        return []
    refs = util.trusted_git("-C", home, "for-each-ref", "--format=%(refname)", "refs/remotes").stdout.split()
    lines = []
    for lane in sorted(dropped):
        suffix = "/ygg/" + project + "/lane/" + lane
        held = False
        for ref in refs:
            if not ref.endswith(suffix):
                continue
            for sha in dropped[lane]:
                if util.trusted_git("-C", home, "merge-base", "--is-ancestor", sha, ref).returncode == 0:
                    held = True
                    break
            if held:
                break
        if held:
            lines.append("lane " + lane + " was reset by a cancel; its remote copy still holds dropped commits and "
                         "needs updating by hand")
    return lines


def check_lane(project: str, lane: str) -> None:
    """For `ygg merge lane` (lane 4): refused while the lane has an interrupted cancel (A11.1 AC-13 row 2)."""
    if interrupted(project, lane):
        raise util.refuse(INTERRUPTED)


# ---------------------------------------------------------------- --finish (AC-14)

def _tree_entries(worktree: str, ref: str) -> dict:
    listing = util.trusted_git("-C", worktree, "ls-tree", "-r", "-z", ref).stdout
    found = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        mode, _, rest = meta.partition(" ")
        _, _, blob = rest.partition(" ")
        found[path] = (mode, blob)
    return found


def _index_entries(worktree: str) -> dict:
    listing = util.trusted_git("-C", worktree, "ls-files", "-s", "-z").stdout
    found = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) >= 2:
            found[path] = (parts[0], parts[1])
    return found


def _blob(data: bytes) -> str:
    """Git's blob id of the bytes (the same id the index and trees hold)."""
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def _file_state(root: Path, path: str) -> tuple[str, str] | None:
    """(mode, blob id) of a path, never following a symlink: a symlink (to a file or a folder) is its target text."""
    full = root / path
    if full.is_symlink():
        return "120000", _blob(os.readlink(full).encode())
    if not full.is_file():
        return None
    mode = "100755" if os.access(full, os.X_OK) else "100644"
    try:
        return mode, _blob(full.read_bytes())
    except OSError:
        return mode, "unreadable"


def _walk(root: Path) -> list[str]:
    """Every path the confirmed cleanup could remove: files, symlinks (a symlinked folder is one entry, never
    entered) and everything inside nested repositories, `.git` folders included. Only the worktree's own `.git`
    entry at its root is left out: it is the lane's link to the home repository, which the cleanup keeps."""
    found = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        rel = os.path.relpath(folder, root)
        if rel == ".git" or rel.startswith(".git" + os.sep):
            dirs[:] = []
            continue
        for name in list(dirs):
            if os.path.islink(os.path.join(folder, name)):
                found.append((name if rel == "." else os.path.join(rel, name)).replace(os.sep, "/"))
        if rel == ".":
            dirs[:] = [d for d in dirs if d != ".git" and not os.path.islink(os.path.join(folder, d))]
        else:
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(folder, d))]
        for name in files:
            if name == ".git" and rel == ".":
                continue
            found.append((name if rel == "." else os.path.join(rel, name)).replace(os.sep, "/"))
    return found


def _preview(project: str, record: dict) -> tuple[list[str], str, dict]:
    """The listing of AC-14: commits not in the drop record, then every path where the lane's index or worktree
    differs from the drop point; and an eight-character digest of what it showed."""
    home = _home(project)
    lane = record["lane"]
    worktree = _worktree(home, project, lane)
    plan = _lanedrop().plan(project, lane, record.get("packages") or [], home)  # AC-06's refusals, no code
    point = record.get("drop_point") or plan["drop_point"]
    lines = []
    extra = [c for c in plan["dropped"] if c not in (record.get("commits") or [])]
    if extra:
        lines.append("commits not in the drop record: " + " ".join(c[:7] for c in extra))
    root = str(worktree)
    present = worktree.is_dir()
    target = _tree_entries(root, point) if present else {}
    before = _tree_entries(root, record.get("head") or plan["head"]) if present else {}
    index = _index_entries(root) if present else {}
    files = set(_walk(worktree)) if present else set()
    digest = hashlib.sha256((str(plan["head"]) + json.dumps(record, sort_keys=True, default=str)).encode())
    for path in sorted(set(target) | set(index) | files):
        if path in OWN_FILES:
            continue
        on_disk = _file_state(worktree, path) if present else None
        if index.get(path) == target.get(path) and on_disk == target.get(path):
            continue
        dropped = before.get(path) is not None and index.get(path) == before.get(path) and on_disk == before.get(path)
        lines.append(path + ": " + ("from the dropped commits" if dropped else "not in any recorded commit"))
        digest.update(json.dumps([path, index.get(path), on_disk]).encode())
    return lines, digest.hexdigest()[:8], dict(plan, extra=extra)


def finish_cancel(project: str, lane: str, code: str | None) -> str:
    """`ygg wp cancel <project> --finish <lane-node> [--yes <code>]`: user-only (AC-14)."""
    state.check_user_only("wp cancel --finish")
    util.check_id(lane, "lane")
    found = interrupted(project, lane)
    if not found:
        raise util.refuse("no interrupted cancel on this lane")
    record = found[-1]
    nodes = state.all_nodes()
    drop = _lanedrop()
    with util.locked():
        refusal = _running_refusal(lane, nodes)
        if refusal:
            raise util.refuse(refusal)
        lines, digest, plan = _preview(project, record)
        if code is None:
            raise _preview_refusal(lines, digest)
        if code != digest:
            raise util.refuse("the lane changed since the preview; run --finish again")
        home = _home(project)
        worktree = _worktree(home, project, lane)
        if plan["extra"]:
            drop.add_commits(project, record["id"], plan["extra"])
        if worktree.is_dir():
            # A directory where the drop point has a file goes first, so the reset can write the file back.
            util.trusted_git("-C", str(worktree), "clean", "-ffdxq", *[x for name in OWN_FILES for x in ("-e", "/" + name)])
        drop.reset(home, project, lane, record["drop_point"], clean=True)
        _finish_state(project, record["packages"], lane, nodes)
        drop.complete(project, record["id"])
    return ", ".join(record["packages"]) + " cancelled; lane " + lane + " reset to " + \
        str(record["drop_point"])[:7] + "\n"


class PreviewRefusal(YggError):
    """The preview: exit 3 with the listing on stdout."""


def _preview_refusal(lines: list[str], digest: str) -> YggError:
    error = PreviewRefusal(util.EXIT_REFUSED, "the cancel isn't finished; check the listing, then run --yes <code>")
    error.stdout = "\n".join(lines + ["finish code: " + digest]) + "\n"
    return error
