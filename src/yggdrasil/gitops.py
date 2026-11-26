"""Pre-push guard and lane and main merges (Code, compute and git)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from . import state, tracker, util
from .util import YggError

ZERO = "0" * 40
BRANCH_RE = re.compile(r"^refs/heads/ygg/([a-z0-9][a-z0-9-]{0,62})/(lane/[a-z0-9][a-z0-9-]{0,62}|_integration)$")


GIT_SELECTION = util.GIT_SELECTION
git_env = util.git_env


def git(repo, *args, home_objects: str | None = None):
    return util.trusted_git("-C", repo, *args, home_objects=home_objects)


def remote_urls(repo) -> dict[str, list[str]]:
    """Git's effective fetch and push URLs of every remote of `repo` (insteadOf and pushInsteadOf applied),
    with local paths made absolute against `repo`, by remote name."""
    found: dict[str, list[str]] = {}
    for name in git(repo, "remote").stdout.split():
        urls = []
        for extra in ((), ("--push",)):
            listing = git(repo, "remote", "get-url", *extra, "--all", name)
            urls.extend(line.strip() for line in listing.stdout.splitlines() if line.strip())
        canonical = [normal_url(url, base=str(repo)) for url in urls]
        found[name] = sorted({url for url in canonical if url})
    return found


def _scp_like(text: str) -> bool:
    head = text.split(":", 1)[0]
    return ":" in text and "/" not in head and len(head) > 1


def normal_url(url: str, base: str | None = None) -> str | None:
    """A URL in the form the guard compares: network URLs as given without a trailing slash, local paths as
    absolute realpaths. A relative local path resolves against `base`; without one it has no fixed meaning,
    so the result is None."""
    text = str(url).strip()
    if text.startswith("file://"):
        text = text[len("file://"):]
    elif "://" in text or _scp_like(text):
        return text.rstrip("/")
    text = os.path.expanduser(text)
    if not os.path.isabs(text):
        if base is None:
            return None
        text = os.path.join(os.path.realpath(base), text)
    return os.path.realpath(text)


def is_ancestor(repo, older, newer) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer).returncode == 0


def approved_repos(project_id: str) -> list[dict] | None:
    """Repos as the user approved them (never the live, PM-editable PID header)."""
    project = tracker.project(project_id)
    if not project or project.get("state") not in ("executing", "awaiting-approval", "exception"):
        return None
    from .commands import approved_pid
    approved = approved_pid(project_id)
    if approved is None:
        return None
    return list((approved.get("guarded") or {}).get("repos") or [])


def _recorded_for(project_id: str, repo: str, field: str):
    """The value the PID approval recorded under `field` for `repo`, matched by repository identity."""
    from .commands import approved_pid
    approved = approved_pid(project_id) or {}
    recorded = approved.get(field) or {}
    if os.path.realpath(repo) in recorded:
        return recorded[os.path.realpath(repo)]
    # A push from a worktree names the worktree path: match by repository identity (common directory).
    home = repo_home(repo)
    for path, value in recorded.items():
        if repo_home(str(path)) == home:
            return value
    return None


def trusted_base(project_id: str, repo: str) -> str | None:
    return _recorded_for(project_id, repo, "main_at_approval")


def check_url(project_id: str, repo: str, url: str | None) -> str | None:
    """The destination must be a remote URL the repository had when the user approved the PID."""
    approved = _recorded_for(project_id, repo, "remote_at_approval") or {}
    urls = {u for values in approved.values() for u in values if u}
    if not urls:
        if url is not None:
            return "no approved remote recorded for " + repo_home(repo) + "; pushes need a remote that " \
                "existed when the PID was approved"
        return None
    if url is None:
        return "destination URL missing: the pre-push hook must pass git's remote URL"
    destination = normal_url(url)
    if destination is None:
        return "destination URL " + str(url) + " is relative, so it has no fixed location; use an absolute remote"
    if destination not in urls:
        return "destination URL " + str(url) + " is not the approved remote of " + repo_home(repo)
    return None


def caller_branches(node_id: str, project_id: str) -> list[str] | None:
    """Branches a managed node may push in `project_id`; None when it may not push at all."""
    nodes = state.all_nodes()
    node = nodes.get(node_id)
    if not node or node.get("project") != project_id:
        return []
    role = node.get("role")
    if role == "tl":
        return ["ygg/" + project_id + "/lane/" + node_id]
    if role == "worker":
        parent = nodes.get(node.get("parent") or "") or {}
        if parent.get("role") == "tl":
            return ["ygg/" + project_id + "/lane/" + parent["id"]]
        if parent.get("role") == "pm":
            return ["ygg/" + project_id + "/_integration"]
        return []
    if role == "pm":
        return ["ygg/" + project_id + "/_integration"]
    return None


def check_caller(project_id: str, remote_ref: str) -> str | None:
    node_id = util.current_node()
    if node_id is None:
        return None
    allowed = caller_branches(node_id, project_id)
    if allowed is None:
        return node_id + " may not push"
    branch = remote_ref[len("refs/heads/"):]
    if branch not in allowed:
        if not allowed:
            return node_id + " may not push to project " + project_id
        return node_id + " may push only " + " or ".join(allowed)
    return None


def repo_home(path: str) -> str:
    """Realpath of the home repository that owns `path` (a worktree resolves to its home). A registered lane
    clone that this hook process mapped resolves to its registered home, through the registry and never through
    the clone's own git (v1.1.1)."""
    from . import lanecheck
    mapped = lanecheck.clone_home(path)
    if mapped:
        return os.path.realpath(mapped)
    result = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common = result.stdout.strip()
    if result.returncode == 0 and common:
        common = os.path.realpath(common)
        if os.path.basename(common) == ".git":
            return os.path.dirname(common)
        return common
    return os.path.realpath(path)


