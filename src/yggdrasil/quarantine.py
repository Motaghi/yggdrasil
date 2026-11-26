"""Quarantined boxes in `ygg status`, cleanup after a lane merges, the cancel's quarantine and the merge `--repo`
box check (v1.1.1).

The primitive itself is `clones.quarantine` and `clones.do_next`, shared with the lane-clone module: quarantine never
moves a box; it is a state in Yggdrasil's own record, the box stays where it is, and the next generation is built
at a new path beside it. This module adds what calls it and what shows it:

- `cleanup_step`, each tick: at most one quarantine, a record left `quarantining` first (crash recovery), else the
  first clone lane owed a close whose activations have all settled (`next: close`);
- `quarantine_clone`, for `ygg wp cancel` and `--finish`: the box of the drop record's generation, rebuilt at the
  drop point;
- `status_lines`: per clone lane, the live box first, marked `live; do not remove`, then its quarantined boxes,
  newest first, each one that may hold work that was not received, with what the report saw; unknown folders; the
  recorded total;
- `inside_box` and `refuse_box_repo`: a merge verb's `--repo` never names a box;
- `end_orphans` (ending a lane's leftover processes before a quarantine) and `timeout_alert` (every third box
  check that ran out of time in 24 hours).
"""

from __future__ import annotations

import os
from pathlib import Path

from . import util

COUNTS = ("refs", "stash", "commits", "changed", "untracked", "ignored")
# The public page on quarantined boxes; an installed copy of Yggdrasil has no docs folder.
SECURITY_PAGE = "https://github.com/Motaghi/yggdrasil/blob/main/docs/security.md#quarantined-boxes"
WARNING = ("agent-written files: before you remove it, copy it with cp -RP outside any git checkout and compare the "
           "copy with the home as " + SECURITY_PAGE + " says; do not open it or the copy with Codex, Claude Code, "
           "an editor or a git-aware shell, nor with git except as that page says")
REPLACED_ALERT = ("the checks of the clone of lane ygg/{project}/lane/{lane} ran out of time {n} times in the last "
                  "24 hours, and the clone was replaced {r} times; the old boxes are kept in the lane folder")


# ---------------------------------------------------------------- records (the lane-clone registry)

def _clones():
    from . import clones
    return clones


def lane_folder(home, project: str) -> Path:
    return _clones().lane_folder(str(home), project)


def lane_records(project: str) -> dict:
    """lane -> clone record, for every lane clone record of the project (tombstones included)."""
    folder = _clones().registry_dir(project)
    found = {}
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return found
    for name in names:
        if not name.endswith(".json") or name.startswith((".", "_", "test-")):
            continue
        data = util.read_json(folder / name, None)
        if isinstance(data, dict) and data.get("kind", "lane") == "lane":
            found[name[:-len(".json")]] = data
    return found


def quarantine_records(project: str) -> list[dict]:
    """Every quarantine record of the project, each with its team lead (`team_lead`)."""
    folder = _clones().quarantine_dir(project)
    found = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return found
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        data = util.read_json(folder / name, None)
        if isinstance(data, dict):
            data.setdefault("team_lead", name.rsplit(".g", 1)[0])
            found.append(data)
    return found


def _lane_entries(home, project: str) -> list[str]:
    """The entry names of the lane folder (a folder no agent can write, the one Yggdrasil lists itself)."""
    try:
        return sorted(os.listdir(lane_folder(home, project)))
    except OSError:
        return []


def _entry_present(path: str) -> bool:
    """An `lstat` of a box's own entry in the lane folder; it follows nothing."""
    try:
        os.lstat(path)
    except OSError:
        return False
    return True


def is_clone_checkout(path) -> bool:
    """Whether `path`, as a string, is the checkout of a lane or test clone in the registry."""
    text = os.path.normpath(str(path))
    root = util.state_root() / "lane-clones"
    try:
        projects = os.listdir(root)
    except OSError:
        return False
    for project in projects:
        try:
            names = os.listdir(root / project)
        except OSError:
            continue
        for name in names:
            if name.endswith(".json"):
                data = util.read_json(root / project / name, None)
                if isinstance(data, dict) and isinstance(data.get("path"), str) and \
                        os.path.normpath(data["path"]) == text:
                    return True
    return False


