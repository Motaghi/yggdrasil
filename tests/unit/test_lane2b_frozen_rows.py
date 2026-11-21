"""Lane 2b: older frozen rows that failed on the integrated A11 code, each cause pinned at unit level."""

import subprocess

from yggdrasil import util


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_the_agreement_lives_in_the_files_root_under_the_linear_tracker(tmp_path, monkeypatch):
    # AC-19 names `<files root>/_ygg/agents-agreement.json`; the Linear cache sits in the state root, which a
    # wipe empties, so an agreement kept there was lost on every rehydrate.
    from yggdrasil import config, permissions, state
    monkeypatch.setenv("YGG_TRACKER", "linear")
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "files"))
    for cached in (config.effective, config.file_layers, config.profile):
        cached.cache_clear()
    path = permissions.agreement_path()
    assert path == tmp_path / "files" / "_ygg" / "agents-agreement.json"
    assert state.root() not in path.parents


def test_nodes_that_stall_in_one_pass_share_one_alert(monkeypatch):
    from yggdrasil import retries, state
    raised = []
    monkeypatch.setattr(state, "alert", lambda kind, message, **fields: raised.append((kind, message, fields)))
    retries.alert_stalled([("tl1", "act-0001"), ("tl2", "act-0002")])
    assert len(raised) == 1
    kind, message, fields = raised[0]
    assert kind == "ygg-stall" and "tl1" in message and "tl2" in message
    assert fields["nodes"] == ["tl1", "tl2"] and fields["escalated"] is True and "node" not in fields
    raised.clear()
    retries.alert_stalled([("tl1", "act-0001")])
    assert raised == []  # that chain is notified already
    retries.alert_stalled([("tl1", "act-0003")])
    assert raised[0][2]["node"] == "tl1" and raised[0][2]["activation"] == "act-0003"
    raised.clear()
    retries.alert_stalled([])
    assert raised == []


def test_a_spent_receipt_with_no_journal_facts_is_outside_the_chain(monkeypatch):
    from yggdrasil import conductor
    monkeypatch.setattr(conductor, "_has_lifecycle", lambda node, activation, data: bool(data.get("lifecycle")))
    assert conductor._spent_outside_chain("tl1", "act-0001", {"exited_at": None})
    assert conductor._spent_outside_chain("tl1", "act-0001", {"exited_at": "2026-11-16T10:00:00Z"})
    assert not conductor._spent_outside_chain("tl1", "act-0001", {"exited_at": "2026-11-16T10:00:00Z",
                                                                  "lifecycle": True})


def test_a_review_of_a_lane_that_never_launched_makes_its_branch(tmp_path, monkeypatch):
    # Amendment 8: `wp complete` opens a review whenever the project has a reviewer node, also for a lane whose
    # team lead never launched (the re-frozen crowded-PM flow), so the lane branch is made where a launch would.
    from yggdrasil import lanedrop, review
    home = tmp_path / "home"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    _git(home, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "a")
    main = _git(home, "rev-parse", "main")
    monkeypatch.setattr(review, "home_of", lambda project: str(home))
    assert review.lane_head("alpha-p1", "pm1tl2") is None
    assert review._create_lane("alpha-p1", "pm1tl2") == main
    assert review.lane_head("alpha-p1", "pm1tl2") == main
    assert lanedrop.creation("alpha-p1", "pm1tl2") == main
    _git(home, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "b")
    _git(home, "branch", "ygg/alpha-p2/_integration", "main")
    integration = _git(home, "rev-parse", "main")
    assert review._create_lane("alpha-p2", "pm3tl1") == integration
    assert review._create_lane("alpha-p1", "pm1tl2") == main  # an existing branch is left as it is
    assert util.trusted_git("-C", str(home), "rev-parse", "refs/heads/ygg/alpha-p1/lane/pm1tl2").stdout.strip() == main


def _stage_alerts(kind):
    from yggdrasil import state
    return [a for a in state.alerts() if a.get("kind") == kind]


def test_a_stage_end_after_the_ready_to_close_alert_adds_no_second_stage_ready():
    # "Stage end alerts" as amended: the stage end's log-class ygg-stage-ready only when none was raised yet for
    # that stage. Ready to close first (stageclose's alert), then the stage end: one in all.
    from yggdrasil import commands, state
    state.alert("ygg-stage-ready", "Stage 1 of alpha-p1 is ready to close", project="alpha-p1", stage=1)
    commands._stage_end_alerts("alpha-p1", 1)
    assert len(_stage_alerts("ygg-stage-ready")) == 1
    assert len(_stage_alerts("ygg-stage-approval")) == 1