def _approved_entry_by_path(project_id: str, repo_path: str):
    """The approved repo entry whose resolved path equals the caller's resolved path, found without git."""
    repos = approved_repos(project_id)
    if repos is None:
        return None
    real = os.path.realpath(str(repo_path))
    for entry in repos:
        if entry.get("path") and os.path.realpath(str(entry["path"])) == real:
            return entry
    return None


def _repo_entry(project_id: str, repo_path: str):
    repos = approved_repos(project_id)
    if repos is None:
        return None
    real = repo_home(repo_path)
    for entry in repos:
        if entry.get("path") and repo_home(str(entry["path"])) == real:
            return entry
    return None


def check_ref(repo: str, local_ref: str, local_sha: str, remote_ref: str, remote_sha: str,
              url: str | None = None) -> str | None:
    if local_ref == "(delete)" or local_sha == ZERO:
        return "deletion of " + remote_ref + " is denied"
    if remote_ref.startswith("refs/tags/"):
        return "tag push " + remote_ref + " is denied"
    match = BRANCH_RE.match(remote_ref)
    if not match:
        return "destination " + remote_ref + " is outside ygg/<project>/ branches"
    project_id = match[1]
    entry = _repo_entry(project_id, repo)
    if entry is None:
        return "destination " + remote_ref + " belongs to project " + project_id + \
            ", which has no approved PID naming this repo"
    problem = check_url(project_id, repo, url) or check_caller(project_id, remote_ref)
    if problem:
        return problem
    from . import lanecheck
    objects = lanecheck._objects(repo)  # a registered clone's hook reads the home's objects here (v1.1.1)
    if remote_sha != ZERO:
        if git(repo, "cat-file", "-e", remote_sha + "^{commit}", home_objects=objects).returncode != 0 or \
                git(repo, "merge-base", "--is-ancestor", remote_sha, local_sha,
                    home_objects=objects).returncode != 0:
            return "non-fast-forward push to " + remote_ref + " is denied"
    if remote_sha != ZERO:
        base = remote_sha
    else:
        trusted = trusted_base(project_id, repo)
        base = git(repo, "merge-base", local_sha, trusted, home_objects=objects).stdout.strip() if trusted else ""
    if not entry.get("workflow_changes"):
        if not base:
            return "workflow-change: no merge base with the main recorded at PID approval for " + remote_ref
        # Every transmitted commit counts, so a workflow change added and then reverted is still caught.
        changed = git(repo, "log", "--format=", "--name-only", "--no-renames", "-m", base + ".." + local_sha,
                      "--", ".github/workflows/", home_objects=objects)
        if changed.returncode != 0 or changed.stdout.strip():
            return "workflow-change: the pushed range to " + remote_ref + " touches .github/workflows/"
    if not base:
        return "lane check: no merge base with the main recorded at PID approval for " + remote_ref
    # The lane check again, on every pushed commit: a commit made with its hooks skipped can't reach the remote.
    try:
        problems = lanecheck.range_problems(repo, base, local_sha, allow_ci=bool(entry.get("workflow_changes")),
                                            project=project_id)
    except lanecheck.CheckError as error:
        return "lane check: " + str(error)
    if problems:
        return "lane check: " + "; ".join(problems)
    return None


