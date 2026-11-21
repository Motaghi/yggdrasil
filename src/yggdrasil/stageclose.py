"""Stage close and the needs-L1 wake (A11 lane 2: AC-14, AC-62, AC-63, AC-80).

Ready to close means the executing stage has at least one package that was authorised and every one of its
packages is accepted, merged or cancelled. Under `stage_close: default` the PM is owed one close task; when its
close activation ends without ending the stage, one class-user `ygg-stage-ready` alert follows. Under `auto` the
conductor ends the stage itself (never approving it), or says why it couldn't. `pm-only` refuses the user's
`ygg stage end`. Everything here is switched by release.on("stage_close"); with it off, today's behaviour.

Markers (ready seen, close task, refusal) live in the state root: they are derived and may be lost.
"""

from __future__ import annotations

from . import config, release, state, tracker, util
from .util import YggError

CLOSED_OK = ("accepted", "merged", "cancelled")


def _markers_path():
    return state.root() / "stage-close.json"


def _markers() -> dict:
    data = util.read_json(_markers_path(), {}) or {}
    return data if isinstance(data, dict) else {}


def _save_markers(data: dict) -> None:
    util.write_json(_markers_path(), data)


def mode(project: str) -> str:
    return str(config.project_get("stage_close", project, "default") or "default")


def _stage(project: str):
    from .commands import current_stage
    data, stage = current_stage(project)
    return data, stage


def _packages(project: str, number: int) -> list:
    found = []
    for issue in tracker.issues(project):
        if issue.get("kind") != "work-package":
            continue
        try:
            if int(issue.get("stage") or 1) != int(number):
                continue
        except (TypeError, ValueError):
            continue
        found.append(issue)
    return found


def closable(project: str) -> int | None:
    """The number of the project's executing stage when it is ready to close, else None."""
    record = tracker.project(project) or {}
    if record.get("state") != "executing":
        return None
    _, stage = _stage(project)
    if stage is None or stage.get("state") != "executing":
        return None
    packages = _packages(project, stage["number"])
    if not packages:
        return None
    if all(p.get("state") in CLOSED_OK for p in packages):
        return int(stage["number"])
    return None


def _key(project: str, number: int) -> str:
    return project + ":" + str(number)


def settle_close_task(project: str) -> None:
    """Withdraw the PM's close task of the project's current stage (a reopen, or the stage ended)."""
    markers = _markers()
    changed = False
    for key in [k for k in markers if k.startswith(project + ":")]:
        stamp = markers[key].get("task")
        if stamp:
            try:
                with util.locked():
                    data = tracker.milestones(project)
                    owed = data.get("owed") or []
                    kept = [s for s in owed if not (isinstance(s, dict) and s.get("id") == stamp)]
                    if len(kept) != len(owed):
                        data["owed"] = kept
                        tracker.save_milestones(project, data)
            except (YggError, OSError):
                pass
        markers.pop(key)
        changed = True
    if changed:
        _save_markers(markers)


def _consumed(pm: str, stamp: str) -> bool | None:
    """Whether an activation given the stamp has exited 0 (True), exists unfinished (False), or none yet (None)."""
    folder = state.root() / "activations"
    found = None
    if folder.is_dir():
        for path in folder.glob("act-*.json"):
            data = util.read_json(path, None)
            if not isinstance(data, dict) or data.get("node") != pm:
                continue
            if stamp not in (((data.get("consumed") or {}).get("records")) or []):
                continue
            if data.get("exited_at") is not None and data.get("exit_code") == 0:
                return True
            found = False
    return found


def step() -> None:
    """conductor.BEFORE_LAUNCH_STEPS["stage_close"]."""
    if not release.on("stage_close"):
        return
    markers = _markers()
    changed = False
    for record in tracker.projects():
        project = record["id"]
        number = closable(project)
        if number is None:
            for key in [k for k in markers if k.startswith(project + ":")]:
                if markers[key].get("task"):
                    settle_close_task(project)
                    markers = _markers()
                else:
                    markers.pop(key)
                changed = True
            continue
        key = _key(project, number)
        entry = markers.setdefault(key, {})
        if not entry.get("ready"):
            entry["ready"] = util.iso()
            changed = True
            state.alert("ygg-stage-ready", "Stage " + str(number) + " of " + project + " is ready to close",
                        project=project, stage=number)
        how = mode(project)
        if how == "auto":
            try:
                end_stage(project, number, by="auto")
                entry.pop("refused", None)
                markers = _markers()
                markers.pop(key, None)
                changed = True
                continue
            except (YggError, OSError) as error:
                reason = error.message if isinstance(error, YggError) else str(error)
                if entry.get("refused") != reason:
                    entry["refused"] = reason
                    changed = True
                    state.alert("ygg-stage-close-refused", "Stage " + str(number) + " of " + project +
                                " couldn't be closed: " + reason, project=project, stage=number)
            continue
        pm = record["pm"]
        if not entry.get("task"):
            try:
                with util.locked():
                    tracker.ensure_writable()
                    data = tracker.milestones(project)
                    tracker.owe(data, pm, "close-stage:" + str(number))
                    entry["task"] = data["owed"][-1]["id"]
                    tracker.save_milestones(project, data)
                changed = True
            except YggError:
                pass
            continue
        if entry.get("escalated"):
            continue
        if _consumed(pm, entry["task"]):
            if entry.get("exit_seen"):
                entry["escalated"] = True
                changed = True
                state.alert("ygg-stage-ready", "Stage " + str(number) + " of " + project + " is ready to close; "
                            "the PM didn't end it", project=project, stage=number, escalated=True)
            else:
                entry["exit_seen"] = util.iso()
                changed = True
    if changed:
        _save_markers(markers)