def test_a_stage_end_with_no_tick_before_it_raises_one_stage_ready():
    from yggdrasil import commands, state
    state.alert("ygg-stage-ready", "Stage 1 of beta-p1 is ready to close", project="beta-p1", stage=1)
    commands._stage_end_alerts("alpha-p1", 1)
    commands._stage_end_alerts("alpha-p1", 2)
    ready = [a for a in _stage_alerts("ygg-stage-ready") if a.get("project") == "alpha-p1"]
    assert [a["stage"] for a in ready] == [1, 2]
    assert all(a["class"] == "log" for a in ready)
    assert len(_stage_alerts("ygg-stage-approval")) == 2


def test_a_pass_interrupted_before_its_stall_alert_leaves_the_chain_due(monkeypatch):
    # Codex review r2, finding 1: a chain counts as notified only once its alert is written.
    import pytest
    from yggdrasil import retries, state
    monkeypatch.setattr(state, "runtime", lambda node: {"state": "stalled"})
    raised = []

    def crash(kind, message, **fields):
        raise RuntimeError("interrupted before the alert")

    monkeypatch.setattr(state, "alert", crash)
    assert retries.mark_stalled("tl1", "act-0001", alert=False)
    assert retries.mark_stalled("tl2", "act-0002", alert=False)
    with pytest.raises(RuntimeError):
        retries.alert_stalled([("tl1", "act-0001"), ("tl2", "act-0002")])
    monkeypatch.setattr(state, "alert", lambda kind, message, **fields: raised.append(fields))
    assert retries.mark_stalled("tl1", "act-0001", alert=False)  # still due on the next pass
    assert retries.alert_stalled([("tl1", "act-0001"), ("tl2", "act-0002"), ("tl1", "act-0001")])
    assert len(raised) == 1 and raised[0]["nodes"] == ["tl1", "tl2"]
    assert not retries.mark_stalled("tl1", "act-0001", alert=False)
    assert not retries.alert_stalled([("tl2", "act-0002")])
    assert len(raised) == 1


def test_a_lane_creation_interrupted_before_its_branch_keeps_the_recorded_point(tmp_path, monkeypatch):
    # Codex review r2, finding 2: the creation point is recorded before the branch is made, and a later call makes
    # the branch at that point even after the integration branch moved ahead of main.
    import pytest
    from yggdrasil import lanedrop, review
    home = tmp_path / "home"
    home.mkdir()
    commit = ("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m")
    _git(home, "init", "-q", "-b", "main")
    _git(home, *commit, "main at approval")
    main = _git(home, "rev-parse", "main")
    _git(home, "checkout", "-q", "-b", "ygg/alpha-p1/_integration")
    _git(home, *commit, "merged lane work")
    integration = _git(home, "rev-parse", "HEAD")
    monkeypatch.setattr(review, "home_of", lambda project: str(home))
    real = util.trusted_git

    def interrupted(*args, **kwargs):
        if "branch" in args:
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(util, "trusted_git", interrupted)
    with pytest.raises(KeyboardInterrupt):
        review._create_lane("alpha-p1", "pm1tl2")
    monkeypatch.setattr(util, "trusted_git", real)
    assert lanedrop.creation("alpha-p1", "pm1tl2") == integration
    assert review.lane_head("alpha-p1", "pm1tl2") is None
    _git(home, *commit, "integration moves on")
    assert review._create_lane("alpha-p1", "pm1tl2") == integration
    assert review.lane_head("alpha-p1", "pm1tl2") == integration
    assert lanedrop.creation_point("alpha-p1", "pm1tl2", str(home)) == integration != main


# ---------------------------------------------------------------- lane 2 review round 2, the cancel items

from test_round2 import package, world  # noqa: E402,F401  (shared fixture)


def _fold_record(activation):
    from yggdrasil import reducer
    for programme_fold in reducer.folds().values():
        if activation in programme_fold["activations"]:
            return programme_fold["activations"][activation]
    return None


