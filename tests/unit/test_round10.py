"""Unit tests for the round-10 review findings (dev #2 pair review and QA)."""

import os

import pytest

from test_round2 import clear_caches, world  # noqa: F401  (fixture and helpers)
from test_round3 import receipt_file, uncleared


# 1: pinning YGG_PROFILE never collapses `..` across a directory symlink.
def test_pinned_profile_keeps_dotdot_through_a_directory_symlink(world, monkeypatch, tmp_path):
    from yggdrasil import launcher
    (tmp_path / "profiles" / "current").mkdir(parents=True)
    (tmp_path / "profiles" / "active.yaml").write_text("platform_pause_dir: /opt/state\n")
    (tmp_path / "cfg").mkdir()
    os.symlink(tmp_path / "profiles" / "current", tmp_path / "cfg" / "link")
    absolute = str(tmp_path / "cfg" / "link" / ".." / "active.yaml")
    monkeypatch.setenv("YGG_PROFILE", absolute)
    clear_caches()
    pinned = launcher.pinned_env()["YGG_PROFILE"]
    assert pinned == absolute
    assert open(pinned).read() == "platform_pause_dir: /opt/state\n"  # still the file the conductor loads
    monkeypatch.chdir(tmp_path / "cfg")
    monkeypatch.setenv("YGG_PROFILE", "link/../active.yaml")
    assert launcher.pinned_env()["YGG_PROFILE"] == os.path.join(os.getcwd(), "link/../active.yaml")


# 2: once load admits again, work the cap defers shows as queued, not held-load (wakes and retries).
@pytest.mark.parametrize("cap", ["count", "time"])
def test_cap_deferred_work_is_queued_after_load_recovers(world, monkeypatch, cap):
    from yggdrasil import conductor, journal, launcher, state, status
    state.wake("pm1", "message")
    state.wake("su1", "message")
    for node in ("pm1", "su1"):
        journal.add("alpha", "held-launch", node=node, reason="load 0.95")
        state.set_state(node, "held-load", held="load 0.95")
    receipt_file("act-0042", pid=999999, outcome="confirmed", exited_at="2026-11-16T00:00:01Z", exit_code=1,
                 started_at="2026-11-16T00:00:00Z", retries_used=0)
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "confirmed",
                               "retry_held": "load 0.95"})
    settings = "tick_cap: count\nmax_launches_per_tick: 1\n" if cap == "count" else \
        "tick_cap: time\ntick_budget_seconds: 0.001\nsettle_delay_seconds: 0\n"
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\n" + settings)
    clear_caches()
    monkeypatch.setattr(conductor, "admit", lambda: (True, "load 0.1"))
    launched = []
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: launched.append(node) or {"started": True})
    state.set_state("pm1", "idle")  # pm1 launches first (its runtime is replaced by the launch)
    queue = [{"node": "pm1"}, {"node": "tl1", "reason": "relaunch", "retries_used": 1,
                                "expected_activation": "act-0042"}, {"node": "su1"}]
    result = conductor.launch_queue(queue, 0.0 if cap == "time" else None)  # time: budget already spent
    assert result["launched"] == ["pm1"] and result["held"] == []
    view = status.build_status()
    states = {n["id"]: n["state"] for n in view["nodes"]}
    # Round 11: a cap-deferred liveness retry shows as queued too, and a deferred wake keeps a durable
    # queued-wake journal entry (the load hold is replaced, never just cleared).
    assert states["su1"] == "queued" and states["tl1"] == "queued"
    assert view["held_launches"] == []
    # Round 12: load and cap never touch durability; the wake was journalled when it was queued.
    assert uncleared("event", node="su1") and "su1" in state.wakes()
    assert state.runtime("tl1")["activation"] == "act-0042"  # nothing about the decision is stored