def end_stage(project: str, number: int, *, by: str) -> str:
    """The one close transition (user form and auto close): the end-stage report listing each package with its
    acceptance SHA, `within tolerance: yes`, the stage and project awaiting approval. Never approves."""
    from .commands import _project_for, _stage_end_alerts, current_stage, wrap_document
    with util.locked():
        tracker.ensure_writable()
        record = _project_for(project)
        data, stage = current_stage(project)
        if stage is None or int(stage["number"]) != int(number) or stage.get("state") != "executing":
            raise util.refuse("gate: stage " + str(number) + " of " + project + " is not executing")
        lines = ["# End of stage " + str(number), "", "within tolerance: yes", ""]
        for issue in _packages(project, number):
            sha = next((r.get("sha") for r in reversed(issue.records)
                        if r.get("kind") == "acceptance" and r.get("verdict") == "accept"), None)
            lines.append("- " + issue.id + ": " + str(issue.get("state")) + (", accepted at " + str(sha) if sha else ""))
            # The PM's acceptance and completion notices lead to this close: settled with it (AC-14 row 2).
            owed = issue.header.get("owed") or []
            kept = [o for o in owed if not (isinstance(o, dict) and o.get("node") == record["pm"] and
                                            str(o.get("reason") or "").startswith(("acceptance:", "wp-complete:")))]
            if len(kept) != len(owed):
                issue.header["owed"] = kept
                issue.save()
        tracker.write_document(project, "end-stage-" + str(number),
                               wrap_document("end-stage", record["pm"], "\n".join(lines) + "\n", stage=number))
        stage["report"] = "documents/end-stage-" + str(number) + ".md"
        stage["within_tolerance"] = "yes"
        stage["closed_by"] = by
        stage["state"] = "awaiting-approval"
        stage["ended_at"] = util.iso()
        tracker.save_milestones(project, data)
        record = _project_for(project)
        record["state"] = "awaiting-approval"
        tracker.save_project(record)
    settle_close_task(project)
    _stage_end_alerts(project, number)
    return "stage " + str(number) + " awaiting approval\n"


def user_stage_end(project: str) -> str:
    """`ygg stage end <project>` with no flags: the user's form (AC-14 rows 2 and 3, AC-63)."""
    state.check_user_only("stage end")
    if not release.on("stage_close"):
        raise util.refuse("ygg stage end without --within-tolerance and --report is not in this release")
    util.check_id(project, "project")
    if tracker.project(project) is None:
        raise util.usage("unknown project '" + project + "'")
    if mode(project) == "pm-only":
        raise util.refuse("stage close is PM-only for this project")
    _, stage = _stage(project)
    if stage is not None and stage.get("state") == "awaiting-approval":
        raise util.refuse("stage " + str(stage["number"]) + " is already awaiting approval")
    if stage is None:
        raise util.refuse("gate: project " + project + " has no executing stage")
    number = closable(project)
    if number is None:
        raise util.refuse("stage " + str(stage["number"]) + " has packages that aren't accepted")
    return end_stage(project, number, by="user")


def status_suffix(project: str) -> str:
    if not release.on("stage_close"):
        return ""
    try:
        number = closable(project)
    except YggError:
        return ""
    if number is None:
        return ""
    entry = _markers().get(_key(project, number)) or {}
    if entry.get("refused"):
        return ", stage " + str(number) + " ready to close (close refused: " + str(entry["refused"]) + ")"
    return ", stage " + str(number) + " ready to close"


# ---------------------------------------------------------------- needs-L1 (AC-80)

def _last_activity(project: str, nodes: dict):
    from . import journal
    record = tracker.project(project) or {}
    members = {n for n, node in nodes.items() if node.get("project") == project}
    newest = None
    entries, _ = journal.trusted_entries(record.get("programme"))
    for entry in entries:
        if entry.get("node") not in members and entry.get("pm") not in members:
            continue
        try:
            moment = util.parse_time(str(entry.get("at")))
        except (TypeError, ValueError):
            continue
        newest = moment if newest is None else max(newest, moment)
    for node_id in members:
        text = util.read_text(state.root() / "heartbeats" / node_id).strip()
        if not text:
            continue
        try:
            moment = util.parse_time(text)
        except ValueError:
            continue
        newest = moment if newest is None else max(newest, moment)
    return newest


def needs_l1_step() -> None:
    """conductor.BEFORE_LAUNCH_STEPS["needs_l1"]: one `ygg-needs-l1` when a project has owed work and nothing
    moved for `needs_l1_minutes`; again only after something moves."""
    from . import reducer
    minutes = int(config.get("needs_l1_minutes", 30))
    nodes = state.all_nodes()
    owed = reducer.owed_all()
    path = state.root() / "needs-l1.json"
    known = util.read_json(path, {}) or {}
    changed = False
    moment = util.now()
    for record in tracker.projects():
        project = record["id"]
        if record.get("state") == "closed":
            continue
        if not any(nodes.get(n, {}).get("project") == project and items for n, items in owed.items()):
            continue
        last = _last_activity(project, nodes)
        if last is None or (moment - last).total_seconds() < minutes * 60:
            continue
        stamp = util.iso(last)
        if known.get(project) == stamp:
            continue
        known[project] = stamp
        changed = True
        state.alert("ygg-needs-l1", project + " has work waiting and nothing has moved for " + str(minutes) +
                    " minutes.", project=project)
    if changed:
        util.write_json(path, known)