def _reserve(node, activation, **fields):
    from yggdrasil import lifecycle
    lifecycle.commit(node, activation, "reserved", launcher_pid=1, launcher_started_at="x", work={}, **fields)


def test_a_senior_users_reservation_fact_names_its_work_packages(world):
    # cancel.py:324 (blocker): every role's reservation fact carries its packages, not only a team lead's.
    from yggdrasil import launcher
    package()
    activation = launcher.launch("su1")["activation"]
    assert _fold_record(activation)["packages"] == ["WP-1"]


def test_work_packages_follow_records_retries_and_messages_and_leave_out_cancelled(world):
    from yggdrasil import cancel, ratatoskr, state
    package("WP-1")
    package("WP-2")
    package("WP-3")
    _reserve("su1", "act-0080", packages=["WP-3"])
    message = ratatoskr.send("pm1", "su1", "work", "look at WP-2", internal=True)
    cancel.seed("p1", "WP-3")
    given = [{"kind": "record", "id": "o-1", "package": "WP-1"}, {"kind": "retry", "id": "act-0080"}]
    node = state.all_nodes()["su1"]
    assert cancel.work_packages(node, given, {"messages": [message]}, None) == ["WP-1", "WP-2"]
    assert cancel.work_packages(node, [], {"messages": []}, "WP-2") == ["WP-2"]


def test_a_cancel_settles_a_senior_users_retry_through_its_reservation_fact(world):
    from yggdrasil import cancel, lifecycle, reducer, state
    package()
    node = state.all_nodes()["su1"]
    packages = cancel.work_packages(node, [{"kind": "record", "id": "o-1", "package": "WP-1"}], {}, None)
    _reserve("su1", "act-0091", packages=packages)
    lifecycle.commit("su1", "act-0091", "spawned", supervisor_pid=1, supervisor_started_at="x", agent_nonce="n")
    lifecycle.commit("su1", "act-0091", "exited", exit_code=1, capacity=False)
    assert ("retry", "act-0091") in {(i["kind"], i["id"]) for i in reducer.owed("su1")}
    cancel.seed("p1", "WP-1")
    assert ("retry", "act-0091") not in {(i["kind"], i["id"]) for i in reducer.owed("su1")}


def _su_receipt(activation):
    from yggdrasil import launcher
    launcher.save_receipt(activation, {"node": "su1", "activation": activation, "reserved_at": "2026-11-16T02:00:00Z",
                                       "testing": {"package": "WP-1"}, "consumed": {"messages": [], "records": []}})


def test_the_start_check_orders_a_same_second_cancel_by_the_journal(world):
    # cancel.py:274 (major): YGG_NOW is fixed, so the cancel and the reservation share one second; journal order
    # decides, as A11.1 AC-13 says.
    from yggdrasil import cancel, state
    package("WP-1")
    node = state.all_nodes()["su1"]
    cancel.seed("p1", "WP-1")  # the cancel first
    _reserve("su1", "act-0101")
    _su_receipt("act-0101")
    assert cancel.start_check(node, "act-0101") is None
    package("WP-2")
    from yggdrasil import launcher
    _reserve("su1", "act-0102")  # the reservation first
    launcher.save_receipt("act-0102", {"node": "su1", "activation": "act-0102",
                                       "reserved_at": "2026-11-16T02:00:00Z", "testing": {"package": "WP-2"},
                                       "consumed": {"messages": [], "records": []}})
    cancel.seed("p1", "WP-2")
    assert cancel.start_check(node, "act-0102").startswith("cancelled: WP-2")


# ---------------------------------------------------------------- Codex review round 3 (lane 2b A)

def test_a_crash_after_the_stall_alert_and_before_its_mark_gives_one_alert(monkeypatch):
    # retries.py:112: the alert carries the chain's stable identity; recovery marks the chain from it.
    import pytest
    from yggdrasil import retries, state
    monkeypatch.setattr(state, "runtime", lambda node: {"state": "stalled"})
    real = util.write_json

    def crash(path, data):
        if str(path).endswith("stall-alerts.json"):
            raise RuntimeError("interrupted after the append")
        return real(path, data)

    monkeypatch.setattr(util, "write_json", crash)
    with pytest.raises(RuntimeError):
        retries.alert_stalled([("tl1", "act-0001")])
    monkeypatch.setattr(util, "write_json", real)
    assert retries.mark_stalled("tl1", "act-0001", alert=False)  # not marked yet
    assert not retries.alert_stalled([("tl1", "act-0001")])  # recovered from the written alert
    assert not retries.mark_stalled("tl1", "act-0001", alert=False)
    stalls = [a for a in state.alerts() if a.get("kind") == "ygg-stall"]
    assert len(stalls) == 1 and stalls[0]["dedup"] == ["stall:tl1:act-0001"]
    retries.alert_stalled([("tl1", "act-0009"), ("tl2", "act-0002")])  # a new chain still alerts
    assert len([a for a in state.alerts() if a.get("kind") == "ygg-stall"]) == 2


