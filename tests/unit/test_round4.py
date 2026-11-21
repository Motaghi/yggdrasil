"""Unit tests for the round-4 review findings (dev #2 pair review and QA)."""

import shutil

import pytest

from yggdrasil import util
from test_round2 import clear_caches, package, world  # noqa: F401  (fixture and helpers)
from test_round3 import receipt_file, uncleared


# 1: an interrupted cancellation (aborted receipt written, runtime not yet) is finished by replay and reconcile.
def test_aborted_receipt_resolves_interrupted_cancellation(world):
    from yggdrasil import conductor, rehydrate, state, tracker
    activation = tracker.allocate("activation")
    receipt_file(activation, outcome="aborted", phase="aborted", pid=None, exited_at=util.iso(), exit_code=None,
                 abort_reason="paused")
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "uncertain"})
    rehydrate.replay_journal()
    conductor.reconcile()
    runtime = state.runtime("tl1")
    assert runtime["state"] == "idle" and runtime["outcome"] == "aborted"
    assert "tl1" in state.wakes() and "tl1" in conductor.candidates()


# 1: the same through reconcile alone, for a pending runtime left by an interrupted stop.
def test_aborted_receipt_repairs_pending_runtime_and_keeps_stop(world):
    from yggdrasil import conductor, state, tracker
    activation = tracker.allocate("activation")
    receipt_file(activation, outcome="aborted", phase="aborted", pid=None, exited_at=util.iso(), abort_reason="stopped")
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "pending"})
    conductor.reconcile()
    assert state.runtime("tl1")["state"] == "stopped" and state.runtime("tl1")["outcome"] == "aborted"


# 2: a confirmed capacity receipt with a pending runtime is repaired to held-capacity, never a liveness retry.
def test_pending_runtime_repair_honours_capacity_class(world):
    from yggdrasil import conductor, state, tracker
    activation = tracker.allocate("activation")
    receipt_file(activation, outcome="confirmed", phase="launched", **{"class": "capacity"}, pid=999999,
                 exited_at="2026-11-16T00:00:01Z", exit_code=1, started_at="2026-11-16T00:00:00Z",
                 stderr_tail="429 rate limit")
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "pending"})
    flagged, relaunch = conductor.heimdall_scan()
    runtime = state.runtime("tl1")
    assert runtime["state"] == "idle" and runtime["outcome"] == "confirmed"  # its hold is derived from facts
    assert relaunch == [] and flagged == []


# 4: WIP enforcement follows the effective layered delivery settings (programme config here).
def test_wip_enforcement_uses_layered_programme_delivery(world):
    from yggdrasil import commands, status, tracker
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\ndelivery_approach_default: waterfall\n")
    programme = tracker.programme("alpha")
    programme["config"] = {"delivery_approach": "agile", "cadence": "flow", "wip_limit": 1}
    tracker.save_programme(programme)
    clear_caches()
    assert status.effective_delivery("p1")[0] == "agile"
    package("WP-1")
    commands.tracker_write("pm1", "p1", "work-package", "Build", stage="1", package="WP-2")
    commands.tracker_write("su1", "p1", "criteria", "Test", package="WP-2")
    with pytest.raises(util.YggError) as error:
        commands.wp_authorise("p1", "WP-2", "tl1")
    assert "wip-limit" in error.value.message