def push_guard(repo: str, remote: str, stdin_text: str, url: str | None = None) -> tuple[int, str]:
    problems = []
    lines = [line for line in stdin_text.splitlines() if line.strip()]
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            problems.append("malformed pre-push line: " + line)
            continue
        problem = check_ref(repo, *parts, url=url)
        if problem:
            problems.append(problem)
    if problems:
        return util.EXIT_GUARD, "DENIED: " + "; ".join(problems)
    return 0, ""


def checked_out_at(repo, branch: str) -> str | None:
    """The worktree path where refs/heads/<branch> is checked out, if any."""
    listing = git(repo, "worktree", "list", "--porcelain")
    path = None
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == "branch refs/heads/" + branch and path:
            return path
    return None


def checkout_of(repo, branch: str) -> str | None:
    """Like checked_out_at, but only a successful, fully parsed `git worktree list --porcelain` counts: every
    record must have a `worktree` line and exactly one of `branch`, `detached` or `bare`. A failure or an
    incomplete record raises (a refusal), so "checked out nowhere" is only ever a positive answer."""
    listing = git(repo, "worktree", "list", "--porcelain")
    if listing.returncode != 0 or not listing.stdout.strip():
        raise util.refuse("git couldn't list the worktrees of " + str(repo) + ": " +
                          (listing.stderr or "no output").strip())
    unreadable = util.refuse("git's worktree list for " + str(repo) + " can't be read")
    found = None
    for block in listing.stdout.strip("\n").split("\n\n"):
        lines = block.split("\n")
        worktrees = [line for line in lines if line.startswith("worktree ")]
        heads = [line for line in lines if line.startswith("HEAD ")]
        kinds = [line for line in lines if line.startswith("branch ") or line in ("detached", "bare")]
        notes = [line for line in lines if line in ("locked", "prunable") or line.startswith(("locked ", "prunable "))]
        if len(worktrees) != 1 or not lines[0].startswith("worktree ") or not worktrees[0][len("worktree "):] or \
                len(heads) > 1 or len(kinds) != 1 or \
                len(worktrees) + len(heads) + len(kinds) + len(notes) != len(lines):
            raise unreadable  # exactly one worktree, at most one HEAD, one kind; nothing else but annotations
        if kinds[0] == "branch refs/heads/" + branch:
            if found is not None:
                raise unreadable
            found = worktrees[0][len("worktree "):]
    return found


def _clean(worktree: str) -> bool:
    status = git(worktree, "status", "--porcelain", "--untracked-files=no")
    return status.returncode == 0 and not status.stdout.strip()


def _merge_into(repo, target_branch: str, source_sha: str, message: str, *, no_ff: bool = False,
                expect: str | None = None) -> str:
    """Merge with a real `git merge` in a worktree, never moving a checked-out branch with update-ref. With
    `expect`, the merge goes ahead only while the target branch is still that commit."""
    target = git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + target_branch).stdout.strip()
    if expect is not None:
        return _merge_from_expected(repo, target_branch, expect, source_sha, message)
    if not target:
        git(repo, "branch", target_branch, source_sha)
        return source_sha
    if is_ancestor(repo, source_sha, target):
        return target
    args = ["merge", "--no-edit", "-m", message] + (["--no-ff"] if no_ff else []) + [source_sha]
    location = checked_out_at(repo, target_branch)
    if location:
        util.helper_refusal(location, "gate")
    if location:
        if not _clean(location):
            raise util.refuse("gate: " + target_branch + " is checked out at " + location +
                              " with uncommitted changes; commit or stash them before merging")
        result = git(location, *args)
        if result.returncode != 0:
            git(location, "merge", "--abort")
            raise util.refuse("merge into " + target_branch + " failed and was aborted: " +
                              (result.stderr or result.stdout).strip())
    else:
        import tempfile
        temporary = tempfile.mkdtemp(prefix="merge-", dir=str(state.root()))
        os.rmdir(temporary)
        added = git(repo, "worktree", "add", "--no-checkout", temporary, target_branch)
        if added.returncode != 0:
            raise util.refuse("cannot create a temporary worktree for " + target_branch + ": " +
                              added.stderr.strip())
        try:
            # Check the config git will load in the new worktree before anything is checked out there.
            util.helper_refusal(temporary, "gate")
            filled = git(temporary, "checkout", "-q", "-f")
            if filled.returncode != 0:
                raise util.refuse("cannot check out the temporary worktree for " + target_branch + ": " +
                                  filled.stderr.strip())
            result = git(temporary, *args)
            if result.returncode != 0:
                git(temporary, "merge", "--abort")
                raise util.refuse("merge into " + target_branch + " failed and was aborted: " +
                                  (result.stderr or result.stdout).strip())
        finally:
            git(repo, "worktree", "remove", "--force", temporary)
            git(repo, "worktree", "prune")
    return git(repo, "rev-parse", "refs/heads/" + target_branch).stdout.strip()


