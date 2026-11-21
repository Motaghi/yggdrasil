"""Unit tests for the round-17 review findings: per-report checkpoint wakes and outstanding retries."""

import shutil

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (fixture and helpers)
from test_round3 import receipt_file, uncleared


def checkpoint_wakes(state):
    owed = state.wakes().get("pm1") or {}
    return [r for r in owed.get("reasons") or [] if r.startswith("checkpoints")]


# 1 (major): deliver A, capture A in a started PM activation, deliver B and queue its wake, ack B, confirm A.
# No checkpoint wake is left, locally or in the journal, and none comes back on recovery.
def test_ack_and_overlapping_confirmation_leave_no_checkpoint_wake(world):
    from yggdrasil import events, ratatoskr, rehydrate, state, upgrade
    from test_rebuild_t4 import SPAWNED, given
    first = ratatoskr.send("tl1", "pm1", "checkpoint", "A")
    given("pm1", "act-0042", SPAWNED, messages=[first])  # running: the first report is reserved
    second = ratatoskr.send("su1", "pm1", "checkpoint", "B")
    assert "checkpoints:" + second in checkpoint_wakes(state)
    ratatoskr.inbox("pm1", ack=second)
    from yggdrasil import lifecycle
    lifecycle.commit("pm1", "act-0042", "exited", exit_code=0)
    upgrade.compact_journal()
    assert checkpoint_wakes(state) == []
    assert "pm1" not in (util.read_json(events.checkpoints_path(), {}) or {})
    shutil.rmtree(world / "state")
    rehydrate.replay_journal()
    assert checkpoint_wakes(state) == []


# 1: confirmation cancels the wakes of the reports in its snapshot, even one queued during the run.
def test_confirmation_cancels_the_wakes_of_its_snapshot_reports(world):
    from yggdrasil import ratatoskr, state
    from test_rebuild_t4 import SPAWNED, given
    first = ratatoskr.send("tl1", "pm1", "checkpoint", "A")
    given("pm1", "act-0042", SPAWNED, ("exited", {"exit_code": 0}), messages=[first])
    second = ratatoskr.send("su1", "pm1", "checkpoint", "B")
    assert "checkpoints:" + first not in checkpoint_wakes(state)
    assert "checkpoints:" + second in checkpoint_wakes(state)


# 1 (QA): a report arriving after an older one expired does not let the old wake launch the incomplete
# batch before the new report's own deadline, even after recovery.
def test_newer_report_waits_for_its_own_deadline_after_the_old_one_is_acked(world, monkeypatch):
    from yggdrasil import commands, conductor, ratatoskr, state
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nload_max_age_seconds: 999999999\n")
    clear_caches()
    commands.node_add("tl2", "tl", "pm1")
    commands.node_add("su2", "su", "pm1", "tl2")
    for wp, lane, su in (("WP-1", "tl1", "su1"), ("WP-2", "tl2", "su2")):
        commands.tracker_write("pm1", "p1", "work-package", "Build " + wp, package=wp, stage="1")
        commands.tracker_write(su, "p1", "criteria", "Test " + wp, package=wp)
        commands.wp_authorise("p1", wp, lane)
    for _ in range(6):
        conductor.tick()
    log = world / "agent.log"
    log.unlink(missing_ok=True)
    old = ratatoskr.send("tl1", "pm1", "checkpoint", "old")
    monkeypatch.setenv("YGG_NOW", "2026-11-16T06:01:00Z")
    (world / "load").write_text("0.95")
    conductor.tick()  # the old report is due; load holds the PM
    monkeypatch.setenv("YGG_NOW", "2026-11-16T06:02:00Z")
    ratatoskr.send("tl1", "pm1", "checkpoint", "new")
    ratatoskr.inbox("pm1", ack=old)
    assert checkpoint_wakes(state) == []
    (world / "load").write_text("0.1")
    conductor.tick()
    shutil.rmtree(world / "state")
    conductor.tick()
    assert not (log.exists() and "pm1" in log.read_text().split())


# 2: a hold whose retry is still pending (retry_of set) survives the acknowledgement.
def test_ack_keeps_a_hold_with_a_pending_retry(world):
    from yggdrasil import journal, ratatoskr, state
    msg = ratatoskr.send("pm1", "tl1", "note", "hello")
    journal.add("alpha", "held-launch", node="tl1", reason="load 0.95", retry_of="act-0041", retries_used=1)
    state.save_runtime("tl1", {"state": "held-load", "held": "load 0.95", "retry_of": "act-0041", "retries_used": 1})
    ratatoskr.inbox("tl1", ack=msg)
    assert state.runtime("tl1")["state"] == "held-load"
    assert uncleared("held-launch", node="tl1")