def _box_roots() -> set:
    """The real path of every live and quarantined box in the registry: each box's parent folder resolved (a folder
    of Yggdrasil's own layout) joined to the box's own name. Nothing inside a box is resolved."""
    root = util.state_root() / "lane-clones"
    try:
        projects = os.listdir(root)
    except OSError:
        return set()
    found = set()
    for project in projects:
        for folder in (root / project, root / project / "_quarantine"):
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                data = util.read_json(folder / name, None) if name.endswith(".json") else None
                for key in ("box", "path"):
                    value = data.get(key) if isinstance(data, dict) else None
                    if isinstance(value, str) and os.path.isabs(value):
                        box = os.path.normpath(value)
                        box = box[:-len("/checkout")] if key == "path" and box.endswith("/checkout") else box
                        found.add(os.path.join(os.path.realpath(os.path.dirname(box)), os.path.basename(box)))
    return found


def _identities(boxes: set) -> set:
    """(device, inode) of each box's own entry, by `lstat` (rule 1); a box that is gone has none."""
    found = set()
    for box in boxes:
        try:
            info = os.lstat(box)
        except OSError:
            continue
        found.add((info.st_dev, info.st_ino))
    return found


def inside_box(path) -> bool:
    """Whether `path` is, or reaches, a live or quarantined box. The raw argument is walked one component at a
    time from the real working folder: a `..` steps to the real parent, a link outside every box is resolved and
    walked on, and each step is compared with every box by text and by the device and inode of its `lstat`, so a
    different spelling of a box (letter case on a case-insensitive volume, Unicode forms) is found too. The walk
    stops at the first step that is a box, so it never stats or follows anything inside one."""
    boxes = _box_roots()
    if not boxes:
        return False
    ids = _identities(boxes)

    def within(candidate: str) -> bool:
        if any(candidate == box or candidate.startswith(box + os.sep) for box in boxes):
            return True
        try:
            info = os.lstat(candidate)
        except OSError:
            return False
        return (info.st_dev, info.st_ino) in ids

    raw = str(path)
    current = "/"
    pending = [part for part in raw.split("/") if part not in ("", ".")]
    if not raw.startswith("/"):
        # The working folder as the kernel reports it, walked like the rest: each of its components is checked
        # against the boxes before anything is resolved, so a working folder inside a box is never resolved.
        pending = [part for part in os.getcwd().split("/") if part] + pending
    hops = 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            current = os.path.dirname(current) or "/"
            continue
        candidate = os.path.join(current, part)
        if within(candidate):
            return True
        if os.path.islink(candidate):
            hops += 1
            if hops > 40:
                return True  # a link loop is never trusted as a repository path
            target = os.readlink(candidate)
            if target.startswith("/"):
                current = "/"
            pending = [p for p in target.split("/") if p not in ("", ".")] + pending
            continue
        current = candidate
    return False


def refuse_box_repo(path, programme: str | None) -> None:
    """A merge verb's `--repo` must name the registered home, never a box: no trusted git may run in one."""
    if path is not None and inside_box(path):
        raise util.refuse("gate: --repo must name the home of programme " + str(programme or "?"))



# ---------------------------------------------------------------- quiesce, cancel, cleanup and the time-out alert

def end_orphans(lane: str) -> None:
    """Before a quarantine: end any process whose environment carries one of the lane's recent activation identities
    (the existing orphan scan), best effort, by exact pid."""
    import signal
    from . import lifecycle, state
    for node_id in _clones().lane_nodes(lane):
        key = state.runtime(node_id).get("activation")
        if not key:
            continue
        try:
            status, pids = lifecycle.orphan_agents(str(key))
        except (util.YggError, OSError, ValueError):
            continue
        for pid in pids if status == "ok" else []:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def kept_reason(packages: list[str]) -> str:
    return "kept when " + " and ".join(packages) + (" were" if len(packages) > 1 else " was") + " cancelled"


def quarantine_clone(project: str, lane: str, drop: dict) -> None:
    """What is left of a clone lane's cancel, in order: quarantine the box of the drop record's generation (from
    the start, or from writing its quarantine record when the clone record is already `quarantining`, or nothing
    when its quarantine record exists), then assemble the next generation at the drop point. Never a clone at the
    pre-cancel head, and never another generation's box."""
    clones = _clones()
    generation, point = drop.get("clone_generation"), drop["drop_point"]
    record = clones.read(project, lane) or {}
    if generation is None or record.get("generation") != generation:
        return  # that generation's box was quarantined and replaced already
    if record.get("state") == "ready":
        end_orphans(lane)
        clones.quarantine(project, lane, reason=kept_reason(list(drop.get("packages") or [])), next_step="rebuild",
                          at=point)
    elif record.get("state") == "quarantining":
        clones.quarantine(project, lane, reason=str(record.get("reason")), next_step=str(record.get("next")),
                          odd_check=record.get("odd_check"), at=record.get("at") or point)
    else:
        return
    clones.do_next(project, lane)