MOVED = " moved since its merge check passed; the check runs again first"


def _merge_from_expected(repo, target_branch: str, expect: str, source_sha: str, message: str) -> str:
    """The merge for `ygg merge main`: the merge commit is made in a detached temporary worktree from exactly
    `expect`, then the branch moves from `expect` to it with a compare-and-swap, refused if anything moved it
    meanwhile. A checked-out branch's worktree is then brought from `expect` to the merge (`read-tree -m -u`),
    and a failure there puts the branch back."""
    import tempfile
    ref = "refs/heads/" + target_branch
    if git(repo, "rev-parse", "--verify", "--quiet", ref).stdout.strip() != expect:
        raise util.refuse(target_branch + MOVED)
    if is_ancestor(repo, source_sha, expect):
        return expect
    location = checkout_of(repo, target_branch)
    if location:
        util.helper_refusal(location, "gate")
        if not _clean(location):
            raise util.refuse("gate: " + target_branch + " is checked out at " + location +
                              " with uncommitted changes; commit or stash them before merging")
    temporary = tempfile.mkdtemp(prefix="merge-", dir=str(state.root()))
    os.rmdir(temporary)
    added = git(repo, "worktree", "add", "--no-checkout", "--detach", temporary, expect)
    if added.returncode != 0:
        raise util.refuse("cannot create a temporary worktree for " + target_branch + ": " + added.stderr.strip())
    try:
        util.helper_refusal(temporary, "gate")
        filled = git(temporary, "checkout", "-q", "-f")
        if filled.returncode != 0:
            raise util.refuse("cannot check out the temporary worktree for " + target_branch + ": " +
                              filled.stderr.strip())
        result = git(temporary, "merge", "--no-edit", "--no-ff", "-m", message, source_sha)
        if result.returncode != 0:
            git(temporary, "merge", "--abort")
            raise util.refuse("merge into " + target_branch + " failed and was aborted: " +
                              (result.stderr or result.stdout).strip())
        merged = git(temporary, "rev-parse", "HEAD").stdout.strip()
    finally:
        git(repo, "worktree", "remove", "--force", temporary)
        git(repo, "worktree", "prune")
    # A durable publication intent first, so a crash between moving the branch and updating its checkout is
    # finished (or refused) by the next merge command, merge check or tick, never left with the merge's reverse
    # staged in the checkout.
    with util.locked():
        # Where the branch is checked out is found again here, just before the swap: a checkout made since the
        # check above is the one that has to follow the merge.
        location = checkout_of(repo, target_branch)
        if location:
            util.helper_refusal(location, "gate")
            if not _clean(location):
                raise util.refuse("gate: " + target_branch + " is checked out at " + location +
                                  " with uncommitted changes; commit or stash them before merging")
        intent = {"repo": str(repo), "branch": target_branch, "expect": expect, "merged": merged,
                  "location": location, "at": util.iso()}
        intent_path = _intent_path(repo, target_branch)  # once, before publication; reused below
        util.write_json(intent_path, intent)
        swapped = git(repo, "update-ref", "-m", message, ref, merged, expect)
        if swapped.returncode != 0:
            # The intent goes only on positive evidence that this merge was not published: main read back, and it
            # neither is nor holds the merge. Otherwise it stays for recovery.
            now = git(repo, "rev-parse", "--verify", "--quiet", ref)
            if now.returncode == 0 and now.stdout.strip() and now.stdout.strip() != merged and \
                    git(repo, "merge-base", "--is-ancestor", merged, now.stdout.strip()).returncode == 1:
                intent_path.unlink(missing_ok=True)
            raise util.refuse(target_branch + MOVED)
        try:
            try:
                _finish_publication(intent)
            except (util.YggError, _KeepIntent):
                raise
            except Exception as error:  # an OS or subprocess failure: kept, never an escaping exception
                raise _KeepIntent(type(error).__name__ + ": " + str(error)) from error
        except _KeepIntent as error:
            _alert_once(intent_path, "ygg-merge-intent-held", "A merge publication intent is kept for now: " +
                        str(error))  # never raises, so the refusal below always happens
            raise util.refuse(target_branch + " was merged, but its checkout couldn't be checked (" + str(error) +
                              "); the next command tries again") from error
    return merged


def _intents_dir():
    return util.ensure_dir(state.root() / "merge-publish")


