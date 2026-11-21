"""The work-ahead gate as a reducer blocker: withheld work stays out of the work set and stays owed."""

from yggdrasil import util
from test_round2 import clear_caches, package, wait_exit, world  # noqa: F401  (shared fixture)


def owed(node):
    from yggdrasil import reducer
    return reducer.owed(node)


def test_a_later_stage_package_holds_the_lead_with_its_reason(world):
    from yggdrasil import conductor, state, status, workahead
    package("WP-2", stage=2)
    items = owed("tl1")
    assert items and all(i["kind"] in ("record", "message") for i in items)
    found = workahead.screen("tl1", items)
    assert found.blocks and found.items == [] and found.messages == []
    assert found.reason == workahead.PID_OFF
    assert conductor.blocker(state.all_nodes()["tl1"], items) == "work-ahead"
    assert "tl1" not in conductor.candidates()
    view = status.build_status()
    lead = next(n for n in view["nodes"] if n["id"] == "tl1")
    assert lead["state"] == "queued" and lead["held_reason"] == workahead.PID_OFF
    assert "tl1" not in view["held_launches"]  # a work-ahead hold is not an admission hold
    assert all(n["held_reason"] is None for n in view["nodes"] if n["id"] != "tl1")
    assert "tl1 waiting: " + workahead.PID_OFF in status.human(view)


def test_current_stage_work_is_never_screened(world):
    from yggdrasil import conductor, state, workahead
    package("WP-1", stage=1)
    items = owed("tl1")
    found = workahead.screen("tl1", items)
    assert not found.blocks and len(found.items) == len(items) and found.ahead == []
    assert conductor.blocker(state.all_nodes()["tl1"], items) is None


def test_a_woken_launch_with_only_withheld_work_is_refused(world):
    import pytest
    from yggdrasil import launcher
    package("WP-2", stage=2)
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1", explicit=False)
    assert "is held: " + "work-ahead: pid off" in error.value.message


def test_a_launch_for_other_work_is_given_none_of_the_withheld_work(world):
    from yggdrasil import launcher, ratatoskr, state
    package("WP-2", stage=2)
    withheld = {i["id"] for i in owed("tl1")}
    note = ratatoskr.send("pm1", "tl1", "note", "unrelated question")
    result = launcher.launch("tl1", explicit=False)
    wait_exit(result["activation"])
    given = launcher.receipt(result["activation"])["consumed"]
    assert given["messages"] == [note]
    assert not set(given.get("records") or []) & withheld
    assert withheld <= {i["id"] for i in owed("tl1")}  # still owed: never consumed
    assert "WP-2" not in ratatoskr.inbox("tl1")
    from yggdrasil import tracker
    document = tracker.read_document("p1", "rehydrate-tl1") or ""
    assert "WP-2" not in document and "unrelated question" in document
    assert not launcher.receipt(result["activation"]).get("work_ahead_packages")
    assert state.runtime("tl1").get("activation") == result["activation"]


def test_a_retry_carries_its_activation_packages(world):
    from yggdrasil import lifecycle, workahead
    package("WP-2", stage=2)
    lifecycle.commit("tl1", "act-0071", "reserved", launcher_pid=1, launcher_started_at="x", work={},
                     packages=["WP-2"])
    lifecycle.commit("tl1", "act-0072", "reserved", launcher_pid=1, launcher_started_at="x", work={},
                     packages=["WP-1"])
    held = workahead.screen("tl1", [{"kind": "retry", "id": "act-0071", "reason": "retry"}], [])
    assert held.blocks and held.reason == workahead.PID_OFF
    free = workahead.screen("tl1", [{"kind": "retry", "id": "act-0072", "reason": "retry"}], [])
    assert not free.blocks  # WP-1 is not a package of the project: nothing is withheld


def test_a_ci_result_is_withheld_only_when_every_unfinished_lane_package_is_blocked(world):
    from yggdrasil import workahead
    package("WP-2", stage=2)
    ci = {"kind": "event", "id": "e-000000000001", "reason": "ci:failed"}
    assert workahead.screen("tl1", [ci], []).blocks
    package("WP-1", stage=1)
    assert not workahead.screen("tl1", [ci], []).blocks


