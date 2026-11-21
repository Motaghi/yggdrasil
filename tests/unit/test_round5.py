"""Unit tests for the round-5 review findings (dev #2 pair review and QA)."""

import shutil
import threading
import time

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (fixture and helpers)
from test_round3 import receipt_file, uncleared


# 1: an abort interrupted after its runtime step (no wake yet) is finished by reconcile, wake first.
def test_interrupted_abort_after_runtime_step_restores_wake(world):
    from yggdrasil import conductor, launcher, state
    receipt_file("act-0042", outcome="pending", phase="starting", pid=None, abort_step="requested",
                 abort_reason="paused")
    state.save_runtime("tl1", {"state": "idle", "activation": "act-0042", "outcome": "aborted"})
    conductor.reconcile()
    data = launcher.receipt("act-0042")
    assert data["outcome"] == "aborted" and data.get("abort_step") is None
    assert "tl1" in state.wakes() and "tl1" in conductor.candidates()


# 1: an abort interrupted right after the request (runtime still pending) is finished the same way.
def test_interrupted_abort_after_request_is_completed(world):
    from yggdrasil import conductor, launcher, state
    receipt_file("act-0042", outcome="pending", phase="starting", pid=None, abort_step="requested",
                 abort_reason="blocked by D-0001")
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "pending"})
    assert launcher.hold_reason(state.all_nodes()["tl1"], "act-0042") == "aborted"  # no agent may start
    conductor.reconcile()
    assert state.runtime("tl1")["state"] == "idle" and state.runtime("tl1")["outcome"] == "aborted"
    assert launcher.receipt("act-0042")["outcome"] == "aborted" and "tl1" in state.wakes()


# 2: a failed process creation consumes nothing, so wake repair still covers its messages.
def test_spawn_failed_receipt_consumes_nothing(world):
    from yggdrasil import ratatoskr, rehydrate, state
    msg = ratatoskr.send("pm1", "tl1", "note", "never delivered")
    receipt_file("act-0042", outcome="confirmed", pid=None, exited_at=util.iso(), exit_code=127, spawn_failed=True,
                 consumed={"messages": [msg], "wakes": ["message:" + msg]})
    rehydrate.replay_journal()
    assert "tl1" in state.wakes()


# 3: a retry aborted at the final gate keeps its retry context, so the next launch keeps the count.
def test_an_aborted_retry_launch_keeps_the_retry_owed(world, monkeypatch):
    from yggdrasil import conductor, launcher, state
    from yggdrasil import lifecycle
    from test_rebuild_t4 import SPAWNED
    lifecycle.commit("tl1", "act-0009", "reserved", launcher_pid=1, launcher_started_at="x", work={}, retries_used=1)
    lifecycle.commit("tl1", "act-0009", *SPAWNED[:1], **SPAWNED[1])
    lifecycle.commit("tl1", "act-0009", "exited", exit_code=1)
    lifecycle.commit("tl1", "act-0042", "reserved", launcher_pid=1, launcher_started_at="x",
                     work={"retries": ["act-0009"]}, retries_used=2)
    receipt_file("act-0042", outcome="pending", phase="starting", pid=None, lifecycle=True)
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "pending",
                               "retries_used": 2})
    with util.locked():
        launcher.abort_activation("tl1", "act-0042", "blocked by D-0001")
    runtime = state.runtime("tl1")
    # The aborted retry ran nothing: the retry of act-0009 is owed again, with its count.
    assert runtime["state"] == "idle" and "tl1" in state.wakes()
    calls = []
    monkeypatch.setattr(conductor, "admit", lambda: (True, "ok"))
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: calls.append((node, kw)) or {"started": True})
    conductor.launch_queue([{"node": "tl1"}])
    assert calls == [("tl1", {"explicit": False, "retries_used": 2})]


# 4: timebox_hours comes from the same layers as the cadence, in status and in PID validation.
def test_layered_timebox_hours(world):
    import pytest
    from yggdrasil import commands, status, tracker
    programme = tracker.programme("alpha")
    programme["config"] = {"delivery_approach": "agile", "cadence": "timebox"}
    tracker.save_programme(programme)
    clear_caches()
    with pytest.raises(util.YggError) as error:
        commands.validate_delivery("p1")
    assert "timebox_hours" in error.value.message
    programme["config"]["timebox_hours"] = 8
    tracker.save_programme(programme)
    clear_caches()
    commands.validate_delivery("p1")
    view = status.delivery_view("p1", None)
    assert view["cadence"] == "timebox" and view["timebox"]["number"] == 1


