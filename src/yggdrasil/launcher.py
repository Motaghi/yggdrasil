"""Headless launcher: worktrees, hook injection, supervisor and receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config, faults, reducer, seams, state, tracker, util
from .util import YggError

# Capacity failures are classified from real provider signals only (spec: HTTP 429, rate limit, quota).
# Each signal needs provider context at a word or JSON boundary. Tokens that are paths, assignments or
# module-style identifiers are masked first, so "429", "rate-limit" or "code:429" inside them never counts
# (/tmp/rate-limit/missing-claude, express-rate-limit, pytest-4296, id=code:429).
_EDGE = r"(?<![\w-])"
_END = r"(?![\w-])"
CAPACITY_SIGNALS = (
    ("http-status", re.compile(r"\bHTTP(?:/[0-9.]+)?\s+429" + _END, re.I)),
    ("api-error-status", re.compile(r"\b(?:API\s+error|error|status(?:\s+code)?)\s*:?\s*429" + _END, re.I)),
    ("json-status", re.compile(r'"(?:status|status_code|statusCode)"\s*:\s*"?429\b', re.I)),
    ("too-many-requests", re.compile(_EDGE + r"too\s+many\s+requests" + _END, re.I)),
    ("rate-limit", re.compile(_EDGE + r"rate[ _]limit(?:s|ed|ing|_error|_exceeded)?" + _END, re.I)),
    ("quota", re.compile(_EDGE + r"(?:insufficient_quota|quota\s+(?:is\s+)?(?:exceeded|exhausted))" + _END
                         + r"|\bexceeded\s+(?:your\s+|the\s+)?(?:current\s+)?quota\b", re.I)),
    ("usage-limit", re.compile(_EDGE + r"usage\s+limit" + _END, re.I)),
    ("resource-exhausted", re.compile(_EDGE + r"resource[ _]exhausted" + _END, re.I)),
)
_MASKS = (re.compile(r"[\w.~:@-]*(?:[/\\][\w.~:@-]*)+"),  # paths and URLs (code:429 inside one too)
          re.compile(r"\b\w+=\S+"),  # assignments such as id=code:429
          re.compile(r"[\w.]+(?:-[\w.]+){2,}"))  # module-style identifiers such as express-rate-limit
_HTTP_TOKEN = re.compile(r"^HTTP/[0-9.]+$", re.I)


def capacity_signal(text: str) -> str | None:
    """The name of the provider capacity signal in a failure's stderr, or None."""
    masked = str(text or "")
    for mask in _MASKS:
        masked = mask.sub(lambda m: m.group(0) if _HTTP_TOKEN.match(m.group(0)) else " ", masked)
    return next((name for name, pattern in CAPACITY_SIGNALS if pattern.search(masked)), None)


TOKEN_ENV = re.compile(r"^(LINEAR_|YGG_LINEAR_TOKEN)")


def activation_path(activation: str) -> Path:
    return state.root() / "activations" / (activation + ".json")


def receipt(activation: str) -> dict:
    return util.read_json(activation_path(activation), {}) or {}


# Fields written once: identity, the exit record and the terminal mirror. A writer holding an older copy
# of the record never clears them (the supervisor records identity under the creation lock, others under
# the journal lock; the receipt lock serialises the file itself).
WRITE_ONCE = ("pid", "process_started_at", "exited_at", "exit_code", "terminal", "supervisor_pid",
              "supervisor_started_at", "launch_returned_at", "confirm_left_pending_at")


def _scope_for(data: dict) -> dict:
    """Scope metadata for a record rebuilt without it (after a wipe), so an offline stop can match it to a
    programme from local evidence alone. A tracker read, so never under the receipt or creation lock."""
    if not data.get("node") or data.get("programme"):
        return {}
    if util.lock_held(util.RECEIPT_LOCK) or util.lock_held(util.CREATION_LOCK):
        return {}
    try:
        from . import lifecycle
        return {k: v for k, v in lifecycle.scope_fields(str(data["node"])).items() if v is not None}
    except (YggError, OSError):
        return {}


def save_receipt(activation: str, data: dict, scope: dict | None = None) -> None:
    """Write an activation record (write-once fields kept). `scope`, when given, was resolved by the caller
    before any lock; otherwise it is resolved here, and only when the receipt lock is not already held."""
    if scope is None:
        scope = _scope_for(data)
    with util.locked(util.RECEIPT_LOCK):
        current = receipt(activation)
        merged = dict(scope, **data) if scope else dict(data)
        for key, value in scope.items():
            if not merged.get(key):
                merged[key] = value
        for key in WRITE_ONCE:
            if current.get(key) is not None and merged.get(key) is None:
                merged[key] = current[key]
        util.write_json(activation_path(activation), merged)


def update_receipt(activation: str, **fields) -> dict:
    scope = _scope_for(dict(receipt(activation), **fields))  # resolved before the leaf lock (tracker read)
    with util.locked(util.RECEIPT_LOCK):
        data = receipt(activation)
        data.update(fields)
        save_receipt(activation, data, scope=scope)
        return data


def commit_aborted(node_id: str, activation: str, why: str) -> bool:
    from . import lifecycle
    return lifecycle.commit(node_id, activation, "aborted", reason=str(why)[:200])


def stopped(node: dict) -> bool:
    """An active stop intent covers the node (a marker in the safety folder, or a journalled stop no
    committed resume lists)."""
    from . import interlock
    return interlock.stopped(node)


def require_kind(node: dict) -> dict:
    """The node's programme, which must declare its work-product kind (A1.2)."""
    programme = tracker.programme(node["programme"]) if node.get("programme") else None
    if not programme or not programme.get("kind"):
        raise util.refuse("launch precondition: programme " + str(node.get("programme")) +
                          " declares no work-product kind")
    return programme


def readiness(node: dict) -> None:
    """Raise when the node is not launch-ready (A1.2)."""
    if state.paused(node.get("programme")) or stopped(node):
        raise YggError(util.EXIT_PAUSED, "paused: launches are paused for " + node["id"])
    programme = require_kind(node)
    home = tracker.programme_home(programme)
    if not allowed_home(home):
        raise util.refuse("launch precondition: programme " + str(node.get("programme")) + " home " + str(home) +
                          " is not an allowed home folder")
    if node.get("project"):
        project = tracker.project(node["project"]) or {}
        if project.get("state") == "closed":
            raise util.refuse("launch precondition: project " + node["project"] + " is closed")
        if node["role"] in ("tl", "worker"):
            # Build work is dispatched only while the project and its current stage are executing.
            if project.get("state") != "executing":
                raise util.refuse("gate: build dispatch for " + node["project"] + " needs an executing project "
                                  "(state " + str(project.get("state")) + ")")
            if project.get("hold") == "envelope-revoked":
                raise util.refuse("gate: build dispatch for " + node["project"] + " is held")
            stages = tracker.milestones(node["project"]).get("stages") or []
            if not any(stage.get("state") == "executing" for stage in stages) and not reopened_lane_work(node):
                raise util.refuse("gate: " + node["project"] + " has no executing stage")


def reopened_lane_work(node: dict) -> bool:
    """Whether a team lead (or its worker) has reopened work to redo with every stage approved: the project is back
    to executing with its last stage still approved (A11 AC-27 row 2, amendment 13), lane 2's milestones carry
    `all_approved_at`, and a package on the node's lane is `authorised` again after the PM's reopen (AC-65), or
    `rejected` by the senior user while being redone (AC-61 keeps a rejection as `rejected` and owes the lane its
    repair)."""
    if not node.get("project") or node.get("role") not in ("tl", "worker"):
        return False
    data = tracker.milestones(node["project"])
    stages = data.get("stages") or []
    if not (data.get("all_approved_at") and stages and all(stage.get("state") == "approved" for stage in stages)):
        return False
    lane = node.get("id") if node.get("role") == "tl" else node.get("parent")
    return any(issue.get("kind") == "work-package" and issue.get("state") in ("authorised", "rejected")
               and issue.get("lane") == lane
               for issue in tracker.issues(node["project"]))


def git(*args, check=False):
    return util.trusted_git(*args, check=check)


def branch_exists(home, branch) -> bool:
    return git("-C", home, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch).returncode == 0