def test_record_stamps_on_a_package_name_it(world):
    package("WP-2", stage=2)
    records = [i for i in owed("tl1") if i["kind"] == "record"]
    assert records and all(i.get("package") == "WP-2" for i in records)


def test_a_withheld_retry_keeps_its_count_on_a_launch_for_other_work(world):
    from yggdrasil import launcher, lifecycle, ratatoskr, reducer
    package("WP-2", stage=2)
    lifecycle.commit("tl1", "act-0081", "reserved", launcher_pid=1, launcher_started_at="x", work={},
                     packages=["WP-2"], retries_used=1)
    lifecycle.commit("tl1", "act-0081", "spawned", supervisor_pid=1, supervisor_started_at="x", agent_nonce="n")
    lifecycle.commit("tl1", "act-0081", "exited", exit_code=1, capacity=False)
    [retry] = [i for i in reducer.owed("tl1") if i["kind"] == "retry"]
    assert retry["retries_used"] == 2
    ratatoskr.send("pm1", "tl1", "note", "unrelated question")
    result = launcher.launch("tl1", explicit=False)
    wait_exit(result["activation"])
    data = launcher.receipt(result["activation"])
    assert data["retries_used"] == 2  # the chain's spending carries over
    assert not data["consumed"].get("retries")  # the withheld retry itself is not given


def _approved_work_ahead():
    from yggdrasil import commands, envelopes, workahead
    workahead.set_setting("p1", "conditional", "test setup")
    commands.node_add("rv1", "reviewer", "alpha")
    package("WP-2", stage=2)
    envelopes.grant("p1", "2026-11-17T00:00:00Z", None, False)
    workahead.write_record("p1", "WP-2", "pm1", "later docs", "independent files")
    workahead.write_review("p1", "WP-2", "su1", "approve", None)
    workahead.write_review("p1", "WP-2", "rv1", "approve", None)


def test_a_withheld_retry_survives_a_successful_launch_for_other_work(world, monkeypatch):
    from yggdrasil import conductor, launcher, ratatoskr, reducer, state, workahead
    _approved_work_ahead()
    agent = world / "agent"
    agent.write_text(agent.read_text() + "sys.exit(1)\n")
    first = launcher.launch("tl1", explicit=False)
    wait_exit(first["activation"])
    assert launcher.receipt(first["activation"]).get("exit_code") == 1
    # A11 AC-29, A11.1 AC-02: the failed run consumed nothing, so its work stays owed beside the retry.
    [retry] = [i for i in reducer.owed("tl1") if i["kind"] == "retry"]
    assert retry["kind"] == "retry"
    workahead.set_setting("p1", "off", "hold later work")
    assert workahead.screen("tl1", [retry]).blocks
    agent.write_text(agent.read_text().replace("sys.exit(1)", "sys.exit(0)"))
    ratatoskr.send("pm1", "tl1", "note", "unrelated question")
    monkeypatch.setenv("YGG_NOW", "2026-11-16T03:00:00Z")
    result = conductor.tick()
    assert "tl1" in result["launched"], result
    second = state.runtime("tl1")["activation"]
    wait_exit(second)
    data = launcher.receipt(second)
    assert data.get("exit_code") == 0 and not data["consumed"].get("retries")
    assert any(i["kind"] == "retry" and i["id"] == first["activation"] for i in reducer.owed("tl1"))
    workahead.set_setting("p1", "conditional", "resume later work")
    assert not workahead.screen("tl1", reducer.owed("tl1")).blocks  # given to the next launch once allowed


def test_a_lead_withheld_only_for_a_suspended_package_shows_the_suspension(world):
    from yggdrasil import launcher, morning_go, ratatoskr, reducer, status, workahead
    _approved_work_ahead()
    first = launcher.launch("tl1", explicit=False)
    wait_exit(first["activation"])
    item = next(i for i in morning_go.items("alpha") if i.get("kind") == "work-ahead")
    morning_go.decide(item["id"], "not-ok")
    ratatoskr.send("pm1", "tl1", "note", "WP-2 followup")
    found = workahead.screen("tl1", reducer.owed("tl1"))
    assert found.blocks and found.reason == workahead.SUSPENDED
    view = status.build_status()
    lead = next(n for n in view["nodes"] if n["id"] == "tl1")
    assert lead["state"] == "queued" and lead["held_reason"] == workahead.SUSPENDED  # owed work stays visible
    assert "tl1" not in view["held_launches"]