def _intent_path(repo, branch: str):
    """The intent file for one repo and branch, keyed by the repo's real path (no git call, so a name can be
    checked before anything in the file is trusted)."""
    import hashlib
    key = hashlib.sha256((os.path.realpath(str(repo)) + "\0" + branch).encode()).hexdigest()[:16]
    return _intents_dir() / (key + ".json")


FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
MAIN = "main"


def _approved_repo_paths() -> set[str]:
    """Real paths of every repo an approved PID names, across all known projects. Every approval is read strictly:
    an approval file that exists but can't be read or decoded raises, so a partial list never counts (the caller
    keeps the intent). Only an absent file or one not by an authority actor is positively "not approved"."""
    from .commands import authority_actors
    found = set()
    actors = authority_actors()
    for project in tracker.projects():
        approval = tracker.project_dir(project["id"]) / "pid-approved.json"
        try:
            text = approval.read_text()
        except FileNotFoundError:
            continue  # the one positive answer for "not approved"; any other OSError propagates
        data = json.loads(text)  # UnicodeDecodeError and ValueError propagate
        if not isinstance(data, dict):
            raise TypeError("the approval of " + str(project["id"]) + " isn't a JSON object")
        if data.get("actor") not in actors:
            continue
        for entry in (data.get("guarded") or {}).get("repos") or []:
            if isinstance(entry, dict) and entry.get("path"):
                found.add(os.path.realpath(str(entry["path"])))
    return found


class _KeepIntent(Exception):
    """The environment, not the intent, failed (tracker, git, filesystem): keep the intent, skip it this run and
    refuse only for its repo."""


def _shape_problem(path, intent) -> str | None:
    """Parse and shape errors, from the file alone (no git, no tracker): a reason to reject, or None."""
    if not isinstance(intent, dict):
        return "not a JSON object"
    repo, branch, location = intent.get("repo"), intent.get("branch"), intent.get("location")
    if branch != MAIN:
        return "the branch isn't main"
    for key in ("expect", "merged"):
        if not isinstance(intent.get(key), str) or not FULL_SHA.match(intent[key]):
            return key + " isn't a full commit id"
    if not _usable_path(repo):
        return "the repo isn't a path"
    if location is not None and not _usable_path(location):
        return "the checkout path isn't a path"
    if os.path.realpath(str(path)) != os.path.realpath(str(_intent_path(repo, branch))):
        return "the file name doesn't match its repo and branch"
    return None


def _usable_path(value) -> bool:
    """A non-empty path string the OS can take: no NUL, encodable."""
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    try:
        os.fsencode(value)
        os.path.realpath(value)
    except (ValueError, UnicodeError, OSError):
        return False
    return True


def _intent_problem(path, intent) -> str | None:
    """Why an intent can't be trusted (reject), or None. Shape first; then, with the environment, whether its
    repo is approved and where main is. Environment failures raise _KeepIntent, never a rejection. Main at
    neither commit with the merge an ancestor of main is a publication that landed (not a rejection);
    main at neither with the merge not an ancestor never landed (moot or forged): reject."""
    problem = _shape_problem(path, intent)
    if problem:
        return problem
    repo, branch = intent["repo"], intent["branch"]
    try:
        approved = _approved_repo_paths()
    except Exception as error:  # the tracker failed, not the intent
        raise _KeepIntent("the approved repos can't be read (" + type(error).__name__ + ")") from error
    if not approved:
        raise _KeepIntent("no approved repo can be read from the tracker")  # an absent tracker isn't evidence
    if os.path.realpath(repo) not in approved:
        return "the repo isn't an approved repo of a known project"
    found = git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch)
    current = found.stdout.strip()
    if found.returncode != 0 or not current:
        raise _KeepIntent("git couldn't read " + branch + " in " + repo)
    if current in (intent["expect"], intent["merged"]):
        return None
    exists = git(repo, "cat-file", "-e", intent["merged"])
    if exists.returncode == 1 and not exists.stderr.strip():
        return "the merge commit doesn't exist in " + repo  # the one positive answer for a missing object
    if exists.returncode != 0:
        raise _KeepIntent("git couldn't look up the merge commit in " + repo)
    kind = git(repo, "cat-file", "-t", intent["merged"])
    if kind.returncode != 0 or not kind.stdout.strip():
        raise _KeepIntent("git couldn't read the merge commit's type in " + repo)
    if kind.stdout.strip() != "commit":
        return "the merge id names a " + kind.stdout.strip() + ", not a commit"
    ancestor = git(repo, "merge-base", "--is-ancestor", intent["merged"], current)
    if ancestor.returncode == 1:
        return branch + " is at neither the commit before the merge nor the merge, and doesn't hold the merge"
    if ancestor.returncode != 0:
        raise _KeepIntent("git couldn't compare " + branch + " with the merge in " + repo)
    return None  # landed, then main moved on: _finish_publication compares the checkout with main now