# 5: retries held by load are persisted, shown in status, and survive rehydration with their retry count.
def test_load_held_retries_persist_and_survive_rehydration(world, monkeypatch):
    from yggdrasil import conductor, launcher, rehydrate, state, status, tracker
    from test_rebuild_t4 import SPAWNED, given
    for node in ("tl1", "su1"):
        activation = tracker.allocate("activation")
        given(node, activation, SPAWNED, ("exited", {"exit_code": 1}))
        receipt_file(activation, node=node, pid=999999, process_started_at="x", outcome="confirmed",
                     started_at="2026-11-16T00:00:00Z", exited_at="2026-11-16T00:00:01Z", exit_code=1, retries_used=0,
                     lifecycle=True)
        state.save_runtime(node, {"state": "running", "activation": activation, "outcome": "confirmed"})
    calls = []
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: calls.append((node, kw)) or {"started": True})
    monkeypatch.setattr(conductor, "admit", lambda: (False, "load 0.95 above 0.85"))
    result = conductor.tick()
    assert {"su1", "tl1"} <= set(result["held"]) and calls == []
    view = status.build_status()
    assert {"su1", "tl1"} <= set(view["held_launches"])
    assert {n["id"]: n["state"] for n in view["nodes"]}["tl1"] == "held-load"
    assert not uncleared("held-launch")  # nothing is stored: the retry is owed from the exit fact
    shutil.rmtree(world / "state")
    rehydrate.replay_journal()
    monkeypatch.setattr(conductor, "admit", lambda: (True, "load 0.1"))
    conductor.tick()
    retried = {node: kw for node, kw in calls if node in ("su1", "tl1")}
    assert sorted(retried) == ["su1", "tl1"]
    assert all(kw.get("retries_used") == 1 for kw in retried.values())


# 6: a decision blocker persisted during preparation aborts the launch right before the agent starts.
def test_decision_blocker_during_preparation_aborts_launch(world, monkeypatch):
    from yggdrasil import launcher, state, tracker
    real = launcher.working_directory

    def preparing(node, nodes):
        path = real(node, nodes)
        tracker.new_issue("p1", "D-0001", "decision", [], "pm1", question="Hold?",
                          scope={"type": "lane", "id": "tl1"}).save()
        return path

    monkeypatch.setattr(launcher, "working_directory", preparing)
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1")
    assert error.value.code == util.EXIT_REFUSED and "blocked by D-0001" in error.value.message
    data = launcher.receipt("act-0001")
    assert data["node"] == "tl1" and data["outcome"] == "aborted" and data["pid"] is None
    assert not (world / "agent.log").exists()
    assert "tl1" in state.wakes()  # requeued: starts once the decision is answered


# 7: a failed process creation is not a start: it uses no slot and is reported as failed.
def test_failed_process_creation_uses_no_launch_slot(world, monkeypatch):
    from yggdrasil import conductor, launcher
    monkeypatch.setenv("YGG_CLAUDE_BIN", str(world / "missing-claude"))
    result = launcher.launch("tl1")
    assert result["outcome"] == "failed" and result["started"] is False
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nsettle_delay_seconds: 0\n"
                                       "tick_cap: count\nmax_launches_per_tick: 1\n")
    clear_caches()
    outcomes = {"pm1": {"started": False}, "su1": {"started": True}}
    monkeypatch.setattr(conductor, "admit", lambda: (True, "ok"))
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: outcomes[node])
    output = conductor.launch_queue([{"node": "pm1"}, {"node": "su1"}])
    assert output == {"launched": ["su1"], "held": [], "failed": ["pm1"]}


# 8: Sol reviewers get the brief verbatim; a team lead is told where it is to pass it to its Sol pair.
def test_brief_reaches_sol_reviewer_and_team_lead_pointer(world):
    from yggdrasil import commands, launcher, state, tracker
    tracker.write_document("p1", "brief", "# Brief\n\nGoal: ship it.\n")
    commands.node_add("tl1w1", "worker", "tl1")
    lead = launcher.prompt_for(state.all_nodes()["tl1"], "act-0001")
    assert str(tracker.document_path("p1", "brief")) in lead and "Goal: ship it." not in lead
    assert "Goal: ship it." not in launcher.prompt_for(state.all_nodes()["tl1w1"], "act-0002")
    programme = tracker.programme("alpha")
    programme["models"] = {"model": "gpt-6.1-sol", "effort": "xhigh"}
    tracker.save_programme(programme)
    assert "Goal: ship it." in launcher.prompt_for(state.all_nodes()["tl1w1"], "act-0003")
