"""A cancel's lane side (A11.1 AC-06, AC-12, AC-13, AC-14; lane 1): the drop point, the commits a cancel
drops, their durable record, the reset with Yggdrasil's own git, and the guard that keeps dropped commits off
the lane.

Lane 2's `ygg wp cancel` owns the verb, its checks, the journal lock, the state and the completion fact; it
calls, under the journal lock: `plan` (refuses when a candidate already landed), `record` (the drop record,
durable before the reset), `reset`, then `complete` after its state writes. `--finish` calls `plan` again,
`add_commits` for the listed commits, `reset(..., clean=True)` and `complete`. A record with no completion is
an interrupted cancel (`interrupted`).

The records live in the safety folder beside the state root (`<state root>.safety/lanes/<project>.json`), so an
emptied state root never lets a dropped commit back: {"syncs": {sha: {lane, main, at}}, "drops": [{id,
packages, lane, head, drop_point, commits, at, completed_at}], "creations": {lane: {commit, at}}}.
"""

from __future__ import annotations

import json
import uuid

from . import util

GO_BACK = "the lane can't go back to the work of cancelled "
SKIP = (".claude/settings.json", ".ygg-managed")


# ---------------------------------------------------------------- the records

def _path(project: str):
    from . import safety
    util.check_id(project, "project")
    return safety.root() / "lanes" / (project + ".json")


