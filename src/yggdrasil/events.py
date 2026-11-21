"""Time-driven events evaluated on each conductor tick."""

from __future__ import annotations

from datetime import timedelta

from . import state, tracker, util


def checkpoints_path():
    return state.root() / "checkpoints.json"


def _programme_of(node_id: str):
    return (state.all_nodes().get(node_id) or {}).get("programme")


def note_checkpoint(pm: str, lane: str, msg_id: str | None = None) -> None:
    """Register a batch entry without a message (messages register theirs in ratatoskr.deliver's write)."""
    from . import journal
    extra = {"msg_id": msg_id} if msg_id else {}
    with util.locked():
        moment = util.iso()
        journal.add_many(_programme_of(pm), [("checkpoint", dict({"pm": pm, "lane": lane}, **extra))], at=moment)
        add_local_checkpoint(pm, lane, msg_id, moment)


def add_local_checkpoint(pm: str, lane: str, msg_id: str | None, at: str) -> None:
    """The local copy of a journalled batch entry, once per message id."""
    with util.locked():
        data = util.read_json(checkpoints_path(), {}) or {}
        items = data.setdefault(pm, [])
        if msg_id and any(item.get("msg_id") == msg_id for item in items):
            return
        items.append(dict({"lane": lane, "at": at}, **({"msg_id": msg_id} if msg_id else {})))
        util.write_json(checkpoints_path(), data)


def drop_checkpoint(pm: str, msg_id: str) -> None:
    """Remove the local batch entry of a checkpoint the PM acknowledged (its journal entry is cleared by the
    acknowledgement's own write)."""
    with util.locked():
        data = util.read_json(checkpoints_path(), {}) or {}
        items = data.get(pm) or []
        kept = [item for item in items if item.get("msg_id") != msg_id]
        if len(kept) != len(items):
            if kept:
                data[pm] = kept
            else:
                data.pop(pm)
            util.write_json(checkpoints_path(), data)


DURATION = __import__("re").compile(
    r"^P(?:(?P<w>[0-9]+)W)?(?:(?P<d>[0-9]+)D)?(?:T(?:(?P<h>[0-9]+)H)?(?:(?P<m>[0-9]+)M)?(?:(?P<s>[0-9]+)S)?)?$")


def duration(text) -> timedelta:
    match = DURATION.match(str(text or "").strip())
    if not match:
        return timedelta(0)
    parts = {k: int(v) for k, v in match.groupdict().items() if v}
    return timedelta(weeks=parts.get("w", 0), days=parts.get("d", 0), hours=parts.get("h", 0),
                     minutes=parts.get("m", 0), seconds=parts.get("s", 0))


def forecast_drift() -> None:
    from .commands import pid_document
    for project in tracker.projects():
        if project.get("state") not in ("executing", "awaiting-approval", "exception"):
            continue
        text = tracker.read_document(project["id"], "route-map")
        if not text:
            continue
        header, _ = util.split_frontmatter(text)
        stages = tracker.milestones(project["id"]).get("stages") or []
        current = next((s for s in stages if s.get("state") in ("executing", "awaiting-approval")), None)
        if not current:
            continue
        forecast = next((f.get("date") for f in (header or {}).get("forecast_end") or []
                         if isinstance(f, dict) and f.get("stage") == current["number"]), None)
        if not forecast:
            continue
        try:
            tolerance = duration((pid_document(project["id"]).header.get("tolerances") or {}).get("time"))
            planned = util.parse_time(str(current["planned_end"]))
            forecast_at = util.parse_time(str(forecast))
        except (util.YggError, ValueError):
            continue
        if forecast_at <= planned + tolerance:
            continue
        key = "stage-" + str(current["number"]) + "@" + util.iso(forecast_at)
        if any(i.get("kind") == "exception-forecast" and i.get("forecast_key") == key
               for i in tracker.issues(project["id"])):
            continue
        with util.locked():
            identifier = tracker.allocate("issue")
            record = tracker.new_issue(project["id"], identifier, "exception-forecast", ["exception-forecast"],
                                       None, to=project["pm"], forecast_key=key, stage=current["number"],
                                       reason="route map forecast " + util.iso(forecast_at) +
                                       " is later than planned end plus tolerance")
            record.save()
            state.alert("ygg-blocker-forecast", "Forecast drift on " + project["id"] + " stage " +
                        str(current["number"]), project=project["id"], record=identifier)
        state.wake(project["pm"], "forecast-drift")