# 5: retries deferred by the tick cap are journalled and come back as queued work after rehydration.
def test_cap_deferred_retries_survive_rehydration(world, monkeypatch):
    from yggdrasil import conductor, launcher, rehydrate, state, tracker
    from test_rebuild_t4 import SPAWNED, given
    for node in ("tl1", "su1"):
        activation = tracker.allocate("activation")
        given(node, activation, SPAWNED, ("exited", {"exit_code": 1}))
        receipt_file(activation, node=node, pid=999999, process_started_at="x", outcome="confirmed",
                     started_at="2026-11-16T00:00:00Z", exited_at="2026-11-16T00:00:01Z", exit_code=1, retries_used=0,
                     lifecycle=True)
        state.save_runtime(node, {"state": "running", "activation": activation, "outcome": "confirmed"})
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nsettle_delay_seconds: 0\n"
                                       "tick_cap: count\nmax_launches_per_tick: 1\n")
    clear_caches()
    calls = []
    monkeypatch.setattr(conductor, "admit", lambda: (True, "ok"))
    monkeypatch.setattr(conductor, "candidates", lambda: [])
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: calls.append((node, kw)) or {"started": True})
    result = conductor.tick()
    assert len(result["launched"]) == 1
    deferred = ({"tl1", "su1"} - set(result["launched"])).pop()
    assert not uncleared("held-launch")  # nothing is stored: the retry is owed from the exit fact
    shutil.rmtree(world / "state")
    rehydrate.replay_journal()
    assert state.runtime(deferred)["state"] == "idle" and deferred in state.wakes()
    calls.clear()
    conductor.launch_queue([{"node": deferred}])
    assert calls[0][1]["retries_used"] == 1


# 6: a process that exits during confirm is classified from its recorded exit, not as a failure.
def test_exit_during_confirm_keeps_its_class(world):
    from yggdrasil import launcher, state
    for code, tail, expected in ((0, "", "confirmed"), (1, "quota exceeded", "capacity")):
        activation = "act-004" + str(code)
        receipt_file(activation, outcome="pending", phase="starting", pid=999999, process_started_at="x")
        state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "pending"})

        class Supervisor:
            done = False

            def poll(self):
                return 0 if self.done else None

        supervisor = Supervisor()

        def record_exit(activation=activation, code=code, tail=tail):
            time.sleep(0.3)
            launcher.update_receipt(activation, exited_at=util.iso(), exit_code=code, stderr_tail=tail)
            supervisor.done = True

        threading.Thread(target=record_exit).start()
        result = launcher.confirm("tl1", activation, supervisor)
        assert result["outcome"] == expected
    assert state.runtime("tl1")["outcome"] == "confirmed"


# 6: finalize re-classifies the fresh receipt under the lock.
def test_finalize_uses_fresh_exit_record(world):
    from yggdrasil import launcher, state
    receipt_file("act-0042", outcome="pending", pid=999999, exited_at=util.iso(), exit_code=0)
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "pending"})
    assert launcher.finalize("tl1", "act-0042", "failed") == "confirmed"
    assert state.runtime("tl1")["state"] == "idle"


# 7: the time cap counts from the start of the tick, so preflight work uses the budget.
def test_time_cap_counts_from_tick_start(world, monkeypatch):
    from yggdrasil import conductor, events, launcher, rehydrate
    clock = {"now": 0.0}
    monkeypatch.setattr(conductor.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(conductor.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    (world / "config.yaml").write_text("tick_cap: time\ntick_budget_seconds: 10\nsettle_delay_seconds: 1\n")
    clear_caches()
    monkeypatch.setattr(rehydrate, "replay_journal", lambda: 0)
    monkeypatch.setattr(conductor, "heimdall_scan", lambda: ([], []))
    monkeypatch.setattr(events, "tick", lambda: clock.__setitem__("now", clock["now"] + 20))
    monkeypatch.setattr(events, "after_launches", lambda: None)
    monkeypatch.setattr(conductor, "candidates", lambda: ["pm1", "tl1", "su1"])
    monkeypatch.setattr(conductor, "admit", lambda: (True, "ok"))
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: {"started": True})
    assert conductor.tick()["launched"] == ["pm1"]  # the first launch is never deferred