def _load(project: str) -> dict:
    data = util.read_json(_path(project), {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("syncs", {})
    data.setdefault("drops", [])
    data.setdefault("creations", {})
    return data


def _save(project: str, data: dict) -> None:
    util.ensure_dir(_path(project).parent)
    util.atomic_write(_path(project), json.dumps(data, indent=2))


def recorded_syncs(project: str) -> dict:
    return dict(_load(project)["syncs"])


def record_sync(project: str, lane: str, sha: str, main: str) -> None:
    with util.locked():
        data = _load(project)
        data["syncs"][sha] = {"lane": lane, "main": main, "at": util.iso()}
        _save(project, data)


def record_creation(project: str, lane: str, commit: str) -> None:
    """Record the commit a lane branch was made from, once (A11.1, the drop point of a lane with no accepted or
    merged package). Called by the launcher where it creates the lane branch; an existing entry is kept."""
    util.check_id(lane, "lane")
    with util.locked():
        data = _load(project)
        if lane not in data["creations"]:
            data["creations"][lane] = {"commit": str(commit), "at": util.iso()}
            _save(project, data)


def creation(project: str, lane: str) -> str | None:
    """The recorded creation commit of the lane, or None (lanes made before A11.1)."""
    entry = _load(project)["creations"].get(lane)
    return str(entry["commit"]) if isinstance(entry, dict) and entry.get("commit") else None


def drops(project: str) -> list[dict]:
    return [d for d in _load(project)["drops"] if isinstance(d, dict)]


def dropped_commits(project: str) -> dict:
    """sha -> the cancelled packages it was dropped for, over every drop record of the project."""
    found = {}
    for drop in drops(project):
        for sha in drop.get("commits") or []:
            found[str(sha)] = ", ".join(drop.get("packages") or [])
    return found


def interrupted(project: str, lane: str) -> dict | None:
    """The lane's drop record with no completion fact, if any (AC-13)."""
    return next((d for d in drops(project) if d.get("lane") == lane and not d.get("completed_at")), None)


def record(project: str, plan: dict) -> str:
    """Write the drop record, durable before the reset; returns its id."""
    identifier = "drop-" + uuid.uuid4().hex[:12]
    with util.locked():
        data = _load(project)
        entry = {"id": identifier, "packages": list(plan["packages"]), "lane": plan["lane"],
                 "head": plan["head"], "drop_point": plan["drop_point"],
                 "commits": list(plan["dropped"]), "at": util.iso(), "completed_at": None}
        if plan.get("clone_generation") is not None:
            entry["clone_generation"] = int(plan["clone_generation"])  # the box a clone lane's cancel quarantines
        data["drops"].append(entry)
        _save(project, data)
    return identifier


def add_commits(project: str, identifier: str, commits) -> None:
    """`--finish`: add the commits it listed that weren't in the record, so the history guard covers them."""
    with util.locked():
        data = _load(project)
        for drop in data["drops"]:
            if drop.get("id") == identifier:
                drop["commits"] = list(dict.fromkeys(list(drop.get("commits") or []) + list(commits)))
        _save(project, data)


def complete(project: str, identifier: str) -> None:
    with util.locked():
        data = _load(project)
        for drop in data["drops"]:
            if drop.get("id") == identifier:
                drop["completed_at"] = util.iso()
        _save(project, data)


# ---------------------------------------------------------------- git

def _git(repo, *args, home_objects: str | None = None):
    return util.trusted_git("-C", repo, *args, home_objects=home_objects)


def _rev(repo, ref: str, home_objects: str | None = None) -> str | None:
    found = _git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}", home_objects=home_objects)
    return found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else None


def _ancestor(repo, older: str, newer: str, home_objects: str | None = None) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", older, newer, home_objects=home_objects).returncode == 0


def _parents(repo, sha: str) -> list[str]:
    return _git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()[1:]


def lane_branch(project: str, lane: str) -> str:
    return "ygg/" + project + "/lane/" + lane


def creation_point(project: str, lane: str, repo) -> str | None:
    """The commit the lane was made from: the recorded creation commit; for a lane made before A11.1 (nothing
    recorded), the merge base of the lane and the integration branch, else of the lane and the main recorded
    at PID approval."""
    head = _rev(repo, "refs/heads/" + lane_branch(project, lane))
    if not head:
        return None
    recorded = creation(project, lane)
    if recorded and _rev(repo, recorded) and _ancestor(repo, recorded, head):
        return recorded
    found = []
    integration = _rev(repo, "refs/heads/ygg/" + project + "/_integration")
    if integration:
        found.append(_git(repo, "merge-base", head, integration).stdout.strip())
    from . import gitops
    trusted = gitops.trusted_base(project, str(repo))
    if trusted and _rev(repo, trusted):
        found.append(_git(repo, "merge-base", head, trusted).stdout.strip())
    found = [sha for sha in found if sha]
    # The earlier of the two: once the lane was merged into the integration branch, that merge base moves past
    # the lane's own commits, and a reopened package's merged work must still count as a candidate (AC-13).
    for sha in found:
        if all(_ancestor(repo, sha, other) for other in found):
            return sha
    return found[0] if found else None


def drop_point(project: str, lane: str, repo, head: str | None = None) -> str | None:
    """The newest accepted head of a package on the lane still accepted or merged (else the lane's creation
    commit), moved forward one recorded sync at a time while the sync's lane-side parent is the current point
    and the sync is already in the integration branch (AC-06)."""
    from . import lanecheck
    head = head or _rev(repo, "refs/heads/" + lane_branch(project, lane))
    if not head:
        return None
    point = None
    for issue in lanecheck._packages(project):
        if issue.get("lane") != lane or issue.get("state") not in ("accepted", "merged"):
            continue
        sha = lanecheck._accepted_sha(issue)
        if sha and _rev(repo, sha) and _ancestor(repo, sha, head) and (point is None or _ancestor(repo, point, sha)):
            point = sha
    point = point or creation_point(project, lane, repo)
    if not point:
        return None
    integration = _rev(repo, "refs/heads/ygg/" + project + "/_integration")
    syncs = recorded_syncs(project)
    moved = True
    while moved and integration:
        moved = False
        for sha, info in syncs.items():
            if info.get("lane") != lane or not _rev(repo, sha) or not _ancestor(repo, sha, head):
                continue
            parents = _parents(repo, sha)
            if parents[:1] == [point] and _ancestor(repo, sha, integration):
                point, moved = sha, True
                break
    return point


def plan(project: str, lane: str, packages, repo) -> dict:
    """What a cancel of `packages` would do to the lane: {packages, lane, head, drop_point, dropped}. Refuses
    (exit 3, nothing changed) when a commit made on the lane is already in the integration branch or main."""
    packages = list(packages)
    head = _rev(repo, "refs/heads/" + lane_branch(project, lane))
    if not head:
        raise util.usage("lane branch " + lane_branch(project, lane) + " does not exist")
    point = drop_point(project, lane, repo, head) or head
    syncs = recorded_syncs(project)
    excluded = ["^" + point]
    for sha in syncs:
        if _rev(repo, sha) and _ancestor(repo, sha, head) and not _ancestor(repo, sha, point):
            excluded += ["^" + parent for parent in _parents(repo, sha)[1:]]  # main's own commits
    candidates = _git(repo, "rev-list", head, *excluded).stdout.split()
    integration = _rev(repo, "refs/heads/ygg/" + project + "/_integration")
    main = _rev(repo, "refs/heads/main")
    named = " and ".join(packages)
    for sha in candidates:
        if sha in syncs:
            continue  # a recorded sync merge not in the integration branch is dropped; the next sync remakes it
        if integration and _ancestor(repo, sha, integration):
            raise util.refuse(named + " is already in the project's integration branch; send a revert through "
                              "review instead")
        if main and _ancestor(repo, sha, main):
            raise util.refuse(named + " is already in main; send a revert through review instead")
    return {"packages": packages, "lane": lane, "head": head, "drop_point": point, "dropped": candidates}


def reset(repo, project: str, lane: str, point: str, *, clean: bool = False) -> None:
    """Move the lane branch and its worktree to the drop point with Yggdrasil's own git (no hooks). With
    `clean`, untracked and ignored files go too, except Yggdrasil's own guard files (AC-14)."""
    from . import gitops
    branch = lane_branch(project, lane)
    location = gitops.checked_out_at(repo, branch)
    if location:
        result = _git(location, "reset", "--hard", "--quiet", point)
        if result.returncode == 0 and clean:
            result = _git(location, "clean", "-ffdxq", *[x for name in SKIP for x in ("-e", "/" + name)])
    else:
        result = _git(repo, "branch", "-f", branch, point)
    if result.returncode != 0:
        raise util.refuse("lane reset failed: " + (result.stderr or result.stdout).strip())


def summary(plan_: dict) -> str:
    dropped = plan_["dropped"]
    count = str(len(dropped)) + (" commit dropped" if len(dropped) == 1 else " commits dropped")
    return (", ".join(plan_["packages"]) + " cancelled; lane " + plan_["lane"] + " reset to " +
            plan_["drop_point"][:7] + "; " + count + (": " + ", ".join(s[:7] for s in dropped) if dropped else "") +
            "\n")


# ---------------------------------------------------------------- the history guard (AC-12)

def history_problem(project: str, repo, new: str, home_objects: str | None = None) -> str | None:
    """Refuse a lane ref move to a commit that has a recorded dropped commit as an ancestor (the commit itself
    counts). `home_objects`: in a registered clone's hook, the home's object folder for these git calls (v1.1.1)."""
    if not new or set(new) == {"0"}:
        return None
    for sha, packages in dropped_commits(project).items():
        if _rev(repo, sha, home_objects) and _ancestor(repo, sha, new, home_objects):
            return GO_BACK + packages
    return None