def _git_out(where, what: str, *args) -> str:
    """A git call whose output recovery needs: a nonzero exit or empty output keeps the intent (_KeepIntent)."""
    result = git(where, *args)
    if result.returncode != 0 or not result.stdout.strip():
        raise _KeepIntent("git " + what + " in " + str(where))
    return result.stdout.strip()


def _alert_once(path, kind: str, message: str) -> None:
    """One log-class alert per intent file and kind. Never raises: an alert that can't be written never wedges
    recovery."""
    try:
        if any(row.get("kind") == kind and row.get("path") == str(path) for row in state.alerts()):
            return
        state.alert(kind, message, path=str(path))
    except Exception:  # noqa: BLE001, S110 (the held intent and the refusal still stand)
        pass


def _reject_intent(path, reason: str) -> None:
    target = util.ensure_dir(_intents_dir() / "rejected") / path.name
    os.replace(path, target)
    try:
        state.alert("ygg-merge-intent-rejected", "A merge publication intent was set aside, not acted on: " +
                    str(target) + " (" + reason + ")", path=str(target), reason=reason)
    except Exception:  # noqa: BLE001, S110 (the file is already set aside)
        pass


def _finish_publication(intent: dict) -> None:
    """Bring an interrupted publication to its end, or refuse. Main at `expect`: nothing was published. Main at
    `merged` with the checkout's index and files still at `expect`: update them (read-tree -m -u). The checkout
    already at `merged`: done. Anything else is refused and the intent kept, so the project doesn't close over a
    checkout that could undo the merge."""
    repo, branch = intent["repo"], intent["branch"]
    expect, merged = intent["expect"], intent["merged"]
    path = _intent_path(repo, branch)
    try:
        location = checkout_of(repo, branch)  # never the stored one: git keeps a branch in one worktree at most
    except util.YggError as error:
        raise _KeepIntent(error.message) from error
    current = _git_out(repo, "couldn't read " + branch, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch)
    if current == expect:
        path.unlink(missing_ok=True)
        return
    moved_on = current != merged
    if moved_on:
        holds = git(repo, "merge-base", "--is-ancestor", merged, current)
        if holds.returncode not in (0, 1):
            raise _KeepIntent("git couldn't compare " + branch + " with the merge in " + repo)
        if holds.returncode == 1:
            raise util.refuse("an interrupted merge into " + branch + " can't be finished: " + branch + " is at " +
                              current[:12] + ", which doesn't hold the merge; ask your main session to check " +
                              str(location or repo))
        merged = current  # the merge landed and main moved on: the checkout must match main as it is now
    if location:
        # The same helper check as a normal publication, before any git call that reads or writes content: a
        # repository filter must never run while the journal lock is held.
        try:
            util.helper_refusal(repo, "gate")
            util.helper_refusal(location, "gate")
        except util.YggError as error:
            _alert_once(path, "ygg-merge-intent-held", "An interrupted merge into " + branch + " is held: " +
                        error.message)
            raise util.refuse("an interrupted merge into " + branch + " can't be finished while " + location +
                              " sets git helpers Yggdrasil doesn't run; ask your main session to remove them (" +
                              error.message + ")")
        index = _git_out(location, "couldn't read the checkout's index", "write-tree")
        merged_tree = _git_out(repo, "couldn't read the merge's tree", "rev-parse", "--verify", "--quiet",
                               merged + "^{tree}")
        expect_tree = _git_out(repo, "couldn't read the old commit's tree", "rev-parse", "--verify", "--quiet",
                               expect + "^{tree}")
        compared = git(location, "diff", "--quiet")
        if compared.returncode not in (0, 1):
            raise _KeepIntent("git couldn't compare the checkout's files with its index in " + location)
        files_match_index = compared.returncode == 0
        if index == merged_tree and files_match_index:
            path.unlink(missing_ok=True)
            return
        if moved_on:
            raise util.refuse("an interrupted merge into " + branch + " left " + location + " behind " + branch +
                              ", which has moved on since; ask your main session to bring that checkout to " + branch)
        if index != expect_tree or not files_match_index:
            raise util.refuse("an interrupted merge into " + branch + " left " + location + " with other changes; "
                              "ask your main session to bring that checkout to " + branch + " before anything else")
        moved = git(location, "read-tree", "-m", "-u", expect, merged)
        if moved.returncode != 0:
            raise _KeepIntent("git couldn't update " + location + " to the merge: " +
                              (moved.stderr or moved.stdout).strip())
    path.unlink(missing_ok=True)