def _lane_done(project: str, lane: str) -> str | None:
    """The cleanup reason when the lane is owed a close, else None: every package merged and the project
    `ready-to-merge` or `closed` (t3's rule), or the project closed or cancelled."""
    from . import t3, tracker
    data = tracker.project(project) or {}
    if data.get("state") == "cancelled":
        return "cancelled"
    if t3._lane_merged(project, lane):
        return "merged"
    if data.get("state") == "closed":
        return "closed"
    return None


def cleanup_step() -> None:
    """Each tick, at most one quarantine. A record left `quarantining` with no open drop record is finished first
    (recovery: the quarantine record from the live record when it is missing, then `next`); else the first clone
    lane owed a close whose activations have all settled is quarantined with `next: close`. A lane with an
    interrupted cancel is left to `ygg wp cancel --finish`. Each candidate's checks are made again under its lane
    lock, which is held through the quarantine and the recovery, so neither acts on a record that changed."""
    from . import tracker
    clones = _clones()
    projects = sorted(p["id"] for p in tracker.projects() if p.get("id"))
    for project in projects:
        for lane, record in lane_records(project).items():
            if _recovery_owed(project, lane, record):
                with clones.lane_lock(project, lane):
                    record = clones.read(project, lane) or {}
                    if not _recovery_owed(project, lane, record):
                        continue
                    clones.quarantine(project, lane, reason=str(record.get("reason")),
                                      next_step=str(record.get("next")), odd_check=record.get("odd_check"),
                                      at=record.get("at"))
                    clones.do_next(project, lane)
                return
    for project in projects:
        for lane, record in lane_records(project).items():
            if _close_owed(project, lane, record) is None:
                continue
            with clones.lane_lock(project, lane):
                why = _close_owed(project, lane, clones.read(project, lane) or {})
                if why is None:
                    continue
                end_orphans(lane)
                clones.quarantine(project, lane, reason="kept after lane ygg/" + project + "/lane/" + lane + " " +
                                  why, next_step="close")
                clones.do_next(project, lane)
            return


def _recovery_owed(project: str, lane: str, record: dict) -> bool:
    from . import lanedrop
    return record.get("kind", "lane") == "lane" and record.get("state") == "quarantining" and \
        not lanedrop.interrupted(project, lane) and not _clones().lane_alive(lane)


def _close_owed(project: str, lane: str, record: dict) -> str | None:
    """The cleanup reason when a `ready` lane is owed a close and no activation of it runs, else None."""
    from . import lanedrop
    if record.get("kind", "lane") != "lane" or record.get("state") != "ready" or lanedrop.interrupted(project, lane):
        return None
    why = _lane_done(project, lane)
    if why is None or _clones().lane_alive(lane):
        return None
    return why


def tick_step() -> None:
    try:
        cleanup_step()
    except util.YggError:
        pass


def timeout_alert(project: str, lane: str, count: int) -> None:
    """At the third box check that ran out of time within 24 hours, and at every third after it, one
    `ygg-clone-replaced` alert; the lane does not hold for it."""
    if count <= 0 or count % 3:
        return
    from . import state
    replaced = len([q for q in quarantine_records(project) if q.get("team_lead") == lane and
                    str(q.get("odd_check")) == "5"])
    state.alert("ygg-clone-replaced", REPLACED_ALERT.format(project=project, lane=lane, n=count, r=replaced),
                project=project, lane=lane)


# ---------------------------------------------------------------- the listing in `ygg status`

def _quoted(text) -> str:
    """A string from the box (through the tender's report) as one line: Git's C-style quoting when it holds a
    control character, a non-printable character, a comma or a double quote."""
    text = str(text)
    if any(not ch.isprintable() or ch in ',"\\' for ch in text):
        out = []
        for ch in text:
            if ch in '"\\':
                out.append("\\" + ch)
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            elif not ch.isprintable():
                out.append("".join("\\%03o" % byte for byte in ch.encode("utf-8")))
            else:
                out.append(ch)
        return '"' + "".join(out) + '"'
    return text


def _resolved(base: str, value: str, box: str) -> str:
    """A path the walk printed, resolved by trusted code as text only (no realpath, no stat, no file read)."""
    if value.startswith(("http://", "https://")):
        return _quoted(value)
    joined = os.path.normpath(value if value.startswith("/") else os.path.join(base, value))
    inside = joined == box or joined.startswith(box.rstrip("/") + "/")
    return _quoted(joined) + (" (inside the box)" if inside else "")


