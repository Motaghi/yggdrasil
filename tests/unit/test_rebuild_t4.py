"""Rebuild task 4: messages and events are owed work derived from journal facts; no queued wakes."""

import json

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)
from test_round3 import receipt_file


def given(node, activation, *phases, **work):
    """Lifecycle facts for an activation given `work` (kind -> ids), through the named later phases."""
    from yggdrasil import lifecycle
    lifecycle.commit(node, activation, "reserved", launcher_pid=1, launcher_started_at="x", work=work)
    for phase, fields in phases:
        lifecycle.commit(node, activation, phase, **fields)


SPAWNED = ("spawned", {"supervisor_pid": 1, "supervisor_started_at": "x", "agent_nonce": "n"})


def owed_ids(node):
    from yggdrasil import reducer
    return [i["id"] for i in reducer.owed(node)]


def test_a_message_is_owed_until_a_spawned_activation_is_given_it(world):
    from yggdrasil import ratatoskr, state
    msg = ratatoskr.send("pm1", "tl1", "note", "hello")
    assert msg in owed_ids("tl1") and "tl1" in state.wakes()
    assert not (state.root() / "wakes.json").exists()  # nothing is queued locally
    given("tl1", "act-0042", **{"messages": [msg]})
    assert msg not in owed_ids("tl1")  # reserved while the activation has not ended
    from yggdrasil import lifecycle
    lifecycle.commit("tl1", "act-0042", "aborted", reason="paused")
    assert msg in owed_ids("tl1")  # an aborted activation consumed nothing
    given("tl1", "act-0043", SPAWNED, ("exited", {"exit_code": 0}), **{"messages": [msg]})
    assert msg not in owed_ids("tl1")


def test_failed_creation_and_capacity_exits_consume_nothing(world):
    from yggdrasil import state
    state.wake("tl1", "criteria:WP-1")
    [event] = owed_ids("tl1")
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 127, "spawn_failed": True}), events=[event])
    assert event in owed_ids("tl1")
    given("tl1", "act-0043", SPAWNED, ("exited", {"exit_code": 1, "capacity": True}), events=[event])
    assert event in owed_ids("tl1")
    given("tl1", "act-0044", SPAWNED, ("exited", {"exit_code": 1}), events=[event])
    # A11 AC-29 and A11.1 AC-02: a failure (a non-zero exit with no signal) consumes nothing; its work goes to the
    # next run, which counts against retry_limit (A11.1 AC-01).
    assert event in owed_ids("tl1")


def test_acknowledging_a_message_is_all_it_takes_to_stop_owing_it(world):
    from yggdrasil import ratatoskr, rehydrate, state
    msg = ratatoskr.send("pm1", "tl1", "note", "hello")
    ratatoskr.inbox("tl1", ack=msg)
    assert "tl1" not in state.wakes()
    rehydrate.replay_journal()
    assert "tl1" not in state.wakes()


def test_an_ended_envelope_is_owed_by_reviewers_from_its_fact_alone(world):
    from yggdrasil import commands, journal, state
    commands.node_add("rv1", "reviewer", "alpha")
    journal.add("alpha", "envelope-ended", key="EN-0001")
    assert state.wakes()["rv1"]["reasons"] == ["envelope-ended:EN-0001"]
    given("rv1", "act-0042", SPAWNED, ("exited", {"exit_code": 0}), envelopes=["EN-0001"])
    assert "rv1" not in state.wakes()


def test_an_aborted_launch_that_was_given_nothing_is_owed_once(world):
    from yggdrasil import launcher, state, journal
    receipt_file("act-0042", outcome="pending", phase="starting", pid=None)
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "pending"})
    with util.locked():
        launcher.abort_activation("tl1", "act-0042", "blocked by D-0001")
        launcher.abort_effects("tl1", "act-0042", "blocked by D-0001")  # idempotent: no second event
    events = [e for e in journal.entries("alpha") if e.get("kind") == "event" and e.get("node") == "tl1"]
    assert [state.decode_reason(e["reason"]) for e in events] == ["launch-aborted"]
    assert "tl1" in state.wakes()


def test_a_stopped_node_aborted_launch_is_not_owed_again(world):
    from yggdrasil import launcher, state
    receipt_file("act-0042", outcome="pending", phase="starting", pid=None)
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "pending"})
    with util.locked():
        launcher.abort_activation("tl1", "act-0042", "stopped")
    assert "tl1" not in state.wakes()


def test_a_reconciled_exit_fact_keeps_the_capacity_class(world):
    from yggdrasil import conductor, lifecycle, state
    given("tl1", "act-0042", SPAWNED)
    receipt_file("act-0042", outcome="confirmed", pid=999999, exited_at=util.iso(), exit_code=1,
                 stderr_tail="HTTP 429", lifecycle=True)
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "confirmed"})
    conductor._reconcile_lifecycle("tl1", "act-0042", state.runtime("tl1"), json.loads(
        (state.root() / "activations" / "act-0042.json").read_text()))
    [end] = [e for e in lifecycle.facts("alpha", "act-0042") if e["phase"] == "exited"]
    assert end["capacity"] is True