def timebox_ends() -> None:
    from . import journal, ratatoskr
    from .commands import pid_document
    moment = util.now()
    for project in tracker.projects():
        if project.get("state") not in ("executing", "awaiting-approval", "exception"):
            continue
        try:
            header = pid_document(project["id"]).header
        except util.YggError:
            continue
        from .status import delivery_view, timebox_hours
        hours = timebox_hours(project["id"])
        if delivery_view(project["id"], header).get("cadence") != "timebox" or not isinstance(hours, int) or hours < 1:
            continue
        stage = next((s for s in tracker.milestones(project["id"]).get("stages") or []
                      if s.get("state") == "executing" and s.get("started_at")), None)
        if not stage:
            continue
        start = util.parse_time(stage["started_at"])
        ended = int((moment - start).total_seconds() // (hours * 3600))
        trusted, _ = journal.trusted_entries(project["programme"])
        signalled = {e.get("key") for e in trusted if e.get("kind") == "timebox-signal"}
        nodes = state.all_nodes()
        for number in range(1, ended + 1):
            key = project["id"] + ":" + str(stage["number"]) + ":" + str(number)
            if key in signalled:
                continue
            journal.add(project["programme"], "timebox-signal", key=key, cleared_at=util.iso())
            body = "stage " + str(stage["number"]) + " timebox " + str(number) + " ended"
            ratatoskr.send("l1", project["pm"], "timebox-end", body, internal=True)
            for node in nodes.values():
                if node.get("project") == project["id"] and node["role"] == "su":
                    ratatoskr.send("l1", node["id"], "timebox-demo", body, internal=True)
                if node.get("project") == project["id"] and node["role"] == "tl":
                    state.wake(node["id"], "timebox-checkpoint")


def _envelope_ended(envelope: dict, moment) -> bool:
    """A timed envelope ends at `until`; a project envelope granted `--through-stage n` ends once a stage
    after n is current (executing or awaiting approval) or the project is closed."""
    until = envelope.get("until")
    if until:
        return util.parse_time(until) <= moment
    through = envelope.get("through_stage")
    scope = envelope.get("scope")
    if through is None or not scope:
        return False
    project = tracker.project(scope)
    if not project:
        return False
    if project.get("state") == "closed":
        return True
    stages = tracker.milestones(scope).get("stages") or []
    current = next((s for s in stages if s.get("state") in ("executing", "awaiting-approval")), None)
    return current is not None and int(current["number"]) > int(through)


def envelope_expiries() -> None:
    from . import envelopes, journal
    moment = util.now()
    for programme in tracker.programmes():
        bases = [tracker.root() / "initiatives" / programme["id"] / "envelopes"]
        bases += [tracker.project_dir(p["id"]) / "envelopes" for p in tracker.projects()
                  if p.get("programme") == programme["id"]]
        trusted, _ = journal.trusted_entries(programme["id"])
        signalled = {e.get("key") for e in trusted if e.get("kind") == "envelope-ended"}
        for base in bases:
            for envelope in envelopes._envelopes(base):
                if envelope.get("state") != "active" or not _envelope_ended(envelope, moment):
                    continue
                if envelope["id"] in signalled:
                    continue
                # The fact is the obligation: every reviewer of the programme owes a launch for it until a
                # reviewer launch given it is confirmed (which clears it).
                journal.add(programme["id"], "envelope-ended", key=envelope["id"])


def tick() -> None:
    # Checkpoint batches are not queued here: a due report is owed work, derived on every read.
    timebox_ends()
    envelope_expiries()


def after_launches() -> None:
    forecast_drift()
