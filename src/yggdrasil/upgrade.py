"""One-way upgrade of records written before the state-core rebuild, and compaction of settled stamps.

Legacy journal kinds are rewritten into their current form, one programme journal per durable write, so
no obligation and no interlock is lost:

- an uncleared `wake` (a queued wake) becomes an `event` fact with the same node and reason;
- an uncleared `held-launch` (a stored hold) becomes an `event` fact owing the node a launch: holds are
  derived now (capacity from exit facts, load from the sample);
- an uncleared `pause` or `stop` without an intent id is re-issued with a fresh intent id, so a resume
  can list it;
- an `uncertain` entry is cleared: uncertainty is derived from lifecycle facts and fresh evidence.

Legacy state-root stores the rebuild replaced (`wakes.json`, `stops.json`, `uncertain.json`, the `pause*` files) are
deleted: nothing reads them. Every step is idempotent and does nothing once the records are current.
"""

from __future__ import annotations

import uuid

from . import state, util


def journal_entries() -> int:
    """Rewrite the legacy kinds in every journal; returns the number of entries upgraded."""
    from . import journal
    upgraded = 0
    with util.locked():
        for programme in journal.all_programmes():
            items = journal.entries(programme)
            trusted, _ = journal.trusted_entries(programme, items)  # the same objects, filtered
            legacy = [e for e in trusted if not e.get("cleared_at") and _legacy(e)]
            if not legacy:
                continue
            moment = util.iso()
            added = []
            for item in legacy:
                item["cleared_at"] = moment
                if item["kind"] != "uncertain":  # uncertainty is derived from lifecycle facts: no replacement
                    added.append(_current_form(item, moment))
                upgraded += 1
            journal.save(programme, items + added)
    return upgraded


def _legacy(entry: dict) -> bool:
    kind = entry.get("kind")
    return kind in ("wake", "held-launch", "uncertain") or (kind in ("pause", "stop") and not entry.get("intent"))


def _current_form(entry: dict, moment: str) -> dict:
    kind = entry["kind"]
    if kind in ("pause", "stop"):
        return {"kind": kind, "at": moment, "actor": "ygg-app", "scope": entry.get("scope") or "*",
                "intent": "si-" + uuid.uuid4().hex[:16], "upgraded_from": entry.get("at")}
    reason = state.decode_reason(str(entry.get("reason") or "")) if kind == "wake" else "held-launch"
    return {"kind": "event", "at": moment, "actor": "ygg-app", "event_id": "e-" + uuid.uuid4().hex[:12],
            "node": entry.get("node"), "reason": state.encode_reason(reason or "upgraded-wake"),
            "upgraded_from": kind}


def state_root_stores() -> None:
    """Delete the state-root stores the rebuild replaced."""
    root = state.root()
    for path in [root / "wakes.json", root / "stops.json", root / "uncertain.json", root / "pause",
                 *root.glob("pause-*")]:
        if path.is_file():
            path.unlink()
            util.fsync_dir(path.parent)