def finish_publications(repo=None) -> None:
    """Finish every pending publication intent, each on its own (merge main, merge check, the merge-check step
    and the close run this first). An intent with a parse or shape error, or a merge that never landed, is set
    aside. One the environment can't check now, or that can't be finished, is kept and refuses only for its
    own repo (one log alert per intent): with `repo` given, that repo's refusal is raised; other repos' intents
    never block it, and nothing else propagates."""
    folder = state.root() / "merge-publish"
    if not folder.is_dir():
        return
    try:
        mine = os.path.realpath(str(repo)) if repo else None
    except (ValueError, OSError):
        mine = None
    refusal = None
    with util.locked():
        for path in sorted(folder.glob("*.json")):
            intent = None
            try:
                text, problem = None, None
                try:
                    text = path.read_text()
                except UnicodeDecodeError as error:  # the bytes were read; they aren't text: invalid content
                    problem = "the file can't be read as an intent (" + type(error).__name__ + ")"
                except OSError as error:
                    raise _KeepIntent("the intent file can't be read (" + type(error).__name__ + ")") from error
                if problem is None:
                    try:
                        intent = json.loads(text)
                    except ValueError as error:  # bad JSON: the content itself is wrong
                        problem = "the file can't be read as an intent (" + type(error).__name__ + ")"
                    else:
                        problem = _intent_problem(path, intent)
                if problem:
                    _reject_intent(path, problem)  # untrusted input: never acted on
                    continue
                _finish_publication(intent)
            except _KeepIntent as error:
                _alert_once(path, "ygg-merge-intent-held", "A merge publication intent is kept for now: " + str(error))
                if _belongs(intent, mine, path):
                    refusal = util.refuse("an interrupted merge into main can't be checked now (" + str(error) +
                                          "); it is kept and checked again on the next command")
            except util.YggError as error:
                if _belongs(intent, mine, path):
                    refusal = error
            except Exception as error:  # noqa: BLE001 (an environment failure never wedges the merges)
                _alert_once(path, "ygg-merge-intent-held", "A merge publication intent is kept for now: " +
                            type(error).__name__ + ": " + str(error))
                if _belongs(intent, mine, path):
                    refusal = util.refuse("an interrupted merge into main can't be finished now (" +
                                          type(error).__name__ + "); it is kept and tried again")
    if refusal is not None:
        raise refusal


def _belongs(intent, mine, path=None) -> bool:
    """Whether an intent belongs to the caller's repo: by its file-name key (it works for a file that can't be
    read), or by the repo it names. Never raises."""
    try:
        if not mine:
            return False
        if path is not None and path.name == _intent_path(mine, MAIN).name:
            return True
        return isinstance(intent, dict) and _usable_path(intent.get("repo")) and \
            os.path.realpath(intent["repo"]) == mine
    except Exception:  # noqa: BLE001
        return False


def _caller_is_pm(project: dict) -> None:
    caller = util.current_node()
    if caller is not None and caller != project["pm"]:
        raise util.refuse("identity: only the PM of " + project["id"] + " merges")


def merge_lane(project_id: str, lane: str, repo: str) -> str:
    from .commands import _project_for
    project = _project_for(project_id)
    _caller_is_pm(project)
    util.check_id(lane, "lane")
    from . import quarantine
    quarantine.refuse_box_repo(repo, project.get("programme"))
    # Match the caller's path to an approved repo by its resolved path first, without running git through it.
    # A relayed call (a Codex agent's) must match that way; only a direct caller falls back to the git-based
    # match, which also accepts a worktree of the approved repo.
    entry = _approved_entry_by_path(project_id, repo)
    if entry is None and not util.env("YGG_VIA_RELAY"):
        entry = _repo_entry(project_id, repo)
    if entry is None:
        raise util.refuse("gate: no approved PID of " + project_id + " names " + str(repo))
    # From here on only the approved path is used, never the caller's spelling: a caller's path can be a link
    # the caller retargets once the checks above have passed.
    repo = str(entry["path"])
    util.helper_refusal(repo, "gate")
    branch = "ygg/" + project_id + "/lane/" + lane
    integration = "ygg/" + project_id + "/_integration"
    # The journal lock covers the head, the gate, the merge and the package states, so a cancel or reopen
    # (which takes the same lock) can't land between the checks and the merge (A11.1 AC-06, AC-13).
    with util.locked():
        head = git(repo, "rev-parse", "refs/heads/" + branch).stdout.strip()
        if not head:
            raise util.usage("lane branch " + branch + " does not exist")
        # AC-85 rows 2 and 3 (A11): every package on the lane accepted at the lane head, then the
        # trusted re-check of the lane's commits with Yggdrasil's own git (D5).
        from . import mergecheck
        mergecheck.lane_gate(project_id, lane, repo, head)
        new = _merge_into(repo, integration, head, "Merge " + branch + " into " + integration)
        from . import envelopes
        if envelopes.effective_envelope(project):
            envelopes.morning_go_item(project, "lane-merge", "merge:" + lane)
        for issue in tracker.issues(project_id):
            if issue.get("kind") == "work-package" and issue.get("lane") == lane and issue.get("state") == "accepted":
                issue.header["state"] = "merged"
                issue.save()
    from . import t3
    t3.close_lane(project_id, lane)  # A5.1: a merged lane's T3 project, then its worktree, is removed
    return "merged " + branch + " into " + integration + " at " + new + "\n"