def test_the_launcher_makes_an_interrupted_lane_at_its_recorded_creation_point(tmp_path, monkeypatch):
    # review.py:222: a creation interrupted after its intent, then the integration branch moves, then the lane's
    # first launch: the branch is made at the intent, so a cancel of the untouched lane drops nothing.
    import pytest
    from yggdrasil import lanedrop, launcher, review
    home = tmp_path / "home"
    home.mkdir()
    commit = ("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m")
    _git(home, "init", "-q", "-b", "main")
    _git(home, *commit, "main at approval")
    _git(home, "checkout", "-q", "-b", "ygg/alpha-p1/_integration")
    _git(home, *commit, "merged lane work")
    first = _git(home, "rev-parse", "HEAD")
    _git(home, "checkout", "-q", "main")
    monkeypatch.setattr(review, "home_of", lambda project: str(home))
    real = util.trusted_git

    def interrupted(*args, **kwargs):
        if "branch" in args:
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(util, "trusted_git", interrupted)
    with pytest.raises(KeyboardInterrupt):
        review._create_lane("alpha-p1", "pm1tl2")
    monkeypatch.setattr(util, "trusted_git", real)
    _git(home, "checkout", "-q", "ygg/alpha-p1/_integration")
    _git(home, *commit, "integration moves on")
    _git(home, "checkout", "-q", "main")
    monkeypatch.setattr(util, "helper_refusal", lambda *a, **k: None)
    path = home / ".ygg-worktrees" / "alpha-p1" / "lane" / "pm1tl2"
    launcher.ensure_worktree(str(home), path, "ygg/alpha-p1/lane/pm1tl2", "ygg/alpha-p1/_integration")
    assert _git(home, "rev-parse", "ygg/alpha-p1/lane/pm1tl2") == first
    assert lanedrop.creation("alpha-p1", "pm1tl2") == first
    plan = lanedrop.plan("alpha-p1", "pm1tl2", ["WP-1"], str(home))
    assert plan["drop_point"] == first and plan["dropped"] == []


def test_a_senior_users_fallback_testing_package_reaches_its_reservation_fact(world, tmp_path, monkeypatch):
    # launcher.py:1147: no owed item names a package, so the oldest ready-for-test one is tested; the journal fact
    # names it, and after a failed run, a reopen and a cancel, the retry settles.
    import time
    from yggdrasil import cancel, launcher, reducer
    package("WP-1")
    _set_package_state("WP-1", "ready-for-test", settle_owed=True)
    failing = tmp_path / "failing-agent"
    failing.write_text("#!/bin/sh\nsleep 1\nexit 1\n")
    failing.chmod(0o755)
    monkeypatch.setenv("YGG_CLAUDE_BIN", str(failing))
    activation = launcher.launch("su1")["activation"]
    assert _fold_record(activation)["packages"] == ["WP-1"]
    # The supervisor saves the receipt's exit before it commits the journal's `exited` fact, and the retry is owed
    # from that fact: wait for the retry itself, not only for the receipt.
    def owed():
        return {(i["kind"], i["id"]) for i in reducer.owed("su1")}

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and ("retry", activation) not in owed():
        time.sleep(0.05)
    assert launcher.receipt(activation).get("exit_code") == 1
    assert _fold_record(activation)["terminal"] == "exited"
    assert ("retry", activation) in owed()
    _set_package_state("WP-1", "authorised")  # the PM's reopen sends it back to its lane
    cancel.seed("p1", "WP-1")
    assert ("retry", activation) not in owed()


def _set_package_state(package_id, value, *, settle_owed=False):
    from yggdrasil import tracker
    issue = tracker.read_issue("p1", package_id)
    issue.header["state"] = value
    if settle_owed:
        issue.header["owed"] = []  # the package's own notices, so no owed item names it
    issue.save()


