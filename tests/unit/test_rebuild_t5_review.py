"""Review fixes for derived holds and record-backed obligations: approvals carried on the milestone record,
lane dispatch owed with the authorisation, retries through a failed creation, explicit launches under a
capacity backoff, holds derived from current load, process evidence first, and the fold cache key."""

import shutil

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)
from test_rebuild_t4 import SPAWNED, given


def test_a_stage_approval_carries_its_obligation_and_transition_on_the_milestone_record(world, monkeypatch):
    from yggdrasil import commands, conductor, reducer, tracker
    data = tracker.milestones("p1")
    data["stages"][0]["state"] = "awaiting-approval"
    tracker.save_milestones("p1", data)
    project = tracker.project("p1")
    project["state"] = "awaiting-approval"
    tracker.save_project(project)
    real = tracker.save_project

    def crash(record):
        raise util.YggError(util.EXIT_TRACKER, "crash after the milestone write")

    monkeypatch.setattr(tracker, "save_project", crash)
    with pytest.raises(util.YggError):
        commands.stage_approve("p1")
    monkeypatch.setattr(tracker, "save_project", real)
    assert tracker.project("p1")["state"] == "awaiting-approval"  # the project write was lost
    assert any(i["reason"] == "stage-approved" for i in reducer.owed("pm1"))
    conductor.recover_records()
    assert tracker.project("p1")["state"] == "executing"
    conductor.recover_records()  # idempotent
    assert tracker.project("p1")["state"] == "executing"


def test_an_authorisation_owes_its_lane_dispatch_and_delivers_it_once(world, monkeypatch):
    from yggdrasil import commands, conductor, journal, ratatoskr, reducer, state
    commands.tracker_write("pm1", "p1", "work-package", "Build", stage="1", package="WP-9")
    commands.tracker_write("su1", "p1", "criteria", "Test", package="WP-9")
    monkeypatch.setattr(ratatoskr, "deliver_dispatches", lambda project_id=None: None)  # crash before sending
    commands.wp_authorise("p1", "WP-9", "tl1")
    monkeypatch.undo()
    assert any(i["reason"] == "dispatch:WP-9" for i in reducer.owed("tl1"))
    conductor.recover_records()
    conductor.recover_records()
    sent = [e for e in journal.entries("alpha") if e.get("kind") == "inbox" and e.get("dispatch")]
    assert len(sent) == 1 and sent[0]["node"] == "tl1"
    assert any("WP-9 authorised" in m["body"] for m in state.inbox("tl1"))


def test_a_failed_creation_during_a_retry_keeps_the_retry_after_a_wipe(world):
    from yggdrasil import reducer
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 1}))
    given("tl1", "act-0043", SPAWNED, ("exited", {"exit_code": 127, "spawn_failed": True}), retries=["act-0042"])
    shutil.rmtree(world / "state")
    retries = [(i["id"], i["retries_used"]) for i in reducer.owed("tl1") if i["kind"] == "retry"]
    assert retries == [("act-0042", 1)]


def test_an_explicit_launch_waits_for_the_capacity_backoff_and_keeps_the_retry_count(world, monkeypatch):
    from yggdrasil import launcher, lifecycle
    from test_rebuild_t5 import capacity_exit
    until = capacity_exit("tl1", "act-0042", retries_used=2)
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1")
    assert error.value.code == util.EXIT_ADMISSION and until in error.value.message
    monkeypatch.setenv("YGG_NOW", until)
    clear_caches()
    launcher.launch("tl1")
    reserved = [lifecycle.phase_fields(found, "reserved") for key, found in lifecycle.all_facts("alpha").items()
                if key != "act-0042"]
    assert [r.get("retries_used") for r in reserved] == [2]


def test_a_live_supervisor_blocks_a_node_without_any_local_record(world):
    import os
    from yggdrasil import conductor, lifecycle, ratatoskr
    me = lifecycle.own_identity()
    lifecycle.commit("tl1", "act-0042", "reserved", launcher_pid=1, launcher_started_at="x", work={})
    lifecycle.commit("tl1", "act-0042", "spawned", supervisor_pid=os.getpid(), supervisor_started_at=me["started_at"],
                     agent_nonce="n")
    shutil.rmtree(world / "state")
    ratatoskr.send("pm1", "tl1", "note", "new work")
    assert "tl1" not in conductor.candidates()
    assert "tl1" not in conductor.launch_holds()


def test_two_tracker_roots_with_equal_revisions_and_last_entries_get_separate_folds(world, tmp_path, monkeypatch):
    from yggdrasil import journal, ratatoskr, reducer
    ratatoskr.send("pm1", "tl1", "note", "first root")
    journal.add("alpha", "counter", value=1)  # the same last entry in both roots
    first = reducer.fold_programme("alpha")
    copy = tmp_path / "tracker-copy"
    shutil.copytree(world / "tracker", copy)
    path = copy / journal.path_for("alpha").relative_to(world / "tracker")
    text = path.read_text()
    marker = "node: tl1\n"
    assert marker in text
    path.write_text(text.replace(marker, "node: su1\n", 1))  # an earlier entry differs
    monkeypatch.setenv("YGG_FILES_ROOT", str(copy))
    revision, entries = journal.snapshot("alpha")
    assert revision == journal.revision("alpha") and entries[-1]["kind"] == "counter"
    second = reducer.fold_programme("alpha")
    assert [m["node"] for m in first["inbox"].values()] == ["tl1"]
    assert [m["node"] for m in second["inbox"].values()] == ["su1"]
