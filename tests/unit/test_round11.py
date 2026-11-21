"""Unit tests for the round-11 review findings (dev #2 pair review and QA)."""

import shutil

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (fixture and helpers)
from test_round3 import receipt_file, uncleared


def _setup_held(world, cap):
    from yggdrasil import journal, state
    state.wake("pm1", "message")
    state.wake("su1", "stage-approved")  # a wake with no inbox message: only the journal records it
    for node in ("pm1", "su1"):
        journal.add("alpha", "held-launch", node=node, reason="load 0.95")
        state.set_state(node, "held-load", held="load 0.95")
    settings = {"count": "tick_cap: count\nmax_launches_per_tick: 1\n",
                "time": "tick_cap: time\ntick_budget_seconds: 0.001\nsettle_delay_seconds: 0\n",
                "both": "tick_cap: both\nmax_launches_per_tick: 1\ntick_budget_seconds: 240\nsettle_delay_seconds: 0\n"}
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\n" + settings[cap])
    clear_caches()


# 1: a cap-deferred wake's load hold becomes a durable queued-wake entry; it survives wipe and rehydrate.
@pytest.mark.parametrize("cap", ["count", "time", "both"])
def test_cap_deferred_wake_survives_rehydrate(world, monkeypatch, cap):
    from yggdrasil import conductor, journal, launcher, rehydrate, state
    _setup_held(world, cap)
    monkeypatch.setattr(conductor, "admit", lambda: (True, "load 0.1"))
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: {"started": True})
    writes = []
    real = journal.save
    monkeypatch.setattr(journal, "save", lambda programme, items: writes.append(1) or real(programme, items))
    state.set_state("pm1", "idle")
    # A tick that has already spent its time budget (time and both caps) still makes its first launch.
    started = 0.0 if cap == "time" else None
    result = conductor.launch_queue([{"node": "pm1"}, {"node": "su1"}], started)
    assert result["launched"] == ["pm1"]
    # Round 12: the wake was journalled when it was queued and stays; the cap only drops the held-load status.
    assert [w["reason"] for w in uncleared("event", node="su1")] == ["stage-approved"]
    assert writes == []  # nothing is stored for a deferred or held launch
    shutil.rmtree(world / "state")
    rehydrate.replay_journal()
    assert "stage-approved" in state.wakes()["su1"]["reasons"]
    monkeypatch.setattr(conductor, "candidates", lambda: [n for n in state.wakes() if n == "su1"])
    launched = []
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: launched.append(node) or {"started": True})
    conductor.launch_queue([{"node": node} for node in conductor.candidates()])
    assert launched == ["su1"]


# 2: a cap-deferred liveness retry shows as queued in status, and keeps its retry lineage.
def test_cap_deferred_retry_shows_queued(world, monkeypatch):
    from yggdrasil import conductor, launcher, state, status
    receipt_file("act-0042", pid=999999, outcome="confirmed", exited_at="2026-11-16T00:00:01Z", exit_code=1,
                 started_at="2026-11-16T00:00:00Z", retries_used=0)
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "confirmed"})
    state.wake("pm1", "message")
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\ntick_cap: count\nmax_launches_per_tick: 1\n")
    clear_caches()
    monkeypatch.setattr(conductor, "admit", lambda: (True, "load 0.1"))
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: {"started": True})
    conductor.launch_queue([{"node": "pm1"}, {"node": "tl1", "reason": "relaunch", "retries_used": 1,
                                              "expected_activation": "act-0042"}])
    view = status.build_status()
    assert {n["id"]: n["state"] for n in view["nodes"]}["tl1"] == "queued"
    assert view["held_launches"] == []