# ---------------------------------------------------------------- Codex review round 4 (lane 2b)

def test_a_reopen_before_preparation_aborts_the_launch_instead_of_testing_another_package(world, monkeypatch):
    # launcher.py:1192: the reservation pins WP-1 (its retry's package) while WP-2 is also ready; the PM reopens WP-1
    # before preparation. The launch aborts, writes no WP-2 testing metadata, and the retry stays owed.
    import pytest
    from yggdrasil import launcher, reducer
    package("WP-1")
    package("WP-2")
    _set_package_state("WP-1", "ready-for-test", settle_owed=True)
    _set_package_state("WP-2", "ready-for-test", settle_owed=True)
    _failed_test_run("act-0301", "WP-1")
    assert ("retry", "act-0301") in {(i["kind"], i["id"]) for i in reducer.owed("su1")}
    real = launcher.working_directory

    def reopened_first(node, nodes, **kwargs):
        _set_package_state("WP-1", "authorised")  # the PM's reopen, after the reservation
        return real(node, nodes, **kwargs)

    monkeypatch.setattr(launcher, "working_directory", reopened_first)
    calls = []
    real_pinned = launcher.pinned_testing_target
    monkeypatch.setattr(launcher, "pinned_testing_target",
                        lambda node, nodes, package: calls.append(package) or real_pinned(node, nodes, package))
    with pytest.raises(util.YggError) as error:
        launcher.launch("su1")
    assert "WP-1 is no longer ready for test" in error.value.message
    assert calls and set(calls) == {"WP-1"}
    import json
    from yggdrasil import state
    receipts = [json.loads(p.read_text()) for p in (state.root() / "activations").glob("act-*.json")]
    receipts = [r for r in receipts if r.get("node") == "su1" and r.get("activation") != "act-0301"]
    assert receipts and all(r.get("outcome") == "aborted" for r in receipts)
    assert not any((r.get("testing") or {}).get("package") == "WP-2" for r in receipts)
    assert ("retry", "act-0301") in {(i["kind"], i["id"]) for i in reducer.owed("su1")}


def _failed_test_run(activation, package_id):
    from yggdrasil import launcher, lifecycle
    _reserve("su1", activation, packages=[package_id])
    launcher.save_receipt(activation, {"node": "su1", "activation": activation, "testing_package": package_id,
                                       "reserved_at": "2026-11-16T02:00:00Z", "consumed": {}})
    lifecycle.commit("su1", activation, "spawned", supervisor_pid=1, supervisor_started_at="x", agent_nonce="n")
    lifecycle.commit("su1", activation, "exited", exit_code=1, capacity=False)


def test_a_reopen_after_the_testing_metadata_and_before_the_lock_still_aborts(world, monkeypatch):
    # Codex review round 5, launcher.py:1297: the check under the lock reads the tracker again.
    import json
    import pytest
    from yggdrasil import launcher, reducer, state
    package("WP-1")
    package("WP-2")
    _set_package_state("WP-1", "ready-for-test", settle_owed=True)
    _set_package_state("WP-2", "ready-for-test", settle_owed=True)
    _failed_test_run("act-0401", "WP-1")
    seen = {"metadata": False}
    real_hold = launcher.hold_reason

    def reopened_before_lock_check(node, activation):
        # hold_reason is the first call inside that lock: the reopen lands after the metadata was computed.
        if not seen["metadata"]:
            seen["metadata"] = True
            _set_package_state("WP-1", "authorised")
        return real_hold(node, activation)

    monkeypatch.setattr(launcher, "hold_reason", reopened_before_lock_check)
    with pytest.raises(util.YggError) as error:
        launcher.launch("su1")
    assert "WP-1 is no longer ready for test" in error.value.message
    receipts = [json.loads(p.read_text()) for p in (state.root() / "activations").glob("act-*.json")]
    receipts = [r for r in receipts if r.get("node") == "su1" and r.get("activation") != "act-0401"]
    assert receipts and all(r.get("outcome") == "aborted" for r in receipts)
    assert not any(r.get("phase") == "starting" for r in receipts)
    assert seen["metadata"]
    assert ("retry", "act-0401") in {(i["kind"], i["id"]) for i in reducer.owed("su1")}
