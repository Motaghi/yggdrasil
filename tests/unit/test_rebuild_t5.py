"""Rebuild task 5: holds are derived (capacity from the exit fact, load from the sample), retries are owed
from exit facts, and cap deferral shows as queued. Nothing about a hold is stored."""

import shutil

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)
from test_round3 import uncleared
from test_rebuild_t4 import SPAWNED, given, owed_ids


def node_state(view, node):
    return {n["id"]: n["state"] for n in view["nodes"]}[node]


def capacity_exit(node, activation, retries_used=0):
    from yggdrasil import launcher, lifecycle
    record = {"exit_code": 1, "exited_at": util.iso(), "stderr_tail": "HTTP 429"}
    lifecycle.commit(node, activation, "reserved", launcher_pid=1, launcher_started_at="x", work={},
                     retries_used=retries_used)
    lifecycle.commit(node, activation, *SPAWNED[:1], **SPAWNED[1])
    fields = launcher.exit_fields(record)
    lifecycle.commit(node, activation, "exited", exit_code=1, **fields)
    return fields["backoff_until"]


def test_a_capacity_hold_comes_from_the_exit_fact_and_survives_a_wipe(world, monkeypatch):
    from yggdrasil import conductor, status
    until = capacity_exit("tl1", "act-0042", retries_used=2)
    assert until == util.iso(util.parse_time(util.iso()) + __import__("datetime").timedelta(seconds=300))
    shutil.rmtree(world / "state")  # nothing local is needed
    view = status.build_status()
    assert node_state(view, "tl1") == "held-capacity" and "tl1" in view["held_launches"]
    assert "tl1" not in conductor.candidates()
    assert not uncleared("held-launch")
    monkeypatch.setenv("YGG_NOW", until)
    clear_caches()
    assert "tl1" in conductor.candidates()
    assert conductor.owed_retries("tl1") == 2  # the backoff resumes the same retry line


def test_held_load_is_derived_from_the_load_sample(world):
    from yggdrasil import ratatoskr, status
    ratatoskr.send("pm1", "tl1", "note", "hello")
    (world / "load").write_text("0.95")
    view = status.build_status()
    assert node_state(view, "tl1") == "held-load" and "tl1" in view["held_launches"]
    shutil.rmtree(world / "state")
    view = status.build_status()
    assert "tl1" in view["held_launches"]  # survives a wipe: nothing was stored
    (world / "load").write_text("0.1")
    view = status.build_status()
    assert node_state(view, "tl1") == "queued" and view["held_launches"] == []


def test_a_held_tick_stores_nothing(world, monkeypatch):
    from yggdrasil import conductor, journal, ratatoskr, state
    ratatoskr.send("pm1", "tl1", "note", "hello")
    monkeypatch.setattr(conductor, "admit", lambda: (False, "load 0.95 above 0.85"))
    before = len(journal.entries("alpha"))
    result = conductor.launch_queue([{"node": "tl1"}])
    assert result["held"] == ["tl1"]
    assert len(journal.entries("alpha")) == before
    assert state.runtime("tl1").get("state", "idle") == "idle"


def test_a_decision_comes_before_a_capacity_hold(world):
    from yggdrasil import status, tracker
    capacity_exit("tl1", "act-0042")
    tracker.new_issue("p1", "D-0001", "decision", [], "pm1", question="Hold?",
                      scope={"type": "lane", "id": "tl1"}).save()
    view = status.build_status()
    assert node_state(view, "tl1") == "blocked" and "tl1" not in view["held_launches"]


def test_a_failed_exit_owes_a_retry_while_the_chain_has_retries_left(world):
    from yggdrasil import journal, reducer
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 1}))
    [retry] = [i for i in reducer.owed("tl1") if i["kind"] == "retry"]
    assert (retry["id"], retry["retries_used"]) == ("act-0042", 1)
    # The retry launch is aborted (it ran nothing): the retry stays owed with the same count.
    given("tl1", "act-0043", ("aborted", {"reason": "paused"}), retries=["act-0042"])
    assert [i["id"] for i in reducer.owed("tl1") if i["kind"] == "retry"] == ["act-0042"]
    # A chain reset closes the chain.
    journal.add("alpha", "chain-reset", node="tl1")
    assert not [i for i in reducer.owed("tl1") if i["kind"] == "retry"]


def test_an_exhausted_chain_owes_no_retry(world):
    from yggdrasil import lifecycle, reducer
    lifecycle.commit("tl1", "act-0042", "reserved", launcher_pid=1, launcher_started_at="x", work={},
                     retries_used=2)
    lifecycle.commit("tl1", "act-0042", *SPAWNED[:1], **SPAWNED[1])
    lifecycle.commit("tl1", "act-0042", "exited", exit_code=1)
    assert not [i for i in reducer.owed("tl1") if i["kind"] == "retry"]


def test_a_retry_launch_consumes_the_retry(world):
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 1}))
    assert "act-0042" in owed_ids("tl1")
    given("tl1", "act-0043", SPAWNED, ("exited", {"exit_code": 0}), retries=["act-0042"])
    assert "act-0042" not in owed_ids("tl1")