def _item_text(item: dict, box: str) -> str:
    path = str(item.get("path") or "")
    kind = str(item.get("kind") or "")
    gitdir = os.path.join(box, "checkout", ".git")
    shown = [_quoted(path) + ((" (" + _quoted(kind) + ")") if kind else "")]
    targets = []
    if item.get("target") is not None:
        folder = os.path.dirname(os.path.join(gitdir, path))
        targets.append(_resolved(folder, str(item["target"]), box))
    for line in item.get("lines") or []:
        if path.endswith("alternates"):
            base = os.path.join(gitdir, "objects")
        elif path.startswith("worktrees/"):
            base = os.path.dirname(os.path.join(gitdir, path))
        else:
            base = gitdir
        if path.endswith("http-alternates"):
            targets.append(_quoted(line))
        else:
            targets.append(_resolved(base, str(line).removeprefix("gitdir: "), box))
    return shown[0] + ((" -> " + ", ".join(targets)) if targets else "")


def _printable(text) -> str:
    """Yggdrasil's own reason text, with any character that is not printable shown as `?`."""
    return "".join(ch if ch.isprintable() else "?" for ch in str(text))


def report_text(report) -> str:
    """What the quarantine's report saw (clones' stored form): the six counts and the metadata list when it
    finished, else `report did not finish`. Never an all-clear."""
    if report == "comparison commit missing":
        return "report did not finish (comparison commit missing)"
    counts = report.get("counts") if isinstance(report, dict) else None
    if isinstance(counts, dict) and all(isinstance(counts.get(k), int) for k in COUNTS):
        return "the report saw " + ", ".join(k + " " + str(counts[k]) for k in COUNTS) + \
            "; metadata the report saw: " + (", ".join(map(_quoted, report.get("metadata") or [])) or "none")
    return "report did not finish"


def _items(report) -> list[dict]:
    walk = report.get("walk") if isinstance(report, dict) else None
    items = (walk or {}).get("outside") if isinstance(walk, dict) else None
    return [i for i in items or [] if isinstance(i, dict) and i.get("path") != "and more"]


def _items_text(report, box: str) -> str:
    items = _items(report)
    if not items:
        return ""
    shown = [_item_text(item, box) for item in items[:10]]
    return "; outside dependencies the walk saw: " + "; ".join(shown) + ("; and more" if len(items) > 10 else "")


def _size(value: int) -> str:
    number = float(value)
    for unit in ("bytes", "KiB", "MiB"):
        if number < 1024:
            return (str(int(number)) + " bytes") if unit == "bytes" else "%.1f %s" % (number, unit)
        number /= 1024
    return "%.1f GiB" % number


def _assembling(records: dict, folder: str, name: str) -> bool:
    """A `.creating-<nonce>` folder that a `creating` record names by its nonce."""
    return any(r.get("state") == "creating" and name == ".creating-" + str(r.get("nonce") or "")
               for r in records.values())


def status_lines(project: str, home) -> list[str]:
    """For each clone lane of the project: its live box first, marked `live; do not remove`, then its quarantined
    boxes, newest generation first; then entries of the lane folder no record names, and a total line."""
    lines = []
    folder = str(lane_folder(home, project))
    entries = _lane_entries(home, project)
    named = set()
    total, size_total = 0, 0
    records = lane_records(project)
    quarantined = quarantine_records(project)
    lanes = sorted(set(records) | {str(q.get("team_lead")) for q in quarantined if q.get("team_lead")})
    for lane in lanes:
        record = records.get(lane) or {}
        if record.get("state") in ("ready", "quarantining", "creating") and record.get("box"):
            box = str(record["box"])
            named.add(os.path.basename(box))
            if record.get("state") == "ready" and _entry_present(box):
                lines.append(box + ": live; do not remove; lane ygg/" + project + "/lane/" + lane + ", generation " +
                             str(record.get("generation")))
        mine = sorted((q for q in quarantined if q.get("team_lead") == lane),
                      key=lambda q: int(q.get("generation") or 0), reverse=True)
        for data in mine:
            name = str(data.get("box") or "")
            named.add(name)
            path = str(data.get("path") or os.path.join(folder, name))
            if not name or not _entry_present(os.path.join(folder, name)):
                continue
            total += 1
            size = data["report"].get("size") if isinstance(data.get("report"), dict) else None
            size_total += size if isinstance(size, int) else 0
            day = str(data.get("at") or "")[:10]
            lines.append(path + ": quarantined " + day + ", may hold work that was not received; " +
                         _printable(data.get("reason") or "no reason recorded") + "; " + report_text(data.get("report")) +
                         _items_text(data.get("report"), path) + "; " + WARNING)
    for name in entries:
        if name not in named and not (name.startswith(".creating-") and _assembling(records, folder, name)):
            lines.append(os.path.join(folder, name) + ": unknown folder")
    if lanes:
        lines.append("quarantined boxes: " + str(total) + ", " + _size(size_total) + " recorded")
    return lines