def _common_dir(path) -> str | None:
    result = git("-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return os.path.realpath(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def _foreign_checkout(home: str, path: Path, branch: str | None) -> bool:
    """Whether the checkout at `path` is not a worktree of `home` at its own top level (and on `branch`)."""
    top = git("-C", path, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or os.path.realpath(top.stdout.strip()) != os.path.realpath(path):
        return True
    if _common_dir(path) is None or _common_dir(path) != _common_dir(home):
        return True
    if branch is not None:
        head = git("-C", path, "symbolic-ref", "-q", "HEAD").stdout.strip()
        return head != "refs/heads/" + branch
    return False


WORKTREES_EXCLUDE = "/.ygg-worktrees/"
# Agents' own notes, such as a multi-line body written for `--body-file` (.ygg/notes/<kind>.md): never committed.
NOTES_EXCLUDE = "/.ygg/"


def exclude_worktrees(home: str) -> None:
    """Keep the worktree folder out of the user's checkout: add it to the repository's own `info/exclude`
    (never to a tracked .gitignore) once, before any worktree is created there."""
    result = git("-C", home, "rev-parse", "--git-path", "info/exclude")
    if result.returncode != 0 or not result.stdout.strip():
        return
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = Path(home) / path
    text = util.read_text(path)
    missing = [line for line in (WORKTREES_EXCLUDE, NOTES_EXCLUDE) if line not in text.splitlines()]
    if not missing:
        return
    util.ensure_dir(path.parent)
    util.atomic_write(path, text + ("" if not text or text.endswith("\n") else "\n") + "".join(m + "\n" for m in missing))


def ensure_worktree(home: str, path: Path, branch: str, base: str) -> Path:
    util.helper_refusal(home, "launch precondition", agents=True)
    exclude_worktrees(home)
    if (path / ".git").exists():
        if _foreign_checkout(home, path, branch):
            raise util.refuse("launch precondition: " + str(path) + " is not the worktree of " + branch +
                              " in " + str(home))
        util.helper_refusal(path, "launch precondition", agents=True)
        _finish_checkout(path)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(home, branch):
        result = git("-C", home, "worktree", "add", "--no-checkout", path, branch)
    else:
        if not branch_exists(home, base):
            base = "main"
        # A recorded creation point with no branch yet (a creation interrupted after its intent, review._create_lane)
        # is where the branch is made, so the lane's drop point and its branch agree (A11.1 AC-06).
        intent = _lane_creation_intent(branch)
        if intent and git("-C", home, "rev-parse", "--verify", "--quiet", intent + "^{commit}").stdout.strip():
            base = intent
        made_from = git("-C", home, "rev-parse", "--verify", "--quiet", base + "^{commit}").stdout.strip()
        result = git("-C", home, "worktree", "add", "--no-checkout", "-b", branch, path, made_from or base)
        if result.returncode == 0 and made_from:
            _record_lane_creation(branch, made_from)
    if result.returncode != 0:
        git("-C", home, "worktree", "prune")
        result = git("-C", home, "worktree", "add", "--no-checkout", "-f", path, branch)
        if result.returncode != 0:
            raise util.refuse("launch precondition: cannot create worktree " + str(path) + ": " +
                              result.stderr.strip())
    _checked_checkout(path)
    return path


try:
    from . import lanedrop  # (A11.1 AC-06); the integration branch brings it in
except ImportError:
    lanedrop = None

LANE_BRANCH = re.compile(r"^ygg/([^/]+)/lane/([^/]+)$")


def _lane_creation_intent(branch: str) -> str | None:
    """The recorded creation commit of a lane branch, if any (lane 1's lanedrop)."""
    match = LANE_BRANCH.match(branch)
    read = getattr(lanedrop, "creation", None) if lanedrop is not None else None
    if read is None or match is None:
        return None
    return read(match.group(1), match.group(2))


def _record_lane_creation(branch: str, commit: str) -> None:
    """Record the commit a lane branch was made from, the drop point of a lane with no accepted or merged package
    (A11.1 AC-06). Only where the branch is first created, never on a relaunch."""
    match = LANE_BRANCH.match(branch)
    if lanedrop is None or match is None:
        return
    lanedrop.record_creation(match.group(1), match.group(2), commit)


def _checked_checkout(path: Path) -> None:
    """Fill a worktree added with --no-checkout, after checking the config git will load for it."""
    util.helper_refusal(path, "launch precondition", agents=True)
    result = git("-C", path, "checkout", "-q", "-f")
    if result.returncode != 0:
        raise util.refuse("launch precondition: cannot check out worktree " + str(path) + ": " +
                          result.stderr.strip())


def _finish_checkout(path: Path) -> None:
    """A worktree whose creation stopped between --no-checkout and the checkout has no index yet: fill it
    from HEAD (already scanned), refusing rather than overwriting any file someone put there meanwhile."""
    index = git("-C", path, "rev-parse", "--git-path", "index").stdout.strip()
    if not index or (Path(path) / index).exists():
        return
    result = git("-C", path, "read-tree", "-m", "-u", "HEAD")
    if result.returncode != 0:
        raise util.refuse("launch precondition: cannot finish checking out worktree " + str(path) + ": " +
                          result.stderr.strip())


def work_area(node: dict, nodes: dict) -> Path:
    """The folder a node works in, computed without creating anything."""
    return working_directory(node, nodes, create=False)


# EXTENSION POINT (the launcher owns it, the review module fills it): the review a project reviewer's launch is for. A "module:function"
# reference to function(node, work) -> dict | None, where `work` is the work set the launch reserves and the dict
# has `id` (rv-0001), `package`, `project`, `base` and `head` (commit SHAs). The review module hands a reviewer one review per
# activation (AC-94 row 3). None means no package review (the programme reviewer, the morning go).
REVIEW_SOURCE = "review:review_source"
DIFF_LIMIT = 100000
DIFF_CUT = "(diff cut at 100000 characters; the full change is in the checkout)"


def _slot(ref):
    """A slot's function, or None while the module that fills it isn't in this build yet (the lanes are built
    apart and joined on the integration branch). Any other import problem is raised."""
    try:
        return seams.resolve(ref)
    except ModuleNotFoundError as error:
        if ref and error.name == "yggdrasil." + str(ref).partition(":")[0]:
            return None
        raise


def review_for(node: dict, work) -> dict | None:
    if node.get("role") != "reviewer" or not node.get("project"):
        return None
    function = _slot(REVIEW_SOURCE)
    found = function(node, work) if function is not None else None
    if not isinstance(found, dict) or not found.get("id") or not found.get("head"):
        return None
    util.check_id(str(found["id"]), "review")
    for key in ("base", "head"):
        if found.get(key) is not None and not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", str(found[key])):
            raise util.refuse("launch precondition: review " + str(found["id"]) + " has no valid " + key + " commit")
    return {k: found.get(k) for k in ("id", "package", "project", "base", "head")}


def review_checkout_path(home: str, project: str, review: dict) -> Path:
    """`<home>/.ygg-worktrees/<project>/_review-<review id>` (AC-95)."""
    return Path(home) / ".ygg-worktrees" / project / ("_review-" + str(review["id"]))


def review_checkout(home: str, path: Path, sha: str) -> Path:
    """A detached checkout of the reviewed head: the reviewer reads exactly what it reviews (AC-95 row 1)."""
    util.helper_refusal(home, "launch precondition", agents=True)
    exclude_worktrees(home)
    if (path / ".git").exists():
        if _foreign_checkout(home, path, None):
            raise util.refuse("launch precondition: " + str(path) + " is not a review checkout of " + str(home))
        util.helper_refusal(path, "launch precondition", agents=True)
        _finish_checkout(path)
        result = git("-C", path, "checkout", "--detach", "--quiet", sha)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        result = git("-C", home, "worktree", "add", "--no-checkout", "--detach", path, sha)
        if result.returncode == 0:
            _checked_checkout(path)
    if result.returncode != 0:
        raise util.refuse("launch precondition: cannot check out " + sha + " for review in " + str(path) + ": " +
                          result.stderr.strip())
    return path


def working_directory(node: dict, nodes: dict, create: bool = True, review: dict | None = None,
                      testing_package: str | None = None) -> Path:
    programme = tracker.programme(node["programme"])
    home = tracker.programme_home(programme)
    kind = programme["kind"]
    project = node.get("project")
    if review and project and kind in ("git-repo", "shared-tree"):
        path = review_checkout_path(home, project, review)
        return review_checkout(home, path, str(review["head"])) if create else path
    if kind == "vault-docs":
        lane = node["id"] if node["role"] != "worker" else node.get("parent")
        if node["role"] in ("pm", "reviewer"):
            lane = "_integration"
        path = Path(home) / ".ygg-staging" / (project or "_programme") / lane
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path
    base = Path(home) / ".ygg-worktrees"
    def planned(home_: str, path: Path, *rest) -> Path:
        return path

    ensure, detached = (ensure_worktree, detached_worktree) if create else (planned, planned)
    if node["role"] == "reviewer" or not project:
        return ensure(home, base / "_review" / node["id"], "ygg/_review/" + node["id"], "main")
    integration = "ygg/" + project + "/_integration"
    lane_owner = node["id"] if node["role"] == "tl" else None
    if node["role"] == "worker":
        parent = nodes.get(node.get("parent"), {})
        if parent.get("role") == "tl":
            lane_owner = parent["id"]
    if lane_owner:
        return ensure(home, base / project / "lane" / lane_owner,
                               "ygg/" + project + "/lane/" + lane_owner, integration)
    if node["role"] == "su":
        # The reservation's package, never another one: if it left testing since, the launch is refused (the caller
        # aborts the activation, and a retry it was given stays owed).
        target = pinned_testing_target(node, nodes, testing_package) if testing_package else _testing_target(node, nodes)
        if testing_package and target is None:
            raise util.refuse(_LEFT_TESTING.format(testing_package))
        ref = target["branch"] if target else integration
        return detached(home, base / project / ("_test-" + node["id"]), ref)
    return ensure(home, base / project / "_integration", integration, "main")


def _testing_target(node: dict, nodes: dict, prefer: str | None = None) -> dict | None:
    """For a senior user: the package it tests, `prefer` when that one is ready-for-test on a paired lane (the one
    its reservation chose), else the oldest ready-for-test package of a paired lane (its lane head is checked
    out)."""
    if node.get("role") != "su" or not node.get("project"):
        return None
    lanes = [lane for lane in node.get("pairs_with") or []
             if (nodes.get(lane) or {}).get("project") == node["project"]]
    waiting = [i for i in tracker.issues(node["project"]) if i.get("kind") == "work-package"
               and i.get("state") == "ready-for-test" and i.get("lane") in lanes]
    if not waiting:
        return None
    ordered = sorted(waiting, key=lambda i: str(i.get("created_at") or ""))
    package = next((i for i in ordered if prefer and i.id == prefer), ordered[0])
    return {"package": package.id, "lane": package.get("lane"),
            "branch": "ygg/" + node["project"] + "/lane/" + package.get("lane")}


_LEFT_TESTING = "launch precondition: {} is no longer ready for test; this launch tests no other package"


def pinned_testing_target(node: dict, nodes: dict, package: str | None) -> dict | None:
    """The testing target of exactly `package` (the one the reservation fixed), or None when it is no longer
    ready-for-test on a paired lane. Never falls back to another package."""
    target = _testing_target(node, nodes, package) if package else None
    return target if target and target["package"] == package else None


def owed_testing_package(node: dict, nodes: dict, given: list[dict]) -> str | None:
    """For a senior user: the package this activation tests, chosen from its owed work (AC-94 row 3): the oldest
    ready-for-test package of a paired lane that an owed item is about. So a package whose item was already given
    to an earlier activation (consumed, but not yet accepted) never shadows the next package that is owed. None
    when no owed item names such a package (then the oldest ready-for-test one is tested, as before)."""
    if node.get("role") != "su" or not node.get("project"):
        return None
    ready = _ready_for_test(node, nodes)
    # A retry (or a capacity relaunch) continues the package its failed activation was testing: that package comes
    # first, so another owed package never takes its retry.
    retried = [package for package in (_item_package(i) for i in given if i.get("kind") in ("retry", "capacity"))
               if package in ready]
    if retried:
        return retried[0]
    owed = {str(i["package"]) for i in given if i.get("package")}
    if not owed:
        return None
    lanes = [lane for lane in node.get("pairs_with") or [] if (nodes.get(lane) or {}).get("project") == node["project"]]
    waiting = sorted((i for i in tracker.issues(node["project"]) if i.get("kind") == "work-package"
                      and i.get("state") == "ready-for-test" and i.get("lane") in lanes and i.id in owed),
                     key=lambda i: str(i.get("created_at") or ""))
    return waiting[0].id if waiting else None


def _item_package(item: dict) -> str | None:
    """The package an owed item is about: its own `package`, or for a retry or capacity relaunch, the package its
    failed activation was testing."""
    if item.get("package"):
        return str(item["package"])
    if item.get("kind") in ("retry", "capacity") and item.get("id"):
        found = receipt(str(item["id"])).get("testing_package")
        return str(found) if found else None
    return None


def _ready_for_test(node: dict, nodes: dict) -> set:
    """The packages a senior user could test now: ready-for-test on one of its paired lanes."""
    lanes = [lane for lane in node.get("pairs_with") or [] if (nodes.get(lane) or {}).get("project") == node["project"]]
    return {i.id for i in tracker.issues(node["project"]) if i.get("kind") == "work-package"
            and i.get("state") == "ready-for-test" and i.get("lane") in lanes}


def one_piece_of_work(node: dict, nodes: dict, given: list[dict], testing_package: str | None = None) -> list[dict]:
    """A senior user tests one package per activation (AC-94 row 3): items about another package that is also ready
    for test stay owed for its next activation. Everything else goes with this one: items about the package it
    tests (`testing_package`, chosen from its owed work), about no package, or about a package not ready for test
    (such as its criteria for a package still being built), so no item waits for an activation of its own."""
    if node.get("role") != "su" or not node.get("project"):
        return given
    target = _testing_target(node, nodes, testing_package)
    chosen = target["package"] if target else None
    others = _ready_for_test(node, nodes) - {chosen}

    def keep(item: dict) -> bool:
        package = _item_package(item)
        if item.get("kind") in ("retry", "capacity") and package:
            # A retry continues only its own package; it never travels with another one, also not once its package
            # has left testing (rejected, accepted): then it waits, and the launch says so (withheld_retries).
            return package == chosen
        return package not in others
    return [i for i in given if keep(i)]


def retry_hold_reason(node: dict, nodes: dict, items: list[dict]) -> str | None:
    """Why a senior user's launch is held when all it owes is a retry whose package has left testing, or None. The
    one predicate for the launch refusal and for `ygg status` (A7 I4), read-only: the retry is never consumed,
    settled or reset here."""
    if node.get("role") != "su" or not items:
        return None
    kept = one_piece_of_work(node, nodes, items, owed_testing_package(node, nodes, items))
    held_back = withheld_retries(node, items, kept)
    if kept or not held_back:
        return None
    return "retry withheld: " + ", ".join(held_back) + " left testing"


def withheld_retries(node: dict, given: list[dict], kept: list[dict]) -> list[str]:
    """The packages whose retries one_piece_of_work held back (for the visible hold when nothing else is left)."""
    if node.get("role") != "su":
        return []
    return sorted({str(_item_package(i)) for i in given if i not in kept and i.get("kind") in ("retry", "capacity")
                   and _item_package(i)})


def one_review(given: list[dict], review: dict) -> list[dict]:
    """A reviewer handles one review per activation (AC-94 row 3): of its owed items, those about another review or
    about another package stay owed; items about neither go with this one."""
    return [i for i in given if (not i.get("review") or i.get("review") == review["id"])
            and (not i.get("package") or i.get("package") == review.get("package"))]


def detached_worktree(home: str, path: Path, ref: str) -> Path:
    """A detached checkout for testing: commits there move no branch (senior users are read-only)."""
    util.helper_refusal(home, "launch precondition", agents=True)
    target = ref if branch_exists(home, ref) else "main"
    exclude_worktrees(home)
    if (path / ".git").exists():
        if _foreign_checkout(home, path, None):
            raise util.refuse("launch precondition: " + str(path) + " is not a testing worktree of " + str(home))
        util.helper_refusal(path, "launch precondition", agents=True)
        _finish_checkout(path)
        result = git("-C", path, "checkout", "--detach", "--quiet", target)
        if result.returncode != 0:
            raise util.refuse("launch precondition: cannot check out " + target + " in testing worktree " +
                              str(path) + ": " + result.stderr.strip())
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    result = git("-C", home, "worktree", "add", "--no-checkout", "--detach", path, target)
    if result.returncode != 0:
        raise util.refuse("launch precondition: cannot create worktree " + str(path) + ": " + result.stderr.strip())
    _checked_checkout(path)
    return path


def ygg_executable() -> str:
    """Realpath of the ygg on the launching PATH, resolved once per launch and written into every guard hook,
    so a session cannot swap in another ygg by changing its own PATH later."""
    found = shutil.which("ygg", path=os.environ.get("PATH", os.defpath))
    if not found:
        raise util.refuse("launch precondition: no ygg executable on PATH")
    return os.path.realpath(found)


def hooks_root() -> Path:
    """Yggdrasil's hooks folder, `<state root>.safety/hooks` (amendment 15): it survives an emptied state root, so an
    agent's git always finds the lane check (AC-96 row 2)."""
    from . import safety
    return safety.root() / "hooks"


HOOKS_RECORD = "hooks.json"


def hooks_dir(ygg: str) -> Path:
    """Write Yggdrasil's lane hooks, each calling the recorded `ygg` executable, and re-create any whose content
    differs from what it should be (every launch and tick, AC-96 row 3)."""
    path = util.ensure_dir(hooks_root())
    util.ensure_dir(state.root() / "inboxes")  # the local inbox store the guard's unread check reads (AC-53)
    scripts = hook_scripts(ygg)
    for name, script in scripts.items():
        hook = path / name
        if not hook.exists() or hook.is_symlink() or hook.read_text(errors="replace") != script:
            if hook.is_symlink():
                hook.unlink()
            util.atomic_write(hook, script, mode=0o755)
    record = path.parent / HOOKS_RECORD
    wanted = {"ygg": ygg, "sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in scripts.items()}}
    if util.read_json(record, None) != wanted:
        util.write_json(record, wanted)
    return path


def repair_hooks() -> None:
    """Put Yggdrasil's lane hooks back as the last launch wrote them, with the ygg it recorded (each tick)."""
    from . import safety
    record = util.read_json(safety.root() / HOOKS_RECORD, None)
    if isinstance(record, dict) and isinstance(record.get("ygg"), str) and os.path.isabs(record["ygg"]):
        hooks_dir(record["ygg"])


def hook_scripts(ygg: str) -> dict:
    """The lane hook scripts by name, each calling `ygg`. Their contents are lane 1's (`lanecheck.hook_scripts`,
    A11 AC-16); until that is merged in, today's three scripts."""
    from . import lanecheck
    if hasattr(lanecheck, "hook_scripts"):
        return lanecheck.hook_scripts(ygg)
    scripts = {
        "pre-push": "#!/bin/sh\nexec " + shlex.quote(ygg) + ' push-guard "$(git rev-parse --show-toplevel)" "$1" "$2"\n',
        # Yggdrasil's own light lane check first (leak scan, touched files), then the home's own pre-commit.
        "pre-commit": "#!/bin/sh\n" + shlex.quote(ygg) + " hook lane-pre-commit || exit 1\n" +
                      HOME_PRE_COMMIT.split("\n", 1)[1],
        # The commit message: leak scan, and no Co-Authored-By trailer when commit_identity says none.
        "commit-msg": "#!/bin/sh\nexec " + shlex.quote(ygg) + ' hook lane-commit-msg "$1"\n',
    }
    return scripts


# Agent worktrees use Yggdrasil's hooks folder, so this pre-commit hands each agent commit to the home
# repository's own pre-commit hook (its repository-level core.hooksPath, else .git/hooks), if it has one.
HOME_PRE_COMMIT = """#!/bin/sh
common=$(git rev-parse --path-format=absolute --git-common-dir) || exit 1
configured=$(git config --file "$common/config" --includes --get core.hooksPath)
if [ -n "$configured" ]; then
  # A relative hooks path is relative to the home checkout, which the launcher records as ygg.home.
  home=$(git config --get ygg.home)
  [ -n "$home" ] || home=$(git worktree list --porcelain | sed -n '1s/^worktree //p')
  case "$configured" in
    /*) dir=$configured ;;
    "~/"*) dir=$HOME/${configured#"~/"} ;;
    *) dir=$home/$configured ;;
  esac
else
  dir=$common/hooks
fi
here=$(cd "$(dirname "$0")" && pwd -P)
there=$(cd "$dir" 2>/dev/null && pwd -P)
[ -n "$there" ] && [ "$there" != "$here" ] && [ -x "$dir/pre-commit" ] && exec "$dir/pre-commit" "$@"
exit 0
"""


# EXTENSION POINT (the launcher owns it; A11 seam): the hook events every Claude launch registers and the `ygg hook`
# action each runs (test conventions, "Post events"; AC-20 row 3). The T3 launcher uses the same table.
HOOK_EVENTS = (("PreToolUse", "pretool"), ("PostToolUse", "posttool"), ("PostToolUseFailure", "posttool"))


def hook_settings(quoted_ygg: str) -> dict:
    """The `hooks` mapping of the launch settings, from HOOK_EVENTS; `quoted_ygg` is the shell-quoted ygg path."""
    return {event: [{"matcher": "*", "hooks": [{"type": "command", "command": quoted_ygg + " hook " + action}]}]
            for event, action in HOOK_EVENTS}


def settings_content(quoted_ygg: str, permissions: dict | None = None) -> dict:
    """EXTENSION POINT (lane 3 owns; A11 seam): the Claude Code launch settings, shared by the headless and T3
    launchers. Hooks are switched on explicitly, so a project or user setting cannot turn the guard off. Lane 3
    adds the attribution settings here (AC-17 row 3)."""
    # Claude Code adds its own Co-Authored-By trailer unless both are set (smoke 4, 19 Nov), whatever the
    # profile's `trailers` value.
    content = {"disableAllHooks": False, "hooks": hook_settings(quoted_ygg), "includeCoAuthoredBy": False,
               "attribution": {"commit": "", "pr": ""}}
    if permissions:
        content["permissions"] = permissions  # what the agent may do without asking (the user agreed)
    return content


def inject_hooks(activation: str, cwd: Path, is_git: bool, home: str | None = None,
                 permissions: dict | None = None) -> Path:
    ygg = ygg_executable()
    try:
        directory = state.root() / "launch" / activation
        if directory.exists():
            raise FileExistsError("launch directory exists: " + str(directory))
        util.ensure_dir(directory)
        settings = directory / "settings.json"
        content = settings_content(shlex.quote(ygg), permissions)
        util.atomic_write(settings, json.dumps(content, indent=2))
        hooks = hooks_dir(ygg)
        if is_git:
            results = [git("-C", cwd, "config", "extensions.worktreeConfig", "true")]
            for key, value in worktree_settings(str(hooks), home):
                results.append(git("-C", cwd, "config", "--worktree", key, value))
            if any(r.returncode for r in results):
                raise OSError("".join(r.stderr for r in results).strip())
    except OSError as error:
        raise util.refuse("launch precondition: hook injection failed: " + str(error))
    return settings


def worktree_settings(hooks: str, home: str | None) -> list[tuple[str, str]]:
    """What Yggdrasil writes into a lane's worktree config at launch: its hooks folder, the home checkout (for the
    composed pre-commit), the profile's commit identity (AC-17 row 2, AC-33 row 3), and the settings that keep an
    agent's git from waiting on GPG or an editor (AC-33; util.EXACT_VALUES accepts only these values)."""
    found = [("core.hooksPath", hooks)]
    if home:
        found.append(("ygg.home", str(home)))
    identity = config.get("commit_identity") or {}
    if isinstance(identity, dict):
        for key in ("name", "email"):
            if isinstance(identity.get(key), str) and identity[key]:
                found.append(("user." + key, identity[key]))
    found += [("commit.gpgsign", "false"), ("core.editor", "true"), ("sequence.editor", "true")]
    return found


EFFORTS = ("low", "medium", "high", "xhigh")
REVIEWER_CHOICE = "review:reviewer_choice"  # filled by the review module: function(node) -> (model, effort, cli) or None


def _reviewer_choice(node: dict):
    function = _slot(REVIEWER_CHOICE)
    return function(node) if function is not None else None
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def model_for(node: dict) -> tuple[str, str, str]:
    """Model, effort and CLI: GPT-family models run on Codex, others on the configured CLI."""
    from . import models, review
    resolved = models.resolve(node["id"])
    model, effort = str(resolved["model"]), str(resolved["effort"])
    chosen = _reviewer_choice(node)  # the project reviewer's model family (AC-12 row 3, AC-56)
    if chosen is not None:
        model, effort = str(chosen[0]), str(chosen[1])
    if effort not in EFFORTS:
        raise util.refuse("launch precondition: effort '" + effort + "' is not one of " + ", ".join(EFFORTS))
    if not MODEL_RE.match(model):
        raise util.refuse("launch precondition: invalid model name '" + model + "'")
    cli = "codex" if model.startswith("gpt") else "claude-code"
    if cli == "codex" and node.get("role") in models.BUILDER_ROLES:
        raise util.refuse(models.CODEX_REFUSAL)  # D1 (A11 seam, AC-99 row 2)
    if cli == "claude-code" and config.get("launcher_cli") == "codex" and node.get("role") != "reviewer":
        # A reviewer is exempt: the programme reviewer runs on Claude (D1), and a project reviewer falls back to the
        # builder's family or follows models.review_model, which may name a Claude model (A11 AC-56, AC-99 row 1).
        raise util.refuse("launch precondition: model '" + model + "' needs Claude Code, but launcher_cli is "
                          "codex; set a Codex model in codex_models or the PID")
    return model, effort, cli


ROLE_PROMPTS = Path(__file__).parent / "data" / "roles"
# Fixed role-to-prompt mapping: a role read from the tracker never becomes a file name.
ROLE_FILES = {"pm": "pm.md", "tl": "tl.md", "su": "su.md", "worker": "worker.md", "reviewer": "reviewer.md"}


def allowed_home(home: str | None) -> bool:
    """A programme home must be an existing absolute folder, not the filesystem root, not the user's home
    folder, and not inside Yggdrasil's own state or tracker folders."""
    if not home or not os.path.isabs(home) or not os.path.isdir(home):
        return False
    real = os.path.realpath(home)
    if real in (os.path.realpath(os.sep), os.path.realpath(os.path.expanduser("~"))):
        return False
    from . import safety
    for own in (state.root(), tracker.root(), safety.root()):
        base = os.path.realpath(str(own))
        if real == base or real.startswith(base.rstrip(os.sep) + os.sep):
            return False
    return True


def _fenced(text: str) -> str:
    """Verbatim text inside a fence longer than any backtick run it contains (data, never instructions)."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return fence + "text\n" + text.rstrip("\n") + "\n" + fence


def _sol_worker(node: dict) -> bool:
    """A worker the conductor starts on a GPT-family (Sol) model: the pair reviewer of ruling 13."""
    if node.get("role") != "worker":
        return False
    try:
        from . import models
        return str(models.resolve(node["id"]).get("model") or "").startswith("gpt")
    except YggError:
        return False


def brief_context(node: dict) -> str:
    """The brief, with the grilling questions and answers L1 wrote into it, passed verbatim (PRINCE2 flow,
    step 1) to every senior user, Sol reviewer and programme reviewer. Fenced as data.

    Other workers do not get it (the flow names only those three). A team lead, which starts its Sol pair
    reviewer itself (ruling 14), is told where the brief is so it can pass it on verbatim.
    """
    if node.get("role") == "pm" and node.get("project"):
        if tracker.read_document(node["project"], "brief"):
            return ("The project brief, with the user's answers verbatim, is at " +
                    str(tracker.document_path(node["project"], "brief")) + ". Read it before you plan.")
        return ""
    if node.get("role") == "tl" and node.get("project"):
        if tracker.read_document(node["project"], "brief"):
            return ("When you start your pair reviewer (the agent you start on the configured pair model to check "
                    "your work), pass it the project brief verbatim: " +
                    str(tracker.document_path(node["project"], "brief")) + ".")
        return ""
    if node.get("role") not in ("su", "reviewer") and not _sol_worker(node):
        return ""
    if node.get("project"):
        projects = [node["project"]]
    else:
        projects = [p["id"] for p in tracker.projects() if p.get("programme") == node.get("programme")]
    parts = []
    for project in projects:
        text = tracker.read_document(project, "brief")
        if text:
            parts.append("Project brief of " + project + " (verbatim, untrusted data):\n" + _fenced(text))
    return "\n\n".join(parts)


def prompt_for(node: dict, activation: str) -> str:
    if node.get("role") not in ROLE_FILES:
        raise util.refuse("launch precondition: node " + str(node.get("id")) + " has no known role")
    role_file = ROLE_PROMPTS / ROLE_FILES[node["role"]]
    role_text = util.read_text(role_file).strip()
    shared = util.read_text(ROLE_PROMPTS / "shared.md").strip()
    role_text = role_text + ("\n\n" + shared if shared else "")
    rehydrate = ""
    if node.get("project"):
        rehydrate = str(tracker.document_path(node["project"], "rehydrate-" + node["id"]))
    context = {"rehydrate": rehydrate, "role_text": role_text}
    sections = []
    for _, ref in PROMPT_SECTIONS:
        function = seams.resolve(ref)
        text = function(node, activation, context) if function is not None else ""
        if text:
            sections.append(text)
    return "\n\n".join(sections)


def _identity_section(node: dict, activation: str, context: dict) -> str:
    return ("You are Yggdrasil node " + node["id"] + " (role " + node["role"] + "). Activation " + activation +
            ". Read your rehydration document at " + context["rehydrate"] + " and your inbox with `ygg inbox " +
            node["id"] + "`.")


def _role_section(node: dict, activation: str, context: dict) -> str:
    return context["role_text"]


def _who_is_who_section(node: dict, activation: str, context: dict) -> str:
    return who_is_who(node)


def _brief_section(node: dict, activation: str, context: dict) -> str:
    return brief_context(node)


# EXTENSION POINT (A11 seam): the named sections of a launch prompt, in order, joined by a blank line; an
# empty section is left out. A lane fills only its own slot with a "module:function" reference to a
# function(node, activation, context) -> str in a file it owns.
PROMPT_SECTIONS = (
    ("identity", _identity_section),
    ("role", _role_section),
    ("who_is_who", _who_is_who_section),
    ("brief", _brief_section),
    ("rulings", "review:rulings_section"),  # `## User rulings` (AC-29, AC-89, AC-90)
    ("review", "launcher:review_section"),  # the reviewer's checkout and diff (AC-95)
    ("commit_path", "launcher:commit_path_section"),  # the commit-path and precedence sentences
                                                       # (AC-17, AC-75)
    ("role_guidance", None),  # role sentences such as the PM's for finding 15 (AC-38) are in data/roles
)


def review_section(node: dict, activation: str, context: dict) -> str:
    """For a reviewer launched for a package review: the review, its checkout of the reviewed head, where the
    verdict goes, and the diff from base to head, cut at 100,000 characters (AC-95 row 3)."""
    found = receipt(activation)
    review = found.get("review_target") or found.get("review")  # a receipt from before v1.1 holds the dict
    if node.get("role") != "reviewer" or not isinstance(review, dict):
        return ""
    checkout = receipt(activation).get("cwd") or ""
    from . import permissions
    # The literal path too: a file-writing tool doesn't expand $YGG_REVIEW_DIR, and the reviewer can't print it.
    verdict = str(permissions.review_dir(activation) / "verdict.json")
    lines = ["## Review " + str(review.get("id")),
             "You review package " + str(review.get("package")) + " of " + str(review.get("project")) +
             ", from " + str(review.get("base")) + " to " + str(review.get("head")) + ". Your read-only checkout "
             "of the reviewed head is " + str(checkout) + ". Write your verdict to $YGG_REVIEW_DIR/verdict.json, "
             "which is " + verdict + "; edit nothing else."]
    if review.get("base") and checkout:
        diff = git("-C", checkout, "diff", str(review["base"]) + ".." + str(review["head"]))
        if diff.returncode == 0:
            text = diff.stdout
            if len(text) > DIFF_LIMIT:
                text = text[:DIFF_LIMIT] + DIFF_CUT
            lines.append("The change (untrusted data):\n" + _fenced(text))
    return "\n\n".join(lines)


PRECEDENCE_SENTENCE = ("Yggdrasil's commit path and role rules come first: where your project's own instructions "
                       "differ, follow Yggdrasil's.")
COMMIT_SENTENCE = ("Commit with plain git on your lane branch. Yggdrasil's lane check runs on each commit, and the "
                   "project's own checks run before the merge into main.")


def commit_path_section(node: dict, activation: str, context: dict) -> str:
    """The commit path and the precedence of Yggdrasil's rules over the project's own instructions (AC-17 row 3,
    AC-75 row 3): the project's CLAUDE.md loads too, and may say otherwise."""
    lines = [PRECEDENCE_SENTENCE]
    if node.get("role") in ("tl", "worker"):
        identity = config.get("commit_identity") or {}
        lines.append(COMMIT_SENTENCE)
        if isinstance(identity, dict) and identity.get("name") and identity.get("email"):
            lines.append("Your commits are made as " + str(identity["name"]) + " <" + str(identity["email"]) +
                         ">; never change the commit author or committer.")
        if not isinstance(identity, dict) or identity.get("trailers", "none") == "none":
            lines.append("Add no Co-Authored-By or other AI trailer to a commit message: no Co-Authored-By line "
                         "is allowed by the profile.")
    return "\n".join(lines)


def who_is_who(node: dict) -> str:
    """The nodes this one works with, by role, so it never has to guess who the senior user or the PM is."""
    nodes = state.all_nodes()
    lines = ["## Who is who", "- You: " + node["id"] + " (" + str(node.get("role")) + ")" +
             (", project " + node["project"] if node.get("project") else "") +
             (", programme " + node["programme"] if node.get("programme") else "") + "."]
    if node.get("project"):
        team = [n for n in nodes.values() if n.get("project") == node["project"]]
        words = {"pm": "project manager", "tl": "team lead", "su": "senior user", "worker": "worker"}
        for role in ("pm", "tl", "su", "worker"):
            for other in sorted((n for n in team if n.get("role") == role), key=lambda n: n["id"]):
                if other["id"] == node["id"]:
                    continue
                extra = ""
                if role == "su" and other.get("pairs_with"):
                    extra = ", paired with " + ", ".join(other["pairs_with"])
                elif role == "worker" and other.get("parent"):
                    extra = ", working for " + str(other["parent"])
                lines.append("- " + other["id"] + ": " + words[role] + extra + ".")
    reviewers = sorted(n["id"] for n in nodes.values()
                       if n.get("role") == "reviewer" and n.get("programme") == node.get("programme"))
    if reviewers:
        lines.append("- " + ", ".join(reviewers) + ": programme reviewer.")
    lines.append("- l1: the main session, which acts for the user. It is not a senior user and approves "
                 "nothing on its own; only the project manager messages it.")
    return "\n".join(lines)


def _alert_uncertain(node_id: str, activation: str, why: str) -> None:
    state.alert("ygg-uncertain", "Activation " + activation + " of " + node_id + " is uncertain (" + why +
                "); held, not relaunched", node=node_id, activation=activation)


def mark_uncertain(node_id: str, activation: str, why: str, *, allow_confirmed: bool = False) -> bool:
    """Hold an activation whose outcome cannot be told: receipt and runtime, then alert. Nothing is stored
    as truth: status derives uncertainty from the lifecycle facts and fresh evidence.

    Only a pending (or receipt-less) activation becomes uncertain, or with `allow_confirmed` a confirmed one
    whose process is gone without an exit record; an aborted one is left alone, one whose valid exit record is
    on file by the time the lock is held is left to that record, and an activation already held is not
    journalled or alerted twice. Returns whether it was newly held.
    """
    with util.locked():
        data = receipt(activation)
        if data.get("outcome") == "uncertain":
            # Already held (journalled and alerted once): only bring a runtime that lags behind in line.
            runtime = state.runtime(node_id)
            if runtime.get("activation") == activation and runtime.get("outcome") != "uncertain":
                runtime.update(state="running" if runtime.get("state") != "stopped" else "stopped",
                               outcome="uncertain")
                state.save_runtime(node_id, runtime)
            return False
        refused = ("aborted",) if allow_confirmed else ("confirmed", "aborted")
        if data.get("outcome") in refused:
            return False
        if start_evidence(data) in EXIT_VERDICTS:
            # A valid exit record landed after the caller read the receipt: it decides, nothing is held or
            # alerted. A record that can't be read is no reason to skip the hold.
            return False
        data["outcome"] = "uncertain"
        data["uncertain_reason"] = why
        save_receipt(activation, data)
        runtime = state.runtime(node_id)
        if runtime.get("activation") == activation:
            runtime.update(state="running" if runtime.get("state") != "stopped" else "stopped",
                           outcome="uncertain")
            state.save_runtime(node_id, runtime)
    _alert_uncertain(node_id, activation, why)
    return True


def hold_reason(node: dict, activation: str) -> str | None:
    """Why an activation must not start its agent now (caller holds the lock).

    Stop, pause, every readiness gate (project and stage state, holds) and decision blockers are checked
    again here, right before the agent process starts, so a gate persisted during preparation wins.
    """
    if stopped(node):
        return "stopped"
    if state.paused(node.get("programme")):
        return "paused"
    runtime = state.runtime(node["id"])
    if runtime.get("activation") != activation:
        return "superseded"
    if runtime.get("state") == "stopped":
        return "stopped"
    data = receipt(activation)
    if data.get("outcome") == "aborted" or data.get("abort_step"):
        return "aborted"
    try:
        readiness(node)
    except YggError as error:
        return error.message.replace("REFUSED: ", "", 1)
    if node.get("role") != "pm":
        from . import decisions
        blocked = decisions.blocking(node["id"], state.all_nodes())
        if blocked:
            return "blocked by " + ", ".join(blocked)
    for _, ref in START_CHECKS:
        function = _slot(ref)
        why = function(node, activation) if function is not None else None
        if why:
            return str(why)
    return None


REQUEUE_NOT = ("stopped", "superseded", "aborted")

# EXTENSION POINTS (the launcher owns them, other modules fill them; A11.1 AC-13). Each slot is a "module:function" reference.
# RESERVE_CHECKS: function(node) -> refusal text or None, run under the launch lock before anything is reserved,
# for example "a cancel on this lane was interrupted; ask your main session to finish it" (the launch exits 3).
# START_CHECKS: function(node, activation) -> reason or None, the last check before the agent starts (the
# supervisor's final admission check, and the launcher's check right after preparation). A reason that starts with
# "cancelled" aborts the start without putting the work back in the queue whole: the slot's owner settles the owed
# items item by item, so the next tick doesn't reserve and abort again.
RESERVE_CHECKS = (("cancel", "cancel:reserve_check"),)  # an interrupted cancel on the node's lane
START_CHECKS = (("cancel", "cancel:start_check"),)  # a work-set package cancelled after this reservation, or a cancel
                                      # in progress on its lane recorded after it


def _cancelled(why) -> bool:
    return isinstance(why, str) and why.startswith("cancelled")


def abort_effects(node_id: str, activation: str, why: str | None, previous: dict | None = None) -> None:
    """Apply an abort to the wake queue and the runtime (caller holds the lock). Idempotent.

    Order: the owed launch first (unless stopped or superseded), then the runtime. A stopped node stays
    stopped; otherwise the runtime returns to its previous state, or idle. A liveness retry the aborted
    activation was given stays owed from the exit fact it retries, with its count.
    """
    runtime = state.runtime(node_id)
    stopped_node = runtime.get("state") == "stopped" or why == "stopped"
    # The aborted activation's work set was never consumed, so it is owed again. A launch that was given
    # nothing (an explicit launch) is owed again through one event fact, unless the node owes work already.
    if not stopped_node and why not in REQUEUE_NOT and not _cancelled(why) and node_id not in state.wakes():
        state.wake(node_id, "launch-aborted")
    if runtime.get("activation") != activation:
        return
    if stopped_node:
        if runtime.get("state") != "stopped" or runtime.get("outcome") != "aborted":
            runtime.update(state="stopped", outcome="aborted")
            state.save_runtime(node_id, runtime)
        return
    if previous is not None:
        state.save_runtime(node_id, previous)
        return
    restored = dict(runtime, state="idle", outcome="aborted")
    if restored != runtime:
        state.save_runtime(node_id, restored)


def abort_activation(node_id: str, activation: str, why: str, previous: dict | None = None) -> None:
    """Abort a not-yet-started activation as one ordered, idempotent sequence (caller holds the lock):

    1. the receipt records the abort request (`abort_step: requested`), so no agent may start and reconcile
       knows to finish it whatever the runtime says;
    2. the wake is restored, then the runtime;
    3. the receipt outcome becomes `aborted` and the request is closed.
    """
    data = receipt(activation)
    if data.get("outcome") == "aborted" and not data.get("abort_step"):
        return
    from . import lifecycle
    if lifecycle.facts(_programme(node_id), activation) or data.get("lifecycle"):
        if not commit_aborted(node_id, activation, why):
            done = lifecycle.mirror_terminal(node_id, activation)
            if done != "aborted":
                return  # another terminal outcome stands (I3)
    if not data.get("abort_step"):
        update_receipt(activation, abort_step="requested", abort_reason=why)
    abort_effects(node_id, activation, why, previous)
    update_receipt(activation, phase="aborted", outcome="aborted", exited_at=util.iso(), exit_code=None,
                   abort_reason=why, abort_step=None)


def finish_aborts() -> None:
    """Complete every abort request an interrupted process left open (receipt `abort_step: requested`)."""
    base = state.root() / "activations"
    if not base.is_dir():
        return
    for path in sorted(base.glob("act-*.json")):
        data = util.read_json(path, {}) or {}
        if isinstance(data, dict) and data.get("abort_step") and data.get("node") and data.get("activation"):
            with util.locked():
                current = receipt(data["activation"])
                if current.get("abort_step"):
                    abort_activation(current["node"], current["activation"], current.get("abort_reason") or "aborted")


def abort_error(node_id: str, activation: str, why: str) -> YggError:
    message = "launch of " + node_id + " aborted (" + why + "); activation " + activation + " is marked aborted"
    if why in ("stopped", "paused"):
        return YggError(util.EXIT_PAUSED, "paused: " + message)
    return util.refuse(message)


def launch(node_id: str, *, reason: str = "launch", retries_used: int = 0, explicit: bool = True,
           expected_activation: str | None = None) -> dict:
    """Reserve, record, start and confirm one activation.

    Sequence (each step durable before the next, so a crash leaves a state reconcile can read):
    1. under one lock: checks, allocate the id, write a `reserved` receipt, then point the runtime at it;
    2. prepare the working directory and hooks (failure marks the receipt `aborted` and restores the runtime);
    3. under the lock, re-check stop and pause, record the prepared receipt and start the supervisor, which
       re-checks again under the lock right before the agent process starts;
    4. confirm: receipt and runtime outcomes are written together, then the journal is updated.
    """
    nodes = state.all_nodes()
    node = state.require_node(node_id, nodes)
    if node["role"] == "l1":
        raise util.usage("l1 is not launched by Yggdrasil")
    check_launch_env()
    model, effort, cli = model_for(node)
    require_kind(node)  # a programme that can never launch says so, whatever the agreement (frozen A1 AC-65)
    from . import permissions, t3
    # The user's agreement, before anything is reserved and whichever route the launch takes:
    # a T3 session gets the same agreed rules, in the settings file T3 loads.
    try:
        permissions.check(cli, node)
    except YggError:
        if not explicit or reason == "relaunch":
            permissions.alert_needed(node)  # the conductor's launch waits on the user's agreement
        raise
    check_agent_env()
    use_t3 = t3.choose(cli)  # A5.3: versions read before anything is reserved; the fallback is logged
    with util.locked():
        if live_activations(node_id):
            # A7.5 (I1): whatever the runtime says, a node with a live activation process is never launched
            # again; the user releases or resets it first.
            raise YggError(util.EXIT_REFUSED, node_id + " has a live activation; release or reset it first")
        from . import lifecycle, safety
        safety.check_admission()  # an established safety folder that is missing fails closed
        open_activation = lifecycle.open_activation(node_id)
        if open_activation is not None:
            # A reservation the journal still holds open (pending, running, uncertain or unknown) owns the
            # node, whatever local state says (after a wipe, too).
            raise util.refuse("launch precondition: " + node_id + " has an unsettled activation " + open_activation)
        held = lifecycle.ambiguous_activations([node_id])
        held_work = lifecycle.held_stop_work(node_id) if held else []
        if held and (not explicit or reason == "relaunch" or held_work):
            # A stopped session that may have finished owns its work until the user settles it: the agent
            # would see that work again in its inbox and rehydration document, so no launch may start the
            # node (I2, I5). Only an explicit launch of a node whose held stop was given nothing goes ahead.
            raise util.refuse("launch precondition: " + node_id + " has a held stop " + held[0] +
                              (" that owns " + ", ".join(held_work) if held_work else "") +
                              "; run `ygg node release " + node_id + " --as aborted|failed` first")
        readiness(node)
        for _, ref in RESERVE_CHECKS:
            function = _slot(ref)
            refusal = function(node) if function is not None else None
            if refusal:
                raise util.refuse(str(refusal))
        current = state.runtime(node_id)
        from . import decisions
        blocked = decisions.blocking(node_id, nodes)
        if blocked and node["role"] != "pm":
            raise util.refuse("launch precondition: " + node_id + " is blocked by " + ", ".join(blocked))
        if current.get("outcome") == "pending":
            raise util.refuse("launch precondition: " + node_id + " is already starting (activation " +
                              str(current.get("activation")) + ")")
        old = receipt(current.get("activation") or "") if current.get("activation") else {}
        if reason == "relaunch":
            if expected_activation is None or current.get("activation") != expected_activation or \
                    current.get("state") != "running" or _alive(old):
                raise util.refuse("launch precondition: relaunch of " + node_id + " no longer applies")
        elif current.get("state") == "running":
            if _alive(old) or not old.get("exited_at"):
                raise util.refuse("launch precondition: " + node_id + " is already running")
        from . import t3
        t3.refuse_if_closing(node, nodes)  # under this lock: never a reservation in a worktree being removed
        programme = tracker.programme(node["programme"])
        activation = tracker.allocate("activation")
        pinned = pinned_env()
        from . import lifecycle
        me = lifecycle.own_identity()
        # What this activation is given, fixed at reservation: every unacknowledged message, and the owed
        # events and due checkpoint reports. It is consumed once the activation is spawned and does not end
        # aborted; while it runs it is reserved, so no second launch is owed for it.
        owed = reducer.owed(node_id)
        backoff = [str(i["backoff_until"]) for i in owed if i["kind"] == "capacity" and i.get("backoff_until")
                   and util.parse_time(str(i["backoff_until"])) > util.now()]
        if backoff:
            # A capacity backoff holds every launch of the node, an explicit one included (checked under the
            # launch lock, before anything is reserved).
            raise YggError(util.EXIT_ADMISSION, "capacity: " + node_id + " may launch again at " + max(backoff))
        # The work-ahead gate (a team lead only): work about a later-stage package that may not be worked on
        # now is withheld from the work set, so it is never consumed and stays owed. A liveness relaunch is
        # screened with its activation's packages; a woken launch or relaunch whose owed work is all
        # withheld is held (the next read shows the held reason). An explicit launch by the user still
        # starts, with nothing withheld given to it.
        from . import workahead
        probe = [{"kind": "retry", "id": str(expected_activation), "reason": "relaunch", "probe": True}] \
            if reason == "relaunch" and expected_activation else []
        screened = workahead.screen(node_id, owed + probe, [m for m in state.inbox(node_id) if not m.get("acked_at")],
                                    nodes)
        if screened.blocks and not (explicit and reason != "relaunch"):
            raise util.refuse("launch precondition: " + node_id + " is held: " + str(screened.reason))
        given = [i for i in screened.items if not i.get("probe")]
        held_retry = retry_hold_reason(node, nodes, given)
        if held_retry:
            # Nothing left but a retry whose package can't continue: held visibly, never spent on another package.
            raise util.refuse("launch precondition: " + node_id + " is held: " + held_retry)
        testing_package = owed_testing_package(node, nodes, given)
        if testing_package is None and node["role"] == "su":
            # No owed item names a package: the oldest ready-for-test one is tested. It is pinned here, before the
            # reservation, so the journal fact names it and a cancel settles this activation's retry (A11.1 AC-06).
            fallback = _testing_target(node, nodes)
            testing_package = fallback["package"] if fallback else None
        given = one_piece_of_work(node, nodes, given, testing_package)
        # A launch resuming a retry or a capacity backoff continues that chain's retry count. A withheld
        # retry is not given, but its count still carries, so screening never resets a chain's spending.
        retries_used = max([int(retries_used or 0)] + [int(i.get("retries_used") or 0) for i in owed
                                                        if i["kind"] in ("retry", "capacity")])
        work = reducer.work_set(given, screened.messages)
        review = review_for(node, work)
        if review:
            # One review per activation (AC-94 row 3): items about another review, or about another package (a
            # review's stamp is on its work package), stay owed for the next activation.
            kept = one_review(given, review)
            if kept != given:
                given = kept
                work = reducer.work_set(given, screened.messages)
        # The reservation is a journal fact before anything local points at it (its launcher's identity
        # lets recovery tell a dead reservation from one still being started). It names the packages its
        # work is about, so a retry of it continues the same package purpose.
        purpose = {"packages": screened.packages} if screened.packages else {}
        if node["role"] == "tl":
            # A team lead's selection is always recorded, empty or not: its context must never hide what
            # this work set gives it, nor show what was withheld from it (rehydrate, inbox listing).
            purpose = {"packages": screened.packages, "blocked": screened.blocked}
        elif not purpose and node.get("project"):
            # Every other role: the packages its work set is about, in the journal fact itself, so a retry of it
            # carries them and a cancel settles it (A11.1 AC-06); cancelled ones are left out (the cancel module's).
            from . import cancel
            derived = cancel.work_packages(node, given, work, testing_package)
            if derived:
                purpose = {"packages": derived}
        lifecycle.commit(node_id, activation, "reserved", launcher_pid=me["pid"],
                         launcher_started_at=me["started_at"], work=work, retries_used=int(retries_used or 0),
                         **purpose)
        save_receipt(activation, {
            "node": node_id, "activation": activation, "launcher": "t3" if use_t3 else "headless",
            "phase": "reserved",
            "lifecycle": True, "launcher_pid": me["pid"], "launcher_started_at": me["started_at"],
            "consumed": dict(work),
            "pid": None, "process_started_at": None, "reserved_at": util.iso(), "started_at": util.iso(),
            "outcome": "pending", "retries_used": retries_used, "reason": reason, "model": model,
            "effort": effort, "cli": cli, "programme": node.get("programme"), "project": node.get("project"),
            # `review` names the review (v1.1 AC-03 reads its id); `review_target` keeps the reviewed range.
            "role": node["role"], "pinned": pinned, "agent_tz": agent_tz(),
            "review": review.get("id") if isinstance(review, dict) else review, "review_target": review,
            "testing_package": testing_package,
            **second_confirm_window(),
            **workahead.launch_fields(node, screened.ahead),
        })
        previous = dict(current)
        state.save_runtime(node_id, dict(current, state="running", activation=activation,
                                         launcher="t3" if use_t3 else "headless",
                                         outcome="pending", retries_used=retries_used, launched_at=util.iso()))
    is_git = programme["kind"] in ("git-repo", "shared-tree")
    try:
        try:
            util.ensure_dir(state.root() / "launch")  # durable before any settings file is written in it
        except OSError as error:
            raise util.refuse("launch precondition: hook injection failed: " + str(error))
        if not os.access(state.root() / "launch", os.W_OK):
            raise util.refuse("launch precondition: hook injection failed: " + str(state.root() / "launch") +
                              " is not writable")
        if node["role"] == "su" and testing_package and pinned_testing_target(node, nodes, testing_package) is None:
            raise util.refuse(_LEFT_TESTING.format(testing_package))  # reopened since the reservation
        extra = dict({"review": review} if review else {}, **({"testing_package": testing_package}
                                                               if testing_package else {}))
        cwd = working_directory(node, nodes, **extra)
        # A T3 session is told to read its private prompt file first (t3.prompt_file): exactly that file.
        prompt = [state.root() / "launch" / activation / "prompt.md"] if use_t3 else None
        grant = permissions.block(node, nodes, cwd, is_git, prompt, activation) if cli == "claude-code" else None
        settings = inject_hooks(activation, cwd, is_git, tracker.programme_home(programme), grant)
        route, t3_project = None, None
        if use_t3:
            # A5.1: the lane worktree is its own T3 project with the guard hook in its settings file; the
            # route (a new thread, or the node's thread) is read before the gate.
            t3.prepare_worktree(cwd, node, activation, pinned, home=tracker.programme_home(programme), permissions=grant)
            t3_project = t3.ensure_project(cwd, str(node.get("project") or node.get("programme") or "") + "/" + node_id,
                                           node)
            route = t3.plan(node_id)
        from . import rehydrate
        rehydrate.write_document(node_id)
    except BaseException:
        with util.locked():
            commit_aborted(node_id, activation, "preparation failed")
            update_receipt(activation, phase="aborted", outcome="aborted", exited_at=util.iso(), exit_code=None)
            runtime = state.runtime(node_id)
            if runtime.get("activation") == activation:
                state.save_runtime(node_id, previous)
        raise
    testing = pinned_testing_target(node, nodes, testing_package) if testing_package else \
        _testing_target(node, nodes)
    if testing:
        testing["checkout"] = str(cwd)
    with util.locked():
        why = hold_reason(node, activation)
        if not why and node["role"] == "su" and testing_package:
            # Read again under the lock, right before the activation is marked starting: a reopen after the
            # metadata above was computed still aborts this launch.
            testing = pinned_testing_target(node, state.all_nodes(), testing_package)
            if testing is None:
                why = _LEFT_TESTING.format(testing_package)[len("launch precondition: "):]
            else:
                testing["checkout"] = str(cwd)
        if why:
            abort_activation(node_id, activation, why, previous)
            raise abort_error(node_id, activation, why)
        # The lane's git directories are recorded for T3 sessions too: the guard binds their git reads to them.
        update_receipt(activation, phase="starting", cwd=str(cwd), settings=str(settings), testing=testing,
                       project_settings=project_settings(),
                       **dict(zip(("git_dir", "git_common_dir"), lane_git_dirs(cwd) if is_git else (None, None))))
    if use_t3:
        return t3.launch(node, activation, cwd, model, previous, route, t3_project)
    command = [sys.executable, "-m", "yggdrasil", "_supervise", activation]
    faults.boundary("before-spawn")  # A7.2: reserved, agent not started
    # The supervisor's own errors go to its launch folder (owner-only, appended), so one that stops before
    # `spawned` leaves its reason (pilot 2, 20 Nov). Unopenable, they are dropped as before.
    try:
        errors = os.open(state.root() / "launch" / activation / "supervisor.err",
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError:
        errors = subprocess.DEVNULL
    try:
        # The supervisor runs in its own launch folder, never in the (possibly managed) worktree: inside a
        # worktree marked `.ygg-managed` every ygg command counts as a managed session (A5.1). The agent
        # still runs in the recorded working directory.
        supervisor = subprocess.Popen(command, cwd=str(state.root() / "launch" / activation),
                                      env=faults.child_env(dict(os.environ, **pinned, YGG_SUPERVISOR_FOR=activation)),
                                      stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True)
    except OSError as error:
        launch_returned(activation)  # no supervisor was created
        mark_uncertain(node_id, activation, "supervisor spawn failed: " + str(error))
        raise YggError(util.EXIT_UNCERTAIN, "launch outcome uncertain for " + node_id + ": " + str(error))
    finally:
        if errors != subprocess.DEVNULL:
            os.close(errors)  # the supervisor holds its own copy
    # The supervisor's identity is durable before the launch step can be recorded as returned: from then on it, not
    # this launcher, is what may still start the agent (lifecycle.unspawned_evidence). A failed write leaves no
    # return mark, so this launcher's own evidence still counts.
    started = util.process_start(supervisor.pid)  # read before the leaf lock
    with util.locked(util.RECEIPT_LOCK):
        record = receipt(activation)
        if record.get("supervisor_pid") is None:  # never over the supervisor's own record of itself
            record.update(supervisor_pid=supervisor.pid, supervisor_started_at=started)
            save_receipt(activation, record, scope={})
    try:
        result = confirm(node_id, activation, supervisor)
    finally:
        launch_returned(activation)
    if result["outcome"] == "aborted":
        raise abort_error(node_id, activation, receipt(activation).get("abort_reason") or "stopped")
    # A start is a process that was created; a failed process creation is not a launched session.
    result["started"] = receipt(activation).get("pid") is not None
    return result


def launch_returned(activation: str) -> None:
    """Record that the launch step of a headless activation has returned, so a long-running launcher (the
    conductor) no longer counts as the process that may start its agent. Best effort: with no mark, the
    launcher's own evidence still counts (fail closed)."""
    try:
        scope = _scope_for(receipt(activation))  # resolved before the leaf lock (tracker read)
        with util.locked(util.RECEIPT_LOCK):
            record = receipt(activation)
            if record.get("launch_returned_at") is None:  # write-once: the first return stands
                record["launch_returned_at"] = util.iso()
                save_receipt(activation, record, scope=scope)
    except (OSError, YggError):
        pass


def live_activations(node_id: str) -> list[dict]:
    """Every activation record of the node with a live process: an integer pid that is alive with a
    matching process start time, and no exit record (A7.1 I1)."""
    found = []
    base = state.root() / "activations"
    if not base.is_dir():
        return found
    for path in sorted(base.glob("act-*.json")):
        data = util.read_json(path, {}) or {}
        if isinstance(data, dict) and data.get("node") == node_id and isinstance(data.get("pid"), int) and \
                not isinstance(data.get("pid"), bool) and not data.get("exited_at") and _alive(data):
            found.append(data)
    return found


def _alive(data: dict) -> bool:
    pid = data.get("pid")
    if not util.pid_alive(pid):
        return False
    started = data.get("process_start_identity") or data.get("process_started_at")
    return started is None or util.same_start(pid, started)


def classify(data: dict) -> str | None:
    """The launch outcome a receipt shows, or None while it shows neither a process nor an exit."""
    if data.get("exited_at") is not None:
        if data.get("phase") == "aborted":
            return "aborted"
        code = data.get("exit_code")
        if code == 0:
            return "confirmed"
        if capacity_signal(data.get("stderr_tail") or ""):
            return "capacity"
        return "failed"
    if data.get("pid") is not None:
        return "confirmed" if _alive(data) else "failed"
    return None


def receipt_outcome(data: dict) -> str:
    """The classified outcome of a receipt already recorded as confirmed (its class included).

    A failure comes only from an exit record: a process that is gone without one is `uncertain`.
    """
    if data.get("class") == "capacity":
        return "capacity"
    if data.get("exited_at") is not None:
        return "confirmed" if data.get("exit_code") == 0 else "failed"
    return "confirmed" if _alive(data) else "uncertain"


EXIT_VERDICTS = ("confirmed", "failed", "capacity", "aborted")
UNIDENTIFIED = "the agent's process was seen but its identity could not be read; held until it exits"


def _valid_exit_record(data: dict) -> bool:
    """An exit record that can be read: `exited_at` is a time, and `exit_code` is a whole number (never a
    bool), or the record is one of the explicit ends written without a code (an abort, a release, a reset)."""
    try:
        util.parse_time(str(data.get("exited_at")))
    except (TypeError, ValueError):
        return False
    code = data.get("exit_code")
    if isinstance(code, int) and not isinstance(code, bool):
        return True
    if code is not None:
        return False
    if data.get("phase") == "aborted" or data.get("released_as") in ("failed", "aborted"):
        return True
    try:
        util.parse_time(str(data.get("reset_at")))
    except (TypeError, ValueError):
        return False
    return data.get("reset_at") is not None


def start_evidence(data: dict, *, seen_dead: bool = False) -> str:
    """What a receipt proves about a start, for the confirm and hold decisions only. A start is confirmed only
    by a valid exit record, or by a live process whose recorded start identity matches; a live pid without a
    recorded identity proves nothing. Once the agent has been seen gone (`seen_dead`) only an exit record counts.

    Returns an exit record's verdict (`confirmed`, `failed`, `capacity`, `aborted`), `malformed` for an exit
    record that can't be read (never a success), `alive` for a live process with its recorded identity, `dead`
    for a recorded process that is gone or whose pid now belongs to another process, or `unknown` (no pid yet,
    no recorded identity, or nothing more to go on after a death). `_alive` is left as it is for its other
    callers, which must still treat a pid of unknown identity as possibly alive."""
    if data.get("exited_at") is not None:
        if not _valid_exit_record(data):
            return "malformed"
        if data.get("phase") == "aborted" or data.get("released_as") == "aborted":
            return "aborted"
        if data.get("class") == "capacity":
            return "capacity"
        return "confirmed" if data.get("exit_code") == 0 else "failed"
    pid = data.get("pid")
    if pid is None:
        return "unknown"
    if seen_dead:
        return "dead"
    if not util.pid_alive(pid):
        return "dead"
    started = data.get("process_start_identity") or data.get("process_started_at")
    if not started:
        return "unknown"  # a live pid of unknown identity may be another process: not proof
    return "alive" if util.same_start(pid, started) else "dead"


def hold_uncertain(node_id: str, activation: str, why: str, *, seen_dead: bool = False) -> str:
    """The one uncertain-outcome path: receipt, runtime and journal entry marked together, one alert.
    Every branch that reports `uncertain` comes through here.

    Under the transition lock it re-reads the receipt first and never overwrites a terminal record: an
    aborted activation reports `aborted`, and a confirmed one reports its outcome only when `start_evidence`
    proves it (a valid exit record, or a live process with its recorded identity). Anything else is held: a
    pending receipt, a confirmed one whose process is gone or can't be identified, and any receipt whose exit
    record can't be read. A caller that has seen the agent's process gone passes `seen_dead`: from then on
    only an exit record proves anything. The return value is always what the receipt says afterwards.
    """
    with util.locked():
        data = receipt(activation)
        if data.get("outcome") == "aborted" or data.get("phase") == "aborted":
            return "aborted"
        evidence = start_evidence(data, seen_dead=seen_dead)
        if data.get("outcome") == "confirmed" and evidence in ("confirmed", "failed", "capacity", "alive"):
            return "confirmed" if evidence == "alive" else evidence
        if data.get("outcome") == "pending" and evidence in EXIT_VERDICTS:
            return "pending"  # a valid exit record arrived: the caller classifies it, nothing is held
        mark_uncertain(node_id, activation, why, allow_confirmed=True)
        return str(receipt(activation).get("outcome") or "uncertain")


def capacity_backoff_until(exited_at: str | None = None) -> str:
    """When a capacity-held node may be tried again: its exit plus `capacity_backoff_seconds` (default
    300). The deadline is part of the `exited` fact, so it never depends on when the exit was classified."""
    from datetime import timedelta
    seconds = float(config.get("capacity_backoff_seconds", 300))
    try:
        moment = util.parse_time(exited_at) if exited_at else util.now()
    except ValueError:
        moment = util.now()
    return util.iso(moment + timedelta(seconds=max(seconds, 1.0)))


def exit_fields(record: dict, activation: str | None = None) -> dict:
    """The `exited` fact's class fields for an exit record: a capacity exit carries its backoff deadline,
    and an exit by a signal is an interruption, which consumes nothing the activation was given. An exit after a
    stop or reset signal may have reached the agent, without the agent dying of it (Claude Code exits 0 on
    SIGINT), is ambiguous: it may have finished or been cut short, so it is held for the user and never
    replayed on its own. That covers a delivered signal and one whose delivery is unresolved (the intent is
    recorded, the kill call's result is not). Only a signal proven not delivered (the kill call failed)
    leaves the exit judged as usual."""
    from . import lifecycle
    code = record.get("exit_code")
    # The caller's own activation id wins over the record: a record rebuilt after a wipe may lack it, and the
    # signal record lives in the safety folder, which survives the wipe.
    key = activation or record.get("activation")
    signalled = bool(key) and lifecycle.signal_state(str(key)) in ("intent", "sent")
    interrupted = isinstance(code, int) and code < 0
    ambiguous = not interrupted and isinstance(code, int) and signalled
    fields = {"capacity": False}
    if code not in (0, None) and not (interrupted or ambiguous) and classify(record) == "capacity":
        fields = {"capacity": True, "backoff_until": capacity_backoff_until(record.get("exited_at"))}
    if interrupted:
        fields["interrupted"] = True  # a signal death consumes nothing (A11 AC-29)
        if not signalled:
            # A signal Yggdrasil didn't send (a stop or a reset kill are recorded first): a crash, which counts as
            # a try against retry_limit (A11.1 AC-01, AC-02). A sent one never counts.
            fields["crashed"] = True
    if ambiguous:
        fields["ambiguous"] = True
    return fields


def apply_outcome(node_id: str, activation: str, outcome: str) -> None:
    """Apply a recorded outcome to the runtime (caller holds the lock). Idempotent: finalize calls it after
    recording the receipt, and reconciliation calls it again when the process stopped between the receipt
    write and the runtime write. A capacity outcome leaves the node idle: its hold and its relaunch after
    the backoff are derived from the `exited` fact. An `uncertain` outcome (a process gone without an exit
    record) holds the activation and alerts once.
    """
    if outcome == "uncertain":
        hold_uncertain(node_id, activation, "the process is gone and no exit was recorded")
        return
    data = receipt(activation)
    if data.get("work_ahead_packages") and not data.get("spawn_failed") and outcome in ("confirmed", "failed",
                                                                                          "capacity"):
        # Every actual work-ahead process start is visible in the morning go (idempotent per activation).
        from . import workahead
        workahead.record_launch(activation)
    runtime = state.runtime(node_id)
    if runtime.get("activation") != activation:
        return
    data = receipt(activation)
    if outcome == "capacity":
        if runtime.get("state") != "stopped":
            runtime["state"] = "idle"
        runtime["outcome"] = "confirmed"
        state.save_runtime(node_id, runtime)
        return
    # Nothing the activation was given is cleared here: confirmation is not a consuming end (the session may
    # still be stopped). Consumed facts are compacted later from the terminal facts (upgrade.compact_journal).
    runtime["outcome"] = "confirmed"
    if runtime.get("state") != "stopped":
        exited_ok = data.get("exited_at") is not None and data.get("exit_code") == 0
        # A live process stays running; a dead one takes the liveness path (running, no process).
        runtime["state"] = "idle" if exited_ok else "running"
    state.save_runtime(node_id, runtime)


def finalize(node_id: str, activation: str, outcome: str, *, resolving=("pending",)) -> str:
    """Record a classified outcome for an activation whose receipt outcome is still in `resolving`.

    Shared by confirm (pending) and reconciliation (pending after a conductor exit, or uncertain). The
    journal is written before local state. Returns the outcome now on record; a receipt that was already
    resolved by another process is left untouched.
    """
    with util.locked():
        data = receipt(activation)
        if data.get("outcome") not in resolving:
            recorded = data.get("outcome")
            if recorded == "confirmed":
                result = receipt_outcome(data)
                if result == "uncertain":
                    return hold_uncertain(node_id, activation, "the process is gone and no exit was recorded")
                return result
            return str(recorded)
        if start_evidence(data) == "malformed":
            # An exit record that can't be read proves nothing: held, never turned into a failure and a retry.
            return hold_uncertain(node_id, activation, "the exit record can't be read")
        fresh = classify(data)
        if fresh is not None and fresh != "aborted" and (data.get("exited_at") is not None or outcome == "failed"):
            outcome = fresh  # the receipt as it is now, under the lock: a recorded exit keeps its class
        if outcome == "failed" and data.get("exited_at") is None:
            # A failure is only ever accepted from the supervisor's exit record. A process that is gone
            # without one (after the startup grace) is held as uncertain and alerted once, never pending.
            return hold_uncertain(node_id, activation, "the process is gone and no exit was recorded")
        data["outcome"] = "confirmed"
        data["phase"] = "launched"
        if data.get("reconciled_at") is None and resolving != ("pending",):
            data["reconciled_at"] = util.iso()
        if outcome == "capacity":
            data["class"] = "capacity"
        save_receipt(activation, data)
        apply_outcome(node_id, activation, outcome)
    return outcome


def second_confirm_window() -> dict:
    """The second confirm window, recorded in the receipt at reservation (A11 AC-22, AC-77; switch
    `confirm_window`): it ends `60 x max(1, L)` seconds after reservation, at most 300 s, where L is the host
    load per CPU at that moment. Switched off, nothing is recorded and only the fixed first window applies."""
    from . import conductor, release
    if not release.on("confirm_window"):
        return {}
    value, _ = conductor.load_sample()
    scale = max(1.0, value) if value is not None else 1.0
    return {"confirm_window_seconds": min(300.0, 60.0 * scale)}


def confirm(node_id: str, activation: str, supervisor=None) -> dict:
    deadline = time.monotonic() + float(config.get("launch_confirm_seconds", 5))
    grace = deadline + 10.0
    while True:
        data = receipt(activation)
        if data.get("exited_at") is not None:
            break
        now = time.monotonic()
        if now >= deadline:
            # A live supervisor that has not reported the agent's pid yet is still starting it: wait a little.
            if data.get("pid") is not None or supervisor is None or supervisor.poll() is not None or now >= grace:
                break
        time.sleep(0.05)
    data = receipt(activation)
    until = None  # set once, when the agent is first seen gone: every wait for its exit record shares it
    seen_dead = start_evidence(data) == "dead"  # recorded whether or not there is a supervisor
    if seen_dead and supervisor is not None:
        # The process is gone but its supervisor has not recorded the exit yet: wait for that record, so a
        # success or capacity exit is classified from it rather than as a failure.
        until = _exit_record_deadline()
        _wait_for_exit_record(activation, supervisor, until)
    for look in (1, 2):
        # Decide under the transition lock, from the receipt as it is now: a concurrent resolution (an exit
        # record, reconciliation or an abort) is reported as recorded, never overwritten.
        with util.locked():
            decided = _confirm_decision(node_id, activation, supervisor, look, seen_dead or look == 2)
        if decided is not None:
            return decided
        # The agent exited while this step waited for the lock, and its supervisor, still running, writes the
        # exit record under that lock: wait for the record outside the lock, then look once more.
        until = until if until is not None else _exit_record_deadline()
        _wait_for_exit_record(activation, supervisor, until)
    raise AssertionError("unreachable: the second look always decides")


def _exit_record_deadline() -> float:
    """The end of the one grace confirm gives an agent's exit record, counted from when the agent is first seen
    gone: max(10 s, launch_confirm_seconds) in total, however many waits share it."""
    return time.monotonic() + max(10.0, float(config.get("launch_confirm_seconds", 5)))


def _wait_for_exit_record(activation: str, supervisor, until: float) -> None:
    """Wait until `until` (the shared deadline) for the supervisor to record the agent's exit; stop at once when
    the record is there or the supervisor has ended without writing it."""
    while time.monotonic() < until and receipt(activation).get("exited_at") is None and supervisor.poll() is None:
        time.sleep(0.05)


def _exit_record_may_come(data: dict, supervisor) -> bool:
    """Whether an agent that is gone without an exit record may still get one: its supervisor, which writes the
    record under the transition lock, is still running. Only with the confirm window off; with it on, nothing
    here changes."""
    return not data.get("confirm_window_seconds") and data.get("pid") is not None and \
        data.get("exited_at") is None and supervisor is not None and supervisor.poll() is None


def _confirm_decision_window_on(node_id: str, activation: str, data: dict, supervisor) -> dict:
    """With the confirm window on, the decision exactly as before this change (A11 AC-22, the O-2 path): the
    conductor judges a start left pending by its recorded second window. Only the hold decisions it calls
    follow the stricter evidence rule."""
    if data.get("outcome") == "confirmed":
        recorded = receipt_outcome(data)
        if recorded == "uncertain":
            recorded = hold_uncertain(node_id, activation, "the process is gone and no exit was recorded")
        return {"node": node_id, "activation": activation, "outcome": recorded}
    if data.get("outcome") == "uncertain":
        return {"node": node_id, "activation": activation,
                "outcome": hold_uncertain(node_id, activation, "no process reported within the confirm window")}
    outcome = classify(data)
    if outcome == "failed" and data.get("exited_at") is None:
        outcome = None  # no exit record yet: success, capacity or failure cannot be told
    if outcome is None and supervisor is not None and supervisor.poll() is None:
        # Its supervisor is still starting the agent: the conductor judges the recorded second window
        # (A11 AC-22), so a slow start is not held here. A supervisor that has already exited without
        # reporting is held at once: its gate write may or may not have landed, so it is not a proven failure to
        # abort (`test_held_release`, `test_linear_gate_conflict`). The decision is recorded here, under the
        # transition lock, in a field of its own: recovery treats a start as deliberately pending only when this
        # field is set, never because the launch step merely returned (a confirm step that raised, or was
        # interrupted, also ends in `launch_returned`, and that start is aborted).
        update_receipt(activation, confirm_left_pending_at=util.iso())
        return {"node": node_id, "activation": activation, "outcome": "pending"}
    if outcome is None:
        return {"node": node_id, "activation": activation,
                "outcome": hold_uncertain(node_id, activation, "no process reported within the confirm window")}
    outcome = finalize(node_id, activation, outcome)
    return {"node": node_id, "activation": activation, "outcome": outcome}


def _confirm_decision(node_id: str, activation: str, supervisor, look: int, seen_dead: bool) -> dict | None:
    """The confirm step's decision, made under the transition lock, from `start_evidence`: a start is confirmed
    only by a valid exit record or by a live process with its recorded identity. None (first look only) when the
    agent's process is gone, its exit is not recorded yet and its supervisor is still running to record it: the
    caller waits for that record outside the lock and looks again. Once the agent has been seen gone
    (`seen_dead`: before the lock, or on the first look) only an exit record decides. Without proof the start
    is held as uncertain, never reported as a success and never relaunched; an exit record that can't be read
    is held too."""
    data = receipt(activation)
    if data.get("outcome") == "aborted" or data.get("phase") == "aborted":
        return {"node": node_id, "activation": activation, "outcome": "aborted"}
    if data.get("confirm_window_seconds"):
        return _confirm_decision_window_on(node_id, activation, data, supervisor)
    evidence = start_evidence(data, seen_dead=seen_dead)

    def held(why: str) -> dict:
        # A death seen here or earlier is passed on: the hold path never probes its way back to a success.
        return {"node": node_id, "activation": activation,
                "outcome": hold_uncertain(node_id, activation, why, seen_dead=seen_dead or evidence == "dead")}
    if data.get("outcome") == "confirmed":
        if evidence in ("confirmed", "failed", "capacity"):
            return {"node": node_id, "activation": activation, "outcome": evidence}
        if evidence == "alive":
            return {"node": node_id, "activation": activation, "outcome": "confirmed"}
        if evidence == "dead" and not seen_dead and _exit_record_may_come(data, supervisor):
            return None  # its exit record may still come: the caller waits for it outside the lock
        if evidence == "unknown" and data.get("pid") is not None:
            return held(UNIDENTIFIED)
        return held("the process is gone and no exit was recorded")
    if data.get("outcome") == "uncertain":
        return held("no process reported within the confirm window")
    if evidence == "malformed":
        return held("the exit record can't be read")
    outcome = classify(data) if evidence in EXIT_VERDICTS else "confirmed" if evidence == "alive" else None
    if outcome is None and evidence == "dead" and not seen_dead and _exit_record_may_come(data, supervisor):
        return None  # its exit record may still come: the caller waits for it outside the lock
    if outcome is None and evidence == "unknown" and data.get("pid") is not None:
        return held(UNIDENTIFIED)
    if outcome is None:
        return held("no process reported within the confirm window")
    outcome = finalize(node_id, activation, outcome)
    return {"node": node_id, "activation": activation, "outcome": outcome}


def pinned_env() -> dict:
    """Configuration and roots as resolved by the launching (user or conductor) process."""
    pinned = {"YGG_STATE_ROOT": str(state.root().resolve()), "YGG_FILES_ROOT": str(tracker.root().resolve()),
              # The user's files root, where the agents agreement lives. On the Linear tracker YGG_FILES_ROOT is
              # the cache, so without this an agent's own view showed every waiting node as not agreed (pilot 2
              # run 4). Read-only: it is never added as a writable folder.
              "YGG_AGREEMENT_ROOT": str(util.files_root().resolve())}
    profile = util.env("YGG_PROFILE")
    if profile:
        # Absolute (sessions run in another folder) with no normalisation at all: no symlink is followed and
        # no `..` is collapsed, so the path names the same file, and relative profile settings resolve the
        # same way, as in the launching process.
        path = os.path.expanduser(profile)
        pinned["YGG_PROFILE"] = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
    user_config = config.user_config_path()
    pinned["YGG_CONFIG"] = str(user_config) if user_config is not None else config.NO_CONFIG
    return pinned


def _programme(node_id: str):
    return (state.all_nodes().get(node_id) or {}).get("programme")


# The agent's environment is built from an allowlist: the launching user's other credentials and Yggdrasil's
# test controls never reach an agent. The supervisor keeps the launcher's full environment.
AGENT_ENV_NAMES = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP", "LANG", "LANGUAGE", "TERM",
    "COLORTERM", "NO_COLOR", "SSH_AUTH_SOCK", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy",
    "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
    "__CF_USER_TEXT_ENCODING",
}
AGENT_ENV_PREFIXES = ("LC_", "YGG_")
# Credentials an agent needs for the command-line tool it runs, and nothing else.
CLI_ENV_NAMES = {
    "claude-code": {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
                    "CLAUDE_CONFIG_DIR"},
    "codex": {"OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME"},
}
# Yggdrasil variables that never pass to an agent: test controls and tracker credentials.
# YGG_SUPERVISOR_FOR names the supervisor's own activation (reset verifies it); the agent never carries it.
AGENT_ENV_DENIED = {"YGG_TEST_MODE", "YGG_NOW", "YGG_SUPERVISOR_FOR"}
# Assigned by Yggdrasil for every agent; never inherited and never set by a launch overlay.
IDENTITY_ENV = ("YGG_NODE", "YGG_ACTIVATION", "YGG_PROGRAMME", "YGG_PROJECT", "YGG_MODEL", "YGG_EFFORT")


def check_launch_env(profile: dict | None = None) -> None:
    """Refuse a launch overlay that sets any YGG_ variable (identity, test controls, paths)."""
    loaded = config.profile() if profile is None else profile
    for key in ((loaded or {}).get("launch_env") or {}):
        if str(key).startswith("YGG_"):
            raise util.usage("launch_env may not set " + str(key))


PASSTHROUGH_DENIED = re.compile(r"^(PATH|GIT_.*|YGG_.*)$")


def passthrough_names() -> list[str]:
    names = config.get("agent_env_passthrough") or []
    return [str(n) for n in names] if isinstance(names, list) else []


def check_agent_env() -> None:
    """Refuse, before anything is reserved, an agent environment Yggdrasil can't set safely (AC-82): an unknown
    profile timezone, a TZ in launch_env (the profile's timezone is the one place for it), and a pass-through of
    PATH or of GIT_ or YGG_ names, which would let the conductor's environment steer the agent's git or
    identity."""
    profile = config.profile() or {}
    if "TZ" in (profile.get("launch_env") or {}):
        raise util.refuse("launch_env may not set TZ; use the profile's timezone")
    zone = profile.get("timezone")
    if zone is not None and not known_zone(zone):
        raise util.refuse("timezone '" + str(zone) + "' is not a known zone")
    for name in passthrough_names():
        if PASSTHROUGH_DENIED.match(name):
            raise util.refuse("agent_env_passthrough may not name " + name)


def known_zone(zone) -> bool:
    if not isinstance(zone, str) or not zone or zone.startswith("/") or ".." in zone:
        return False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(zone)
    except Exception:  # noqa: BLE001  (ZoneInfoNotFoundError, ValueError and friends all mean unknown)
        return False
    return True


def agent_tz() -> str | None:
    """The agent's TZ, fixed at reservation (AC-26, AC-81): a valid profile `timezone` first, then the launching
    process's TZ when `agent_env_passthrough` names TZ, otherwise none (TZ is removed)."""
    zone = (config.profile() or {}).get("timezone")
    if zone is not None and known_zone(zone):
        return str(zone)
    if "TZ" in passthrough_names():
        return os.environ.get("TZ")
    return None


def agent_claude_dir() -> Path | None:
    """Yggdrasil's own Claude Code folder for agents (AC-75), created private to the user."""
    configured = config.get("agent_claude_config_dir")
    if not configured:
        return None
    path = Path(os.path.expanduser(str(configured)))
    util.ensure_dir(path)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def child_env(data: dict) -> dict:
    passthrough = {n for n in passthrough_names() if n != "TZ" and not PASSTHROUGH_DENIED.match(n)}
    allowed = AGENT_ENV_NAMES | CLI_ENV_NAMES.get(str(data.get("cli") or ""), set()) | passthrough
    environment = {k: v for k, v in os.environ.items()
                   if (k in allowed or k.startswith(AGENT_ENV_PREFIXES))
                   and k not in AGENT_ENV_DENIED and not TOKEN_ENV.match(k) and k != "TZ"}
    if data.get("agent_tz"):
        environment["TZ"] = str(data["agent_tz"])  # fixed at reservation (AC-81 row 2)
    # Agent git never waits on a prompt (AC-96 row 1).
    environment["GIT_TERMINAL_PROMPT"] = "0"
    # Pinned once by the launching process and read from the receipt; never recomputed after chdir.
    environment.update({k: str(v) for k, v in (data.get("pinned") or {}).items()})
    testing = data.get("testing") or {}
    if testing.get("checkout"):
        environment["YGG_TESTING_CHECKOUT"] = str(testing["checkout"])
    if data.get("role") == "reviewer" and data.get("activation"):
        # The only folder a reviewer writes: its verdict goes to $YGG_REVIEW_DIR/verdict.json (AC-32 row 1).
        from . import permissions
        environment["YGG_REVIEW_DIR"] = str(util.ensure_dir(permissions.review_dir(str(data["activation"]))))
    profile = config.profile() or {}
    if profile.get("hub_base_url"):
        environment["ANTHROPIC_BASE_URL"] = str(profile["hub_base_url"])
    check_launch_env(profile)
    for key, value in (profile.get("launch_env") or {}).items():
        environment[str(key)] = str(value)
    if str(data.get("cli") or "claude-code") == "claude-code":
        # Agents get Yggdrasil's own Claude Code folder (instructions, hooks, skills), never the user's (AC-75);
        # they sign in with the credentials in their environment alone (smoke 2, 19 Nov).
        agent_config = agent_claude_dir()
        if agent_config is not None:
            environment["CLAUDE_CONFIG_DIR"] = str(agent_config)
    # Identity last, so nothing above can replace it.
    environment["YGG_NODE"] = data["node"]
    environment["YGG_ACTIVATION"] = data["activation"]
    environment["YGG_PROGRAMME"] = data.get("programme") or ""
    environment["YGG_PROJECT"] = data.get("project") or ""
    environment["YGG_MODEL"] = str(data.get("model") or "")
    environment["YGG_EFFORT"] = str(data.get("effort") or "")
    return environment


def lane_git_dirs(cwd) -> tuple[str | None, str | None]:
    """The git directory and the common git directory of a lane worktree as git selects them at launch (real
    paths), recorded in the receipt so the guard hook can refuse a git read that selects any other repository,
    or whose lane-local metadata (commondir, HEAD) was redirected."""
    found = git("-C", cwd, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    lines = [line.strip() for line in found.stdout.splitlines() if line.strip()]
    if found.returncode != 0 or len(lines) != 2:
        return None, None
    return os.path.realpath(lines[0]), os.path.realpath(lines[1])


def lane_git_dir(cwd) -> str | None:
    return lane_git_dirs(cwd)[0]


def project_settings() -> str:
    """Whether agents load their project's Claude Code settings: `exclude` (the default; ruling 129) or
    `include`, a super-user setting honoured only from the user's own layers."""
    return "include" if config.get("agent_project_settings") == "include" else "exclude"


def claude_command(data: dict, environment: dict) -> tuple[list[str], dict]:
    """The Claude Code command line and environment of an activation.

    By default the agent reads its project's instructions (CLAUDE.md) but not the project's settings
    (.claude/settings.json and settings.local.json: hooks and permission rules): `--setting-sources user` leaves
    them out, and the working folder is added back as a CLAUDE.md folder. Yggdrasil's own `--settings` (the guard
    and the agreed rules) applies either way. Checked against Claude Code 2.1.322."""
    argv = [util.env("YGG_CLAUDE_BIN", "claude"), "-p", "--settings", data["settings"], "--model", data["model"]]
    if data.get("effort"):
        argv += ["--effort", str(data["effort"])]  # A1.6 and A5.6: the effort is a Claude argument
    if (data.get("project_settings") or project_settings()) != "include":
        # The project's instructions load only with --add-dir plus the variable (smoke 2, 19 Nov).
        argv += ["--setting-sources", "user", "--add-dir", str(data["cwd"])]
        environment = dict(environment, CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD="1")
    else:
        argv += ["--setting-sources", "user,project,local"]
    return argv, environment


def codex_sandbox(data: dict, node: dict) -> list[str]:
    """Codex has no PreToolUse hook; it runs in its own sandbox (Threat model, stated gap).

    Editing roles get workspace-write on their checkout. Senior users and reviewers get workspace-write
    rooted in an empty scratch folder, so their checkout stays read-only, with only the state root and the
    files-tracker root added as writable (`--add-dir`, codex-cli 0.166) for their `ygg` writes.
    """
    pinned = data.get("pinned") or {}
    role = data.get("role") or node.get("role")
    if role in ("su", "reviewer"):
        scratch = state.root() / "scratch" / data["activation"]
        scratch.mkdir(parents=True, exist_ok=True)
        # The scratch root is not a Git repository; codex exec refuses one without this flag.
        args = ["--sandbox", "workspace-write", "--skip-git-repo-check", "--cd", str(scratch)]
        if role == "reviewer":
            # A reviewer writes only verdict.json into its scratch folder: no writable state or tracker folder
            # (AC-94 row 2).
            return args
        for root in (pinned.get("YGG_STATE_ROOT"), pinned.get("YGG_FILES_ROOT")):
            if root:
                args += ["--add-dir", str(root)]
        return args
    args = ["--sandbox", "workspace-write"]
    if not _inside_git(data.get("cwd")):
        args.append("--skip-git-repo-check")  # staging folders of non-Git work-product kinds
    return args


def _inside_git(path) -> bool:
    if not path:
        return False
    result = git("-C", path, "rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


# How often the supervisor's gate is tried when its journal write meets a newer journal in Linear that extends the
# cache (adopted, then checked again); a supervisor has no one to run it again.
GATE_ATTEMPTS = 3


def _safe_summary(text: str) -> str:
    """One line, with secret-looking text and the values of credential variables in this environment removed."""
    for key, value in os.environ.items():
        if value and len(value) >= 8 and (TOKEN_ENV.match(key) or util.SECRET_KEY_RE.search(key)):
            text = text.replace(value, "[REDACTED]")
    return " ".join(util.redact(text).split())


def supervise(activation: str) -> int:
    try:
        code = _supervise(activation)
    except Exception as error:
        # Standard error is the launch folder's supervisor.err (launch): a supervisor that stops leaves why, on one
        # line with secrets removed. Not raised again, so the command line never prints the raw message there.
        print(util.iso() + " supervisor of " + activation + " stopped: " + type(error).__name__ + ": " +
              _safe_summary(str(getattr(error, "message", error))), file=sys.stderr, flush=True)
        return error.code if isinstance(error, YggError) else 1
    faults.supervisor_done(code)
    return code


def _supervise(activation: str) -> int:
    data = receipt(activation)
    if not data:
        return 1
    nodes = state.all_nodes()
    node = nodes.get(data["node"]) or {"id": data["node"], "role": "worker"}
    cli = data.get("cli") or ("codex" if node.get("role") == "reviewer" else "claude-code")
    effort = str(data.get("effort") or "")
    prompt = prompt_for(node, activation)
    testing = data.get("testing")
    if testing:
        checkout = str(testing.get("checkout") or data.get("cwd"))
        prompt += ("\n\nYour read-only testing checkout is " + checkout + " (also in $YGG_TESTING_CHECKOUT): "
                   "the lane head of " + str(testing.get("branch")) + " for package " + str(testing.get("package")) +
                   ". Run Git and test commands there, for example " +
                   # Claude Code works in the checkout and its guard allows no `git -C`; Codex works in a scratch folder.
                   ("`git -C " + checkout + " rev-parse HEAD`" if cli == "codex" else
                    "`git rev-parse HEAD` from your working folder, which is that checkout") +
                   ", and record acceptance against that sha. Do not edit files in it." +
                   # Pilot 2 run 3: read as background, the senior user waited to be asked and the project stalled.
                   " Package " + str(testing.get("package")) + " is ready for testing and this session is for it: "
                   "test it now against its acceptance criteria, then record `ygg acceptance record --from " +
                   str(data["node"]) + " --package " + str(testing.get("package")) +
                   " --sha <that sha> --verdict accept|reject`. Nobody else will ask you to.")
    if cli == "codex":
        argv = [util.env("YGG_CODEX_BIN", "codex"), "exec"] + codex_sandbox(data, node) + ["-m", data["model"]]
        if effort:
            argv += ["-c", "model_reasoning_effort=" + effort]
        # The prompt never goes on the command line (visible in process listings): `-` makes Codex read it
        # from standard input.
        argv.append("-")
        stdin_text = prompt
        environment = child_env(data)
    else:
        argv, environment = claude_command(data, child_env(data))
        # The prompt reaches Claude Code on standard input, never as an argument.
        stdin_text = prompt
    node_id = data["node"]
    from . import lifecycle
    faults.supervisor_setup(node_id, activation)
    faults.barrier("before-gate", node_id, activation)
    # Gate: under the journal lock, the reservation must be this node's newest and not ended; then
    # `spawned` is committed with this supervisor's identity. Otherwise nothing is started.
    me = lifecycle.own_identity()
    nonce = uuid.uuid4().hex  # put in the agent's environment; adoption after a crash requires it
    environment["YGG_ACTIVATION_NONCE"] = nonce
    from .linear.adapter import Changed
    with util.locked():
        for attempt in range(GATE_ATTEMPTS):
            found = lifecycle.facts(_programme(node_id), activation)
            newest = _newest_reservation(node_id)
            if lifecycle.terminal_of(found) is not None or (newest is not None and newest != activation):
                return 0
            if any(e.get("phase") == "spawned" for e in found):
                return 0  # already spawned (an adopted journal can show it): never a second agent
            try:
                committed = lifecycle.commit(node_id, activation, "spawned", supervisor_pid=me["pid"],
                                             supervisor_started_at=me["started_at"], agent_nonce=nonce)
            except Changed:
                # Refused before anything was written, with Linear's newer journal adopted into the cache: the
                # checks above run again on it, still under this lock. No other refusal is retried.
                if attempt + 1 == GATE_ATTEMPTS:
                    raise
                continue
            if not committed:
                return 0
            break
        update_receipt(activation, supervisor_pid=me["pid"], supervisor_started_at=me["started_at"],
                       agent_nonce=nonce)  # lets a local-only orphan scan verify the agent (stop, offline)
    faults.barrier("before-final-check", node_id, activation)
    # Final admission check, step 1, with no creation lock held: the journal and today's hold checks.
    refusal = None
    try:
        with util.locked():
            if lifecycle.terminal_of(lifecycle.facts(_programme(node_id), activation)) is not None:
                refusal = "ended"
            else:
                refusal = hold_reason(node, activation)
    except YggError as error:
        refusal = "journal unreadable: " + error.message
    process = None
    spawn_error = None
    if refusal is None:
        # Step 2, under the creation lock and with local files only: the interlock markers and the
        # revocation record; then the agent is created and its identity recorded before the lock goes.
        with util.locked(util.CREATION_LOCK):
            refusal = _local_refusal(node, activation)
            if refusal is None:
                try:
                    out, err = output_files(activation)
                    try:
                        # The prompt goes on standard input; the output is kept in private files.
                        process = subprocess.Popen(argv, cwd=data["cwd"], env=environment,
                                                   stdin=subprocess.PIPE, stdout=out, stderr=err, text=True,
                                                   start_new_session=True)
                    finally:
                        out.close()
                        err.close()
                except OSError as error:
                    spawn_error = str(error)
                if process is not None:
                    faults.boundary("after-spawn")  # A7.2: agent started, pid not yet recorded
                    update_receipt(activation, pid=process.pid, process_started_at=util.process_start(process.pid))
            if refusal is not None or spawn_error is not None:
                # Positive no-agent evidence, written locally first; the journal comes after the lock.
                lifecycle.write_local(lifecycle.no_agent_path(activation),
                                      {"activation": activation, "reason": refusal or "spawn-failed",
                                       "at": util.iso()})
    if process is None:
        if not lifecycle.no_agent_path(activation).exists():
            # Every refusal leaves positive no-agent evidence locally before any journal write.
            lifecycle.write_local(lifecycle.no_agent_path(activation),
                                  {"activation": activation, "reason": refusal or "spawn-failed", "at": util.iso()})
        try:
            with util.locked():
                if spawn_error is not None:
                    # A failed process creation is an exit with no agent (127): reported as failed, it
                    # consumes nothing, and the work stays owed.
                    update_receipt(activation, exit_code=127, exited_at=util.iso(), stderr_tail=spawn_error,
                                   spawn_failed=True)
                    lifecycle.commit(node_id, activation, "exited", exit_code=127, spawn_failed=True)
                else:
                    abort_activation(node_id, activation, _abort_reason(refusal))
                lifecycle.no_agent_path(activation).unlink(missing_ok=True)
        except YggError:
            pass  # the no-agent record stays; recovery turns it into `aborted` once the tracker is back
        return 0 if spawn_error is None else 1

    def forward(signum, frame):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, forward)
    # The prompt reaches the agent on standard input, which is then closed.
    process.communicate(input=stdin_text)
    code = process.returncode
    # The exit is recorded only once the agent's whole process group is gone (AC-71 row 3): a
    # child it left behind still works in the lane.
    from . import interlock
    while interlock.group_alive(process.pid):
        time.sleep(0.2)
    faults.boundary("after-exit")  # A7.2: agent exited, exit not yet recorded
    tail = "\n".join(_tail(output_path(activation, "stderr")).splitlines()[-20:])
    if code is not None and code < 0:
        pass  # killed by a signal (stop, reset, or a test teardown): no rehydration document is written
    else:
        try:
            from . import rehydrate
            rehydrate.write_document(data["node"])
        except Exception as error:  # the receipt must still be written
            tail += "\nrehydrate failed: " + str(error)
    with util.locked():
        record = receipt(activation)
        # The state root may have been wiped while the agent ran: the record's identity is restored from
        # what this supervisor knows, so the exit is classified and saved as this activation's.
        record.setdefault("activation", activation)
        record.setdefault("node", data["node"])
        record.update(exit_code=code, exited_at=util.iso(), stderr_tail=tail)
        fields = exit_fields(record, activation)
        if fields.get("interrupted") or fields.get("ambiguous"):
            # A stop or reset: neither a failure for the liveness path nor a capacity exit. A crash (a signal
            # Yggdrasil didn't send) is marked as such, for the retry count (A11.1 AC-01).
            record["interrupted"] = True
            if fields.get("crashed"):
                record["crashed"] = True
        elif record.get("outcome") == "confirmed" and classify(record) == "capacity":
            # A quota failure after confirmation is a capacity hold, not a crash for the liveness path.
            record["class"] = "capacity"
        save_receipt(activation, record)
        from . import lifecycle
        lifecycle.commit(data["node"], activation, "exited", exit_code=code, **fields)
        runtime = state.runtime(data["node"])
        if record.get("class") == "capacity" and record.get("outcome") == "confirmed":
            apply_outcome(data["node"], activation, "capacity")
        elif runtime.get("activation") == activation and runtime.get("state") == "running" and \
                (code == 0 or record.get("interrupted")):
            runtime["state"] = "idle"
            runtime["last_exit_at"] = util.iso()
            state.save_runtime(data["node"], runtime)
    if fields.get("ambiguous"):
        try:
            lifecycle.alert_stop_holds()
        except YggError:
            pass  # the conductor's next pass reports the hold
    return 0


def output_path(activation: str, stream: str) -> Path:
    """Where an activation's agent output is kept: `<state root>/output/<activation>/<stream>.log`."""
    return state.root() / "output" / activation / (stream + ".log")


def output_files(activation: str):
    """The agent's stdout and stderr files, created private to the user (folders 0700, files 0600) before
    the agent starts, so its output is kept for diagnosis. Local files only (creation lock safe)."""
    folder = util.ensure_dir(state.root() / "output" / activation)
    os.chmod(folder.parent, 0o700)
    os.chmod(folder, 0o700)
    files = []
    for stream in ("stdout", "stderr"):
        handle = os.open(folder / (stream + ".log"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        files.append(os.fdopen(handle, "wb"))
    return files[0], files[1]


def _tail(path: Path, limit: int = 65536) -> str:
    """The end of an output file (at most `limit` bytes), decoded leniently; empty when it is missing."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _newest_reservation(node_id: str) -> str | None:
    """The node's newest reserved activation, from the journal."""
    from . import lifecycle
    newest = None
    for key, found in lifecycle.all_facts(_programme(node_id)).items():
        if any(e.get("phase") == "reserved" and e.get("node") == node_id for e in found):
            if newest is None or int(key.split("-")[1]) > int(newest.split("-")[1]):
                newest = key
    return newest


def _local_refusal(node: dict, activation: str) -> str | None:
    """Final check under the creation lock, local files only: a stop or pause interlock marker covering the
    node, or a revocation record for this activation."""
    from . import lifecycle, safety
    if lifecycle.revoked_path(activation).exists():
        return "revoked"
    scopes = {"*", node["id"], node.get("programme")}
    for marker in safety.markers():
        if marker.get("scope") in scopes:
            return "stopped" if marker.get("kind") == "stop" else "paused"
    return None


def _abort_reason(refusal) -> str:
    """The abort reason today's effects understand: a stop keeps the node stopped, others requeue it."""
    if refusal == "stopped":
        return "stopped"
    if refusal == "paused":
        return "paused"
    return str(refusal)
