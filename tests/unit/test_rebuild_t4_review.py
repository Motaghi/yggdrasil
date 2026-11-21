"""Review fixes for the lifecycle slice and derived owed work: reset evidence, unknown evidence after a
wipe, retry decisions, record-backed obligations, node-scoped consumption."""

import os
import shutil
import subprocess

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)
from test_round3 import receipt_file, uncleared
from test_rebuild_t3 import seed
from test_rebuild_t4 import SPAWNED, given, owed_ids


def test_reset_refuses_an_agent_with_unknown_evidence_and_settles_nothing(world, monkeypatch):
    from yggdrasil import launcher, lifecycle, noderepair, state
    identifier = seed("tl1", "--phase", "spawned", "--process", "live")
    agent = launcher.receipt(identifier)["pid"]
    real = util.process_start
    try:
        # The agent is alive but its start time cannot be read right now.
        monkeypatch.setattr(util, "process_start", lambda pid: None if pid == agent else real(pid))
        state.set_state("tl1", "stalled-suspect")
        with pytest.raises(util.YggError) as error:
            noderepair.reset("tl1", kill=True)
        assert error.value.code == util.EXIT_UNCERTAIN and "unknown" in error.value.message
        assert util.pid_alive(agent)
        assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) is None
    finally:
        os.killpg(agent, 9)


def test_reset_refuses_when_the_orphan_scan_cannot_answer(world, monkeypatch):
    from yggdrasil import lifecycle, noderepair, state
    identifier = seed("tl1", "--phase", "spawned")  # no agent recorded, supervisor dead
    monkeypatch.setattr(lifecycle, "orphan_agents", lambda *a, **k: ("unknown", []))
    state.set_state("tl1", "stalled-suspect")
    with pytest.raises(util.YggError) as error:
        noderepair.reset("tl1", kill=True)
    assert error.value.code == util.EXIT_UNCERTAIN
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) is None


def test_reset_kills_an_unrecorded_agent_before_settling(world, monkeypatch):
    from yggdrasil import lifecycle, noderepair, state
    identifier = seed("tl1", "--phase", "spawned")
    orphan = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        monkeypatch.setattr(lifecycle, "orphan_agents",
                            lambda *a, **k: ("ok", [orphan.pid] if orphan.poll() is None else []))
        state.set_state("tl1", "stalled-suspect")
        with pytest.raises(util.YggError):
            noderepair.reset("tl1")  # without --kill a live unrecorded agent refuses
        noderepair.reset("tl1", kill=True)
        orphan.wait(timeout=10)
        [end] = [e for e in lifecycle.facts("alpha", identifier) if e["phase"] == "exited"]
        assert end["reset_kill"] is True
    finally:
        if orphan.poll() is None:
            orphan.kill()


def test_unknown_evidence_stays_visible_after_the_activation_files_are_wiped(world, monkeypatch):
    from yggdrasil import reducer, status
    identifier = seed("tl1", "--phase", "spawned")
    shutil.rmtree(world / "state")
    monkeypatch.setenv("YGG_TEST_PROBE", "unknown")
    view = status.build_status()
    assert identifier in view["uncertain_keys"]
    assert reducer.shadow_diff(view) == {}


def test_a_due_retry_is_held_only_by_the_current_load_and_the_blocker_order(world):
    from yggdrasil import reducer, state, status, tracker
    receipt_file("act-0042", pid=999999, outcome="confirmed", exited_at="2026-11-16T00:00:01Z", exit_code=1,
                 started_at="2026-11-16T00:00:00Z", retries_used=0)
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "confirmed",
                               "retry_held": "load 0.95"})  # a stored decision from older code is ignored
    (world / "load").write_text("0.95")
    view = status.build_status()
    assert "tl1" in view["held_launches"] and reducer.shadow_diff(view) == {}
    (world / "load").write_text("0.1")
    view = status.build_status()
    assert "tl1" not in view["held_launches"] and reducer.shadow_diff(view) == {}
    (world / "load").write_text("0.95")
    tracker.new_issue("p1", "D-0001", "decision", [], "pm1", question="Hold?",
                      scope={"type": "lane", "id": "tl1"}).save()
    view = status.build_status()  # a decision comes before load
    assert "tl1" not in view["held_launches"] and reducer.shadow_diff(view) == {}
    assert {n["id"]: n["state"] for n in view["nodes"]}["tl1"] == "blocked"


def test_a_record_change_carries_its_obligation_in_the_same_write(world, monkeypatch):
    from yggdrasil import commands, journal, state, tracker
    from test_round2 import package
    package("WP-1")
    commands.tracker_write("su1", "p1", "criteria", "Test", package="WP-1")
    from yggdrasil import reducer
    criteria = [i["id"] for i in reducer.owed("tl1") if i["kind"] == "record" and i["reason"].startswith("criteria")]
    assert len(criteria) == 1
    issue = tracker.read_issue("p1", "WP-1")
    assert [s["id"] for s in issue.header["owed"] if s["node"] == "tl1" and s["reason"].startswith("criteria")] == criteria
    assert not [e for e in journal.entries("alpha") if e.get("kind") == "event" and e.get("node") == "tl1"
                and "criteria" in state.decode_reason(str(e.get("reason")))]
    # Another node given the same identity consumes nothing for this one.
    given("su1", "act-0041", SPAWNED, ("exited", {"exit_code": 0}), records=criteria)
    assert criteria[0] in owed_ids("tl1")
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 0}), records=criteria)
    assert criteria[0] not in owed_ids("tl1")


def test_reviewer_confirmation_clears_only_the_envelopes_it_was_given(world):
    from yggdrasil import commands, journal, upgrade
    commands.node_add("rv1", "reviewer", "alpha")
    journal.add("alpha", "envelope-ended", key="EN-0001")
    given("rv1", "act-0042", SPAWNED, ("exited", {"exit_code": 0}), envelopes=["EN-0001"])
    journal.add("alpha", "envelope-ended", key="EN-0002")  # ended after the reservation above
    upgrade.compact_journal()
    assert not uncleared("envelope-ended", key="EN-0001")
    assert uncleared("envelope-ended", key="EN-0002")
    assert "EN-0002" in owed_ids("rv1")


def test_a_recovered_exit_fact_keeps_a_failed_creation(world):
    from yggdrasil import conductor, launcher, lifecycle, state
    state.wake("tl1", "criteria:WP-1")
    [event] = owed_ids("tl1")
    given("tl1", "act-0042", SPAWNED, events=[event])
    receipt_file("act-0042", outcome="pending", pid=None, exited_at=util.iso(), exit_code=127, spawn_failed=True,
                 lifecycle=True)
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "pending"})
    conductor._reconcile_lifecycle("tl1", "act-0042", state.runtime("tl1"), launcher.receipt("act-0042"))
    [end] = [e for e in lifecycle.facts("alpha", "act-0042") if e["phase"] == "exited"]
    assert end["spawn_failed"] is True
    assert event in owed_ids("tl1")  # no agent was created: the work stays owed