def test_a_mixed_retry_is_withheld_whole_while_one_of_its_packages_is_blocked(world):
    from yggdrasil import lifecycle, workahead
    package("WP-1", stage=1)
    package("WP-2", stage=2)
    lifecycle.commit("tl1", "act-0091", "reserved", launcher_pid=1, launcher_started_at="x", work={},
                     packages=["WP-1", "WP-2"])
    found = workahead.screen("tl1", [{"kind": "retry", "id": "act-0091", "reason": "retry"}], [])
    assert found.blocks and found.reason == workahead.PID_OFF  # never spent on its allowed part alone


def test_the_context_keeps_the_reservation_selection_while_the_activation_runs(world, monkeypatch):
    from yggdrasil import launcher, ratatoskr, tracker, workahead
    _approved_work_ahead()
    monkeypatch.setenv("YGG_UNIT_AGENT_SLEEP", "3")
    result = launcher.launch("tl1", explicit=False)
    assert workahead._selection("tl1")[0] == {"WP-2"}  # from the durable reserved fact
    workahead.set_setting("p1", "off", "switched off during the run")
    assert "WP-2" not in workahead.blocked_packages("tl1")  # given to this activation: still shown
    assert "WP-2" in ratatoskr.inbox("tl1")
    from yggdrasil import rehydrate
    rehydrate.write_document("tl1")
    assert "WP-2" in (tracker.read_document("p1", "rehydrate-tl1") or "")
    wait_exit(result["activation"])
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "WP-2" not in workahead.blocked_packages("tl1"):
        time.sleep(0.1)
    assert "WP-2" in workahead.blocked_packages("tl1")  # settled: the current selection applies again


def test_stop_never_depends_on_reading_the_tracker(world):
    import os
    from yggdrasil import cli, config, tracker
    package("WP-1", stage=1)
    path = tracker.issue_path("p1", "WP-1")
    os.chmod(path, 0)
    try:
        config.validate()  # an unreadable record never fails the PID-only scan
        assert cli.main(["stop"]) == 0
    finally:
        os.chmod(path, 0o600)


def test_a_message_given_before_its_package_became_blocked_stays_shown(world, monkeypatch):
    from yggdrasil import commands, launcher, ratatoskr, workahead
    note = ratatoskr.send("pm1", "tl1", "note", "please look at WP-7 next")
    monkeypatch.setenv("YGG_UNIT_AGENT_SLEEP", "3")
    result = launcher.launch("tl1", explicit=False)
    assert note in launcher.receipt(result["activation"])["consumed"]["messages"]
    commands.tracker_write("pm1", "p1", "work-package", "Later", stage="2", package="WP-7")  # blocked from now
    assert "WP-7" in workahead.blocked_packages("tl1")
    assert not workahead.hidden("tl1", {"msg_id": note, "body": "please look at WP-7 next"})
    assert note in ratatoskr.inbox("tl1")
    wait_exit(result["activation"])


def test_the_reservation_selection_survives_a_wiped_state_root(world, monkeypatch):
    import shutil
    from yggdrasil import launcher, state, workahead
    _approved_work_ahead()
    package("WP-3", stage=2)  # held: no work-ahead record of its own
    monkeypatch.setenv("YGG_UNIT_AGENT_SLEEP", "3")
    result = launcher.launch("tl1", explicit=False)
    given, _, blocked = workahead._selection("tl1")
    assert "WP-2" in given and "WP-3" in blocked
    for folder in ("activations", "nodes"):
        shutil.rmtree(state.root() / folder, ignore_errors=True)  # the local receipt and runtime are gone
    assert workahead._selection("tl1") == (given, workahead._selection("tl1")[1], blocked)
    assert "WP-3" in workahead.blocked_packages("tl1") and "WP-2" not in workahead.blocked_packages("tl1")
    wait_exit(result["activation"])