def compact_stamps() -> int:
    """Drop the obligation stamps on tracker records that their node has consumed (an activation given the
    stamp was spawned and did not end aborted). Owed work is unchanged: a consumed stamp owes nothing, and a
    consumed lane dispatch stamp is never delivered. One write per record that changes, each under the
    journal lock with the record re-read first. Returns the number of stamps dropped."""
    from . import reducer, tracker
    activations: dict = {}
    for programme_fold in reducer.folds().values():
        activations.update(programme_fold["activations"])
    consumed, _ = reducer.consumption(activations)

    def settled(stamp) -> bool:
        if not isinstance(stamp, dict) or not stamp.get("id") or not stamp.get("node"):
            return False
        return (str(stamp["node"]), "record:" + str(stamp["id"])) in consumed

    dropped = 0
    with util.locked():
        for project in tracker.projects():
            project_id = project["id"]
            current = tracker.project(project_id) or {}
            kept = [s for s in current.get("owed") or [] if not settled(s)]
            if len(kept) != len(current.get("owed") or []):
                dropped += len(current["owed"]) - len(kept)
                current["owed"] = kept
                tracker.save_project(current)
            milestones = tracker.milestones(project_id)
            kept = [s for s in milestones.get("owed") or [] if not settled(s)]
            if len(kept) != len(milestones.get("owed") or []):
                dropped += len(milestones["owed"]) - len(kept)
                milestones["owed"] = kept
                tracker.save_milestones(project_id, milestones)
            for issue in tracker.issues(project_id):
                kept = [s for s in issue.header.get("owed") or [] if not settled(s)]
                if len(kept) != len(issue.header.get("owed") or []):
                    dropped += len(issue.header["owed"]) - len(kept)
                    issue.header["owed"] = kept
                    issue.save()
            text = tracker.read_document(project_id, "backlog")
            if text:
                doc = tracker.parse_issue(project_id, "backlog", text)
                kept = [s for s in doc.header.get("owed") or [] if not settled(s)]
                if len(kept) != len(doc.header.get("owed") or []):
                    dropped += len(doc.header["owed"]) - len(kept)
                    doc.header["owed"] = kept
                    from .commands import _render_doc
                    tracker.write_text(tracker.document_path(project_id, "backlog"), _render_doc(doc))
    return dropped


COMPACT_EVERY_SECONDS = 3600
COMPACTED_AT = "compacted-at"


def compaction_due() -> bool:
    """Whether the tick should compact now: at most once an hour, by a timestamp in the safety folder
    (it survives a state-root wipe)."""
    from . import safety
    text = util.read_text(safety.root() / COMPACTED_AT).strip()
    try:
        return not text or (util.now() - util.parse_time(text)).total_seconds() >= COMPACT_EVERY_SECONDS
    except ValueError:
        return True


def compact_journal() -> int:
    """Clear the event facts, checkpoint batch entries and ended envelopes that a consuming terminal
    outcome settled (an activation given them was spawned and ended without being aborted, interrupted or
    held as ambiguous). Owed work is unchanged: a consumed fact owes nothing either way. Done here, never at
    launch confirmation, so a session stopped after confirming still leaves its work owed. One write per
    journal that changes. Returns the number of entries cleared."""
    from . import events, journal, reducer
    activations: dict = {}
    for programme_fold in reducer.folds().values():
        activations.update(programme_fold["activations"])
    consumed, _ = reducer.consumption(activations)

    def settled(entry: dict) -> bool:
        kind = entry.get("kind")
        if kind == "event":
            return (entry.get("node"), "event:" + str(entry.get("event_id"))) in consumed
        if kind == "envelope-ended":
            return ("*", "envelope:" + str(entry.get("key"))) in consumed
        if kind == "checkpoint" and entry.get("msg_id"):
            pm, msg = entry.get("pm"), str(entry["msg_id"])
            return (pm, "checkpoint:" + msg) in consumed or (pm, "message:" + msg) in consumed
        return False

    cleared = 0
    with util.locked():
        for programme in journal.all_programmes():
            pending = [e for e in journal.entries(programme) if not e.get("cleared_at") and settled(e)]
            if pending:
                cleared += len(pending)
                journal.clear_where(programme, lambda e: settled(e))
        local = util.read_json(events.checkpoints_path(), {}) or {}
        kept = {pm: [i for i in items if not ((pm, "checkpoint:" + str(i.get("msg_id"))) in consumed or
                                              (pm, "message:" + str(i.get("msg_id"))) in consumed)]
                for pm, items in local.items()}
        kept = {pm: items for pm, items in kept.items() if items}
        if kept != local:
            util.write_json(events.checkpoints_path(), kept)
    return cleared


def run(*, tick: bool = False) -> None:
    """Upgrade, delete the replaced stores, compact settled stamps; each step tolerates an unwritable
    tracker (the next run repeats it). On a conductor tick, compaction runs at most once an hour."""
    for step in (journal_entries, state_root_stores):
        try:
            step()
        except util.YggError:
            pass
    if tick and not compaction_due():
        return
    try:
        compact_stamps()
        compact_journal()
    except util.YggError:
        return
    if tick:
        from . import safety
        util.atomic_write(safety.layout() / COMPACTED_AT, util.iso() + "\n")
