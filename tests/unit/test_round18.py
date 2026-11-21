"""Unit tests for the round-18 review findings: the batch tick enqueues under the cancellation lock, and
every checkpoint report keeps its own deadline."""

import shutil
from datetime import timedelta
from pathlib import Path

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (fixture and helpers)
from test_round3 import uncleared
from test_round17 import checkpoint_wakes

YGG = str(Path(__import__("sys").executable).parent / "ygg")


def two_lanes(world):
    from yggdrasil import commands, conductor
    commands.node_add("tl2", "tl", "pm1")
    commands.node_add("su2", "su", "pm1", "tl2")
    for wp, lane, su in (("WP-1", "tl1", "su1"), ("WP-2", "tl2", "su2")):
        commands.tracker_write("pm1", "p1", "work-package", "Build " + wp, package=wp, stage="1")
        commands.tracker_write(su, "p1", "criteria", "Test " + wp, package=wp)
        commands.wp_authorise("p1", wp, lane)
    for _ in range(6):
        conductor.tick()
    (world / "agent.log").unlink(missing_ok=True)


# 1: inside the lock the tick re-checks each report: an acknowledged or consumed report gets no wake.
def test_batch_tick_skips_acknowledged_and_consumed_reports(world, monkeypatch):
    from yggdrasil import ratatoskr, state
    acked = ratatoskr.send("tl1", "pm1", "checkpoint", "acked")
    given = ratatoskr.send("tl1", "pm1", "checkpoint", "given")
    owed = ratatoskr.send("su1", "pm1", "checkpoint", "owed")
    from yggdrasil import lifecycle
    # A spawned activation was given the second report; the first is acknowledged.
    lifecycle.commit("pm1", "act-0042", "reserved", launcher_pid=1, launcher_started_at="x",
                     work={"messages": [given]})
    lifecycle.commit("pm1", "act-0042", "spawned", supervisor_pid=1, supervisor_started_at="x", agent_nonce="n")
    ratatoskr.inbox("pm1", ack=acked)
    monkeypatch.setenv("YGG_NOW", "2026-11-16T06:01:00Z")
    assert set(checkpoint_wakes(state)) == {"checkpoints:" + owed}


# 2 (QA): a newer report keeps its own deadline. Newer report arrives, a high-load tick runs, the expired
# report is acknowledged: nothing launches the PM before the newer report's deadline, with or without
# recovery; at that deadline it launches once.
def test_newer_report_keeps_its_own_deadline_through_a_held_tick(world, monkeypatch):
    from yggdrasil import conductor, ratatoskr, state
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nload_max_age_seconds: 999999999\n")
    clear_caches()
    two_lanes(world)
    log = world / "agent.log"
    old = ratatoskr.send("tl1", "pm1", "checkpoint", "old")  # 02:00Z
    monkeypatch.setenv("YGG_NOW", "2026-11-16T06:01:00Z")
    (world / "load").write_text("0.95")
    conductor.tick()
    monkeypatch.setenv("YGG_NOW", "2026-11-16T06:02:00Z")
    new = ratatoskr.send("tl1", "pm1", "checkpoint", "new")
    conductor.tick()  # high load: the old report is due, the new one is not
    assert "checkpoints:" + new not in checkpoint_wakes(state)
    ratatoskr.inbox("pm1", ack=old)
    assert checkpoint_wakes(state) == []
    (world / "load").write_text("0.1")
    conductor.tick()
    shutil.rmtree(world / "state")
    conductor.tick()
    assert not (log.exists() and "pm1" in log.read_text().split())
    deadline = util.parse_time("2026-11-16T06:02:00Z") + timedelta(hours=4)
    monkeypatch.setenv("YGG_NOW", util.iso(deadline))
    conductor.tick()
    conductor.tick()
    assert log.read_text().split().count("pm1") == 1


# 2: when every active lane has reported, the whole batch is due at once.
def test_complete_batch_is_due_for_every_report(world):
    from yggdrasil import ratatoskr, state
    first = ratatoskr.send("tl1", "pm1", "checkpoint", "first")
    second = ratatoskr.send("su1", "pm1", "checkpoint", "second")
    assert {"checkpoints:" + first, "checkpoints:" + second} <= set(checkpoint_wakes(state))
    assert uncleared("checkpoint")