LANE_SYNC_CONFLICT = "lane sync hit a conflict; the lane is unchanged"


def lane_sync(project_id: str, lane: str) -> str:
    """`ygg lane sync <project> --lane <node>` (AC-66 row 3): merge main into the lane with Yggdrasil's own git
    (no hooks), recorded as a sync, so the re-check skips main's commits. A conflict is aborted and the lane is
    left unchanged (resolving it is planned for v1.2). The user or the project's PM runs it."""
    from . import lanecheck
    from .commands import _project_for
    project = _project_for(project_id)
    caller = util.current_node()
    if caller is not None and caller != project["pm"]:
        raise util.refuse("identity: only the PM of " + project_id + " or the user syncs a lane")
    util.check_id(lane, "lane")
    repos = approved_repos(project_id) or []
    if not repos or not repos[0].get("path"):
        raise util.refuse("gate: no approved PID of " + project_id + " names a repo")
    repo = repo_home(str(repos[0]["path"]))
    util.helper_refusal(repo, "gate")
    branch = "ygg/" + project_id + "/lane/" + lane
    head = git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch).stdout.strip()
    if not head:
        raise util.usage("lane branch " + branch + " does not exist")
    main = git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/main").stdout.strip()
    if not main:
        raise util.usage("main does not exist in " + repo)
    from . import clones
    clone_lane = clones.is_clone_lane(project_id, lane)
    if clone_lane:
        # v1.1.1: lane sync on a clone lane (no activation running, a clean clone, the receive step first).
        clones.sync_precheck(project_id, lane)
        head = git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch).stdout.strip()
    if is_ancestor(repo, main, head):
        return "lane " + branch + " already holds main\n" + (clones.sync_feed(project_id, lane) if clone_lane else "")
    from . import config
    identity = config.get("commit_identity")
    if isinstance(identity, dict) and identity.get("name") and identity.get("email"):
        # The sync merge carries the profile's identity (Yggdrasil's own git reads no user config).
        for prefix in ("GIT_AUTHOR", "GIT_COMMITTER"):
            os.environ[prefix + "_NAME"] = str(identity["name"])
            os.environ[prefix + "_EMAIL"] = str(identity["email"])
    with util.locked():
        try:
            new = _merge_into(repo, branch, main, "Sync main into " + branch, no_ff=True)
        except YggError as error:
            after = git(repo, "rev-parse", "refs/heads/" + branch).stdout.strip()
            if after != head or "merge into " not in error.message:
                raise
            raise util.refuse(LANE_SYNC_CONFLICT)
        lanecheck.record_sync(project_id, lane, new, main)
    fed = clones.sync_feed(project_id, lane) if clone_lane else ""
    return "synced main into " + branch + " at " + new + "\n" + fed


def merge_mode(project_id: str) -> str:
    """The merge mode from the protected configuration path only, the same value `ygg config explain` shows:
    a PID value counts only as approved, and programme-record or package values never do."""
    from . import config
    value = (config.explain_all(project_id).get("merge_mode") or (None, None))[0]
    return value if value in ("prepare", "local") else "prepare"


def merge_main(project_id: str, repo: str | None = None, print_only: bool = False) -> str:
    """`ygg merge main` (A11 amendment 14, D4): user-only, after a merge check pass for the current pair; it merges
    into local main in either merge mode, and `--print` prints the prepare-mode git line instead. With `--repo`
    left out it takes the approved PID's one repo (amendment 29)."""
    from . import mergecheck
    return mergecheck.merge_main(project_id, repo, print_only)


def main_push_guard(repo: str, remote: str, url: str | None = None) -> int:
    code, message = push_guard(repo, remote, sys.stdin.read(), url)
    if message:
        print(message, file=sys.stderr)
    return code
