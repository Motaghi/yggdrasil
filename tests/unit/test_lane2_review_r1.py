"""A11 lane 2, Codex review round 1: a unit test per confirmed finding."""

import contextlib
import os
import subprocess

import pytest

from yggdrasil import util
from test_round2 import package, world  # noqa: F401  (shared fixture)
from test_rebuild_t4 import SPAWNED, given


def _store(review_id="rv-0001", **fields):
    from yggdrasil import review, tracker
    entry = {"id": review_id, "project": "p1", "package": "WP-1", "lane": "tl1", "reviewer": "pm1rv1",
             "base": "a" * 40, "head": "b" * 40, "criteria": "c", "rulings": 0, "round": 1, "state": "assigned",
             "no_verdict": 0, "launches": 1, "stamp": None, "seen": [], "family": None}
    entry.update(fields)
    data = {"counter": int(review_id[3:]), "reviews": {review_id: entry}, "packages": {}}
    tracker.write_json(review._store_path("reviews"), data)


def _set_state(state_name, package_id="WP-1"):
    from yggdrasil import tracker
    issue = tracker.read_issue("p1", package_id)
    issue.header["state"] = state_name
    issue.save()


def _messages(node):
    from yggdrasil import state
    return [m for m in state.inbox(node) if not m.get("acked_at")]


# review.py:469 (blocker): a result saved with its effect pending is finished by the next step, exactly once.
def test_a_saved_approval_is_replayed_and_routes_once(world):
    from yggdrasil import review, tracker
    package()
    _set_state("in-review")
    _store(state="approved", verdict="approve", effect="pending")
    review.replay_effects()
    issue = tracker.read_issue("p1", "WP-1")
    assert issue.get("state") == "ready-for-test" and issue.get("reviewed_head") == "b" * 40
    stamps = len(issue.header.get("owed") or [])
    assert review.get("rv-0001")["effect"] == "done"
    _store(state="approved", verdict="approve", effect="pending")
    review.replay_effects()
    assert len(tracker.read_issue("p1", "WP-1").header.get("owed") or []) == stamps


def test_a_saved_revise_sends_its_findings_once_across_replays(world):
    from yggdrasil import review, tracker
    package()
    _set_state("in-review")
    findings = [{"file": "a.py", "line": 1, "text": "unsafe report path"}]
    _store(state="revised", verdict="revise", findings=findings, effect="pending")
    review.apply_effect("rv-0001")
    _store(state="revised", verdict="revise", findings=findings, effect="pending")  # as if the done mark was lost
    review.replay_effects()
    assert tracker.read_issue("p1", "WP-1").get("state") == "authorised"
    assert len([m for m in _messages("tl1") if "unsafe report path" in m["body"]]) == 1


# review.py:40 (blocker): a malformed store is a corrupt tracker record, never an empty one.
@pytest.mark.parametrize("text", ["{", "[]", '{"counter": 1, "reviews": []}'])
def test_a_malformed_review_store_is_corrupt(world, text):
    from yggdrasil import review
    path = review._store_path("reviews")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    with pytest.raises(util.YggError) as caught:
        review.reviews()
    assert caught.value.code == util.EXIT_TRACKER


def test_a_malformed_ruling_store_is_corrupt(world):
    from yggdrasil import review
    path = review._store_path("rulings")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"counter": 1, "rulings": {}}')
    with pytest.raises(util.YggError) as caught:
        review.rulings("p1")
    assert caught.value.code == util.EXIT_TRACKER


# commands.py:1039 (blocker): withdrawing a capped package keeps a message that also names another package.
def test_withdraw_keeps_mixed_messages(world):
    from yggdrasil import commands, ratatoskr, tracker
    package()
    ratatoskr.send("pm1", "tl1", "work", "WP-1 alone", internal=True)
    ratatoskr.send("pm1", "tl1", "work", "WP-1 and WP-4 share the config change", internal=True)
    issue = tracker.read_issue("p1", "WP-1")
    commands._withdraw(issue, ["tl1"])
    bodies = [m["body"] for m in _messages("tl1")]
    assert "WP-1 and WP-4 share the config change" in bodies
    assert "WP-1 alone" not in bodies


# review.py:244 (major): the ready checks run again under the lock, so a second ready opens no second review.
def test_ready_rechecks_an_open_review_under_the_lock(world, monkeypatch):
    from yggdrasil import review
    package()
    _store(state="assigned")
    calls = {"n": 0}
    real = review.open_review

    def first_none(project, package_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(project, package_id)

    monkeypatch.setattr(review, "open_review", first_none)
    monkeypatch.setattr(review, "programme_kind", lambda project: "git-repo")
    monkeypatch.setattr(review, "project_reviewer", lambda project, nodes=None: {"id": "pm1rv1"})
    monkeypatch.setattr(review, "lane_head", lambda project, lane: "b" * 40)
    monkeypatch.setattr(review, "merge_base", lambda project, head: "a" * 40)
    monkeypatch.setattr(review, "_trusted_recheck", lambda *args: None)
    assert review.ready("tl1", "WP-1") == "review rv-0001 of WP-1 is already open\n"
    assert sorted(review.reviews()) == ["rv-0001"]


# review.py:699 (major): the drop message carries the frozen AC-57 row 3 phrase.
def test_a_drop_message_says_review_dropped_by_the_user(world):
    from yggdrasil import review
    package()
    _set_state("in-review")
    _store(state="held")
    review.drop("rv-0001")
    assert any("review dropped by the user" in m["body"] for m in _messages("tl1"))


# retries.py:50 (major): a node reset starts the failure chain again.
def test_a_node_reset_clears_the_failure_chain(world, monkeypatch):
    from yggdrasil import journal, retries
    monkeypatch.setattr(retries, "limit", lambda: 1)
    for number in (1, 2):
        given("tl1", "act-00%02d" % number, SPAWNED, ("exited", {"exit_code": 1}))
    assert retries.stalled("tl1") == "act-0002"
    journal.add("alpha", "chain-reset", node="tl1")
    assert retries.stalled("tl1") is None


# cancel.py:185 (blocker): a retry and a message whose packages are all cancelled are settled; a mixed one stays.
def test_cancelled_packages_settle_retries_and_messages(world):
    from yggdrasil import lifecycle, ratatoskr, reducer
    package()
    lifecycle.commit("tl1", "act-0090", "reserved", launcher_pid=1, launcher_started_at="x", work={},
                     packages=["WP-1"])
    lifecycle.commit("tl1", "act-0090", "spawned", supervisor_pid=1, supervisor_started_at="x", agent_nonce="n")
    lifecycle.commit("tl1", "act-0090", "exited", exit_code=1, capacity=False)
    alone = ratatoskr.send("pm1", "tl1", "work", "WP-1 alone", internal=True)
    mixed = ratatoskr.send("pm1", "tl1", "work", "WP-1 and WP-4 together", internal=True)
    owed = {(i["kind"], i["id"]) for i in reducer.owed("tl1")}
    assert ("retry", "act-0090") in owed and ("message", alone) in owed
    _set_state("cancelled")
    owed = {(i["kind"], i["id"]) for i in reducer.owed("tl1")}
    assert ("retry", "act-0090") not in owed and ("message", alone) not in owed
    assert ("message", mixed) in owed


# cancel.py:108 (blocker) and testmode.py:146 (seam major): a managed session without YGG_NODE is not the user.
def test_a_managed_session_without_a_node_cannot_cancel_or_seed(world, monkeypatch):
    from yggdrasil import cancel, ratatoskr, testmode
    package()
    monkeypatch.setenv("YGG_ACTIVATION", "act-0001")
    with pytest.raises(util.YggError) as caught:
        cancel.cancel("p1", ["WP-1"])
    assert caught.value.code == util.EXIT_REFUSED and "identity: only the PM of p1" in caught.value.message
    for call in (lambda: cancel.seed("p1", "WP-1"), lambda: ratatoskr.seed_inbox("tl1", 1),
                 lambda: testmode.acceptance("p1", "WP-1", "b" * 40)):
        with pytest.raises(util.YggError) as caught:
            call()
        assert caught.value.code == util.EXIT_REFUSED


# cancel.py:151 (blocker): the named packages are read under the lock.
def test_cancel_reads_the_named_packages_under_the_lock(world, monkeypatch):
    from yggdrasil import cancel, tracker
    package()
    held = {"in": False, "reads": []}
    real_locked, real_read = util.locked, tracker.read_issue

    @contextlib.contextmanager
    def locked(*args, **kwargs):
        was = held["in"]
        held["in"] = True
        try:
            with real_locked(*args, **kwargs):
                yield
        finally:
            held["in"] = was

    def read_issue(project, identifier):
        if identifier == "WP-1":
            held["reads"].append(held["in"])
        return real_read(project, identifier)

    monkeypatch.setattr(util, "locked", locked)
    monkeypatch.setattr(tracker, "read_issue", read_issue)
    with pytest.raises(util.YggError):
        cancel.cancel("p1", ["WP-1"])  # vault-docs home: refused once the checks reach the lane
    assert held["reads"] and all(held["reads"])


# cancel.py:363 (blocker): the preview lists nested repositories and folder symlinks the cleanup removes.
def test_the_preview_walk_lists_nested_git_and_folder_symlinks(tmp_path):
    from yggdrasil import cancel
    root = tmp_path / "lane"
    (root / "vendor/x/.git").mkdir(parents=True)
    (root / "vendor/x/.git/HEAD").write_text("ref: refs/heads/main\n")
    (root / "real").mkdir()
    (root / "real/f.txt").write_text("x")
    os.symlink(root / "real", root / "linked")
    (root / ".git").write_text("gitdir: elsewhere\n")
    found = set(cancel._walk(root))
    assert {"vendor/x/.git/HEAD", "linked", "real/f.txt"} <= found
    assert ".git" not in found and not any(p.startswith("linked/") for p in found)
    assert cancel._file_state(root, "linked")[0] == "120000"


# cancel.py:306 (major): status warns while a lane's remote copy still holds a dropped commit.
def test_status_warns_about_a_remote_holding_dropped_commits(tmp_path, monkeypatch):
    from yggdrasil import cancel
    home = tmp_path / "home"
    home.mkdir()
    git = ["git", "-C", str(home), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "C1"], check=True)
    c1 = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    class Drops:
        @staticmethod
        def drops(project):
            return [{"lane": "tl1", "commits": [c1], "completed_at": "x"}]

    monkeypatch.setattr(cancel, "_lanedrop", lambda: Drops)
    monkeypatch.setattr(cancel, "_home", lambda project: str(home))
    assert cancel.remote_warnings("p1") == []
    subprocess.run(git + ["update-ref", "refs/remotes/origin/ygg/p1/lane/tl1", c1], check=True)
    assert cancel.remote_warnings("p1") == ["lane tl1 was reset by a cancel; its remote copy still holds dropped "
                                            "commits and needs updating by hand"]


# lane 3 follow-up (A11.1 AC-13): a senior user's or worker's start aborts when its package was cancelled after it.
def test_the_start_check_covers_a_senior_users_testing_package(world):
    from yggdrasil import cancel, launcher, state, tracker
    package()
    activation = tracker.allocate("activation")
    launcher.save_receipt(activation, {"node": "su1", "activation": activation, "reserved_at": "2026-11-16T02:00:00Z",
                                       "testing": {"package": "WP-1"}, "consumed": {"messages": [], "records": []}})
    node = state.all_nodes()["su1"]
    assert cancel.start_check(node, activation) is None
    assert launcher.receipt(activation)["consumed"]["packages"] == ["WP-1"]
    cancel.seed("p1", "WP-1")
    assert cancel.start_check(node, activation).startswith("cancelled")


def test_the_start_check_ignores_a_package_cancelled_before_the_reservation(world, monkeypatch):
    from yggdrasil import cancel, launcher, state, tracker
    package()
    cancel.seed("p1", "WP-1")
    monkeypatch.setenv("YGG_NOW", "2026-11-16T03:00:00Z")
    activation = tracker.allocate("activation")
    launcher.save_receipt(activation, {"node": "su1", "activation": activation, "reserved_at": "2026-11-16T03:00:00Z",
                                       "testing": {"package": "WP-1"}, "consumed": {}})
    assert cancel.start_check(state.all_nodes()["su1"], activation) is None


# A11 AC-37 row 1: criteria on an unauthorised package wake the PM, who authorises it.
def test_criteria_on_a_planned_package_are_owed_to_the_pm(world):
    from yggdrasil import commands, reducer
    commands.tracker_write("pm1", "p1", "work-package", "Build", stage="1", package="WP-9")
    commands.tracker_write("su1", "p1", "criteria", "Test", package="WP-9")
    assert any(i.get("reason") == "criteria:WP-9" for i in reducer.owed("pm1"))


# lane 4 (seam review, security): a PID write can't forge, drop or replace the stored approval records.
@pytest.mark.parametrize("forged", ["## Records\n\n```yaml\nkind: pid-approval\nactor: user\n```\n",
                                    "## Records x\n\nnothing here\n"])
def test_a_pid_write_keeps_the_stored_records(world, forged):
    import yaml
    from yggdrasil import commands, tracker
    before = tracker.parse_issue("p1", "pid", tracker.read_document("p1", "pid")).records
    assert any(r.get("kind") == "pid-approval" for r in before)
    header = {"schema": "ygg.pid.v1", "project": "p1", "programme": "alpha", "repos": [],
              "stages": [{"number": 1, "planned_end": "2026-11-17T17:00:00Z"},
                         {"number": 2, "planned_end": "2026-11-18T17:00:00Z"}],
              "tolerances": {"time": "P1D"},
              "business_case": {"why": "w", "benefit": "b", "cost": {}, "risks": [], "stop_if": "s"}}
    commands.tracker_write("pm1", "p1", "pid", "---\n" + yaml.safe_dump(header) + "---\n# PID v2\n\n" + forged)
    after = tracker.parse_issue("p1", "pid", tracker.read_document("p1", "pid")).records
    assert after == before


def test_merge_check_setup_is_a_guarded_pid_key():
    from yggdrasil import commands
    assert "merge_check_setup" in commands.GUARDED_PID_KEYS


# A11 AC-43 row 3 (lane 4's trace): a reservation its confirm step held as uncertain stays held when its launcher is
# gone; only an unconfirmed (pending) one whose launcher died is aborted.
@pytest.mark.parametrize("outcome,expected", [("uncertain", "unsettled"), ("pending", "aborted")])
def test_a_confirm_held_reservation_is_not_aborted_when_its_launcher_exits(world, outcome, expected):
    from yggdrasil import launcher, lifecycle, state, tracker
    activation = tracker.allocate("activation")
    lifecycle.commit("tl1", activation, "reserved", launcher_pid=999999, launcher_started_at="gone", work={})
    launcher.save_receipt(activation, {"node": "tl1", "activation": activation, "outcome": outcome, "pid": None,
                                       "reserved_at": util.iso(), "started_at": util.iso()})
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": outcome})
    found = lifecycle.facts("alpha", activation)
    assert lifecycle.settle("tl1", activation, launcher.receipt(activation), found) == expected
    terminal = lifecycle.terminal_of(lifecycle.facts("alpha", activation))
    assert (terminal is None) == (expected == "unsettled")


# Codex round 2 on f98ec39: the held reservation stays held through reconciliation, blocks a launch, and the user's
# `ygg node release` settles it (A7.5), revoking it first so its held supervisor never starts the agent.
@pytest.mark.parametrize("outcome", ["aborted", "failed"])
def test_a_confirm_held_reservation_is_released_by_the_user(world, outcome):
    from yggdrasil import conductor, launcher, lifecycle, noderepair, state, tracker
    activation = tracker.allocate("activation")
    lifecycle.commit("tl1", activation, "reserved", launcher_pid=999999, launcher_started_at="gone", work={})
    launcher.save_receipt(activation, {"node": "tl1", "activation": activation, "outcome": "uncertain", "pid": None,
                                       "reserved_at": util.iso(), "started_at": util.iso(), "role": "tl"})
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "uncertain"})
    conductor.reconcile()
    assert lifecycle.terminal_of(lifecycle.facts("alpha", activation)) is None
    assert conductor.blocker(state.all_nodes()["tl1"], []) is not None
    assert noderepair.release("tl1", outcome) == "released tl1 as " + outcome + "\n"
    assert lifecycle.terminal_of(lifecycle.facts("alpha", activation)) == "released"
    assert lifecycle.revoked_path(activation).exists()
    assert state.runtime("tl1")["state"] == "idle"
    assert conductor.blocker(state.all_nodes()["tl1"], []) is None


def test_an_unconfirmed_reservation_whose_launcher_exited_is_still_aborted_by_reconciliation(world):
    from yggdrasil import conductor, launcher, lifecycle, state, tracker
    activation = tracker.allocate("activation")
    lifecycle.commit("tl1", activation, "reserved", launcher_pid=999999, launcher_started_at="gone", work={})
    launcher.save_receipt(activation, {"node": "tl1", "activation": activation, "outcome": "pending", "pid": None,
                                       "reserved_at": util.iso(), "started_at": util.iso(), "role": "tl"})
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "pending"})
    conductor.reconcile()
    assert lifecycle.terminal_of(lifecycle.facts("alpha", activation)) == "aborted"


# Codex round 2, review.py:636: a replay after a crash between the cap marker and the decision reuses the decision id.
def test_a_replayed_cap_opens_one_decision(world):
    from yggdrasil import review, tracker
    package()
    _set_state("in-review")
    _store(state="revised", verdict="revise", capped=True, effect="pending",
           findings=[{"file": "a.py", "line": 1, "text": "unsafe report path"}])
    data = review._load("reviews")
    data["packages"]["p1/WP-1"] = {"revises": 5, "allowed": 5, "cap_review": "rv-0001", "cap_decision": "D-0042"}
    review._save("reviews", data)  # as if the process died after the marker, before the decision was written
    review.replay_effects()
    _store(state="revised", verdict="revise", capped=True, effect="pending", findings=[])
    data = review._load("reviews")
    data["packages"]["p1/WP-1"] = {"revises": 5, "allowed": 5, "cap_review": "rv-0001", "cap_decision": "D-0042"}
    review._save("reviews", data)
    review.replay_effects()  # and once more, after the decision was written
    decisions = [i for i in tracker.issues("p1") if i.get("kind") == "decision" and i.get("review") == "rv-0001"]
    assert [i.id for i in decisions] == ["D-0042"]


# Codex round 2, review.py:618: a replayed approval never acts over a later transition.
def test_a_replayed_approval_leaves_a_rejected_package_alone(world):
    from yggdrasil import review, tracker
    package()
    _set_state("rejected")
    _store(state="approved", verdict="approve", effect="pending")
    review.replay_effects()
    assert tracker.read_issue("p1", "WP-1").get("state") == "rejected"
    assert review.get("rv-0001")["effect"] == "done"


# Codex round 2, review.py:600: the findings message names its package, so a cancel settles it.
def test_a_findings_message_names_its_package_and_a_cancel_settles_it(world):
    from yggdrasil import cancel, review
    package()
    _set_state("in-review")
    _store(state="revised", verdict="revise", effect="pending",
           findings=[{"file": "a.py", "line": 1, "text": "unsafe report path"}])
    review.replay_effects()
    found = [m for m in _messages("tl1") if "unsafe report path" in m["body"]]
    assert len(found) == 1 and "WP-1" in found[0]["body"]
    _set_state("cancelled")
    cancel.settle_messages("p1")
    assert not [m for m in _messages("tl1") if "unsafe report path" in m["body"]]


def _two_reviews(first_fields):
    from yggdrasil import review
    _store(**first_fields)
    data = review._load("reviews")
    newer = dict(data["reviews"]["rv-0001"], id="rv-0002", state="queued", effect=None, head="c" * 40)
    data["reviews"]["rv-0002"] = newer
    data["counter"] = 2
    review._save("reviews", data)


# Codex round 3, review.py:642: an older review's pending effect never acts once a newer review owns the package.
def test_a_replayed_approval_does_not_route_a_newer_review(world):
    from yggdrasil import review, tracker
    package()
    _set_state("in-review")
    _two_reviews({"state": "approved", "verdict": "approve", "effect": "pending"})
    review.replay_effects()
    issue = tracker.read_issue("p1", "WP-1")
    assert issue.get("state") == "in-review" and issue.get("reviewed_head") is None
    assert review.get("rv-0001")["effect"] == "done"


def test_a_replayed_revise_does_not_reset_a_newer_review(world):
    from yggdrasil import review, tracker
    package()
    _set_state("in-review")
    _two_reviews({"state": "revised", "verdict": "revise", "effect": "pending",
                  "findings": [{"file": "a.py", "line": 1, "text": "unsafe report path"}]})
    review.replay_effects()
    assert tracker.read_issue("p1", "WP-1").get("state") == "in-review"
    assert not [m for m in _messages("tl1") if "unsafe report path" in m["body"]]


def test_an_applied_effect_is_never_applied_again_whatever_the_state(world):
    from yggdrasil import review, tracker
    package()
    _set_state("in-review")
    _store(state="approved", verdict="approve", effect="pending")
    review.apply_effect("rv-0001")
    _set_state("in-review")  # as if a later step had put it back in review
    _store(state="approved", verdict="approve", effect="pending")
    review.replay_effects()
    approvals = [r for r in tracker.read_issue("p1", "WP-1").records if r.get("kind") == "review-approved"]
    assert len(approvals) == 1


# Codex round 3, review.py:690: a crash after the cap opened, then the answer `one more round`, then the replay: the
# findings stay owed and the package stays authorised.
def test_a_replayed_cap_keeps_the_one_more_round_findings(world):
    from yggdrasil import decisions, review, tracker
    package()
    _set_state("in-review")
    findings = [{"file": "a.py", "line": 1, "text": "unsafe report path"}]
    _store(state="revised", verdict="revise", capped=True, effect="pending", findings=findings)
    review._open_cap(review.get("rv-0001"))  # the cap opened; the process died before effect=done
    decision = review.package_info("p1", "WP-1")["cap_decision"]
    decisions.decide(decision, "one more round", None)
    review.step()
    assert tracker.read_issue("p1", "WP-1").get("state") == "authorised"
    assert len([m for m in _messages("tl1") if "unsafe report path" in m["body"]]) == 1
    review.step()
    assert len([m for m in _messages("tl1") if "unsafe report path" in m["body"]]) == 1


# Codex round 3, noderepair.py:30: a delayed supervisor that records `spawned` before the revocation wins; the release
# is refused and nothing is committed.
def test_a_release_that_races_a_delayed_supervisor_is_refused(world, monkeypatch):
    from yggdrasil import launcher, lifecycle, noderepair, state, tracker
    activation = tracker.allocate("activation")
    lifecycle.commit("tl1", activation, "reserved", launcher_pid=999999, launcher_started_at="gone", work={})
    launcher.save_receipt(activation, {"node": "tl1", "activation": activation, "outcome": "uncertain", "pid": None,
                                       "reserved_at": util.iso(), "started_at": util.iso(), "role": "tl"})
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "uncertain"})
    real_revoke = lifecycle.revoke

    def revoke_after_the_supervisor_spawned(key, why):
        lifecycle.commit("tl1", key, "spawned", supervisor_pid=os.getpid(),
                         supervisor_started_at=util.process_start(os.getpid()), agent_nonce="n")
        real_revoke(key, why)

    monkeypatch.setattr(lifecycle, "revoke", revoke_after_the_supervisor_spawned)
    with pytest.raises(util.YggError) as caught:
        noderepair.release("tl1", "aborted")
    assert caught.value.code == util.EXIT_REFUSED and "live activation" in caught.value.message
    assert lifecycle.terminal_of(lifecycle.facts("alpha", activation)) is None


# Codex round 4, review.py:711: a capped effect left pending by a crash is retired once a newer review owns the package
# (suspended, reopened, readied again); the reopen message stays owed, no decision opens, the cap count is unchanged.
def test_a_superseded_capped_effect_is_retired_untouched(world):
    from yggdrasil import ratatoskr, review, tracker
    package()
    _set_state("in-review")
    findings = [{"file": "a.py", "line": 1, "text": "unsafe report path"}]
    _store(state="revised", verdict="revise", capped=True, effect="pending", findings=findings)
    data = review._load("reviews")
    data["packages"]["p1/WP-1"] = {"revises": 5, "allowed": 5}
    data["reviews"]["rv-0002"] = dict(data["reviews"]["rv-0001"], id="rv-0002", state="queued", effect=None,
                                      capped=False, findings=[])
    data["counter"] = 2
    review._save("reviews", data)  # crash, then suspended, reopened and readied again: rv-0002 is current
    reopen = ratatoskr.send("pm1", "tl1", "work", "Work package WP-1 reopened by the PM", internal=True)
    review.replay_effects()
    assert reopen in [m["msg_id"] for m in _messages("tl1")]
    assert not [i for i in tracker.issues("p1") if i.get("kind") == "decision"]
    info = review.package_info("p1", "WP-1")
    assert info.get("revises") == 5 and "cap_decision" not in info
    assert review.get("rv-0001")["effect"] == "done" and review.get("rv-0001")["effect_note"] == "superseded"
    assert tracker.read_issue("p1", "WP-1").get("state") == "in-review"


# Landing review finding 2 (review.py:328): a crash right after the review is saved, before the package enters review,
# is finished by recovery; the later approval then reaches ready-for-test with testing owed.
def test_an_interrupted_review_opening_is_finished_by_recovery(world, monkeypatch):
    from yggdrasil import review, tracker
    package()
    monkeypatch.setattr(review, "programme_kind", lambda project: "git-repo")
    monkeypatch.setattr(review, "project_reviewer", lambda project, nodes=None: {"id": "pm1rv1"})
    monkeypatch.setattr(review, "lane_head", lambda project, lane: "b" * 40)
    monkeypatch.setattr(review, "merge_base", lambda project, head: "a" * 40)
    monkeypatch.setattr(review, "_trusted_recheck", lambda *args: None)
    real_finish = review.finish_openings

    def crash():
        raise SystemExit("killed right after the review was saved")

    monkeypatch.setattr(review, "finish_openings", crash)
    with pytest.raises(SystemExit):
        review.ready("tl1", "WP-1")
    assert tracker.read_issue("p1", "WP-1").get("state") == "authorised"
    assert review.get("rv-0001")["opening"]
    monkeypatch.setattr(review, "finish_openings", real_finish)
    review.assign()  # recovery: the next tick's reviews step
    issue = tracker.read_issue("p1", "WP-1")
    assert issue.get("state") == "in-review" and "opening" not in review.get("rv-0001")
    assert review.get("rv-0001")["state"] == "assigned"
    data = review._load("reviews")
    data["reviews"]["rv-0001"].update(state="approved", verdict="approve", effect="pending")
    review._save("reviews", data)
    review.replay_effects()
    issue = tracker.read_issue("p1", "WP-1")
    assert issue.get("state") == "ready-for-test"
    assert any(s.get("node") == "su1" and s.get("reason") == "ready-for-test:WP-1" for s in issue.header.get("owed") or [])


def test_an_interrupted_opening_on_a_package_cancelled_since_is_abandoned(world, monkeypatch):
    from yggdrasil import cancel, review, tracker
    package()
    _store(state="queued", opening={"by": "tl1", "via": "ready"}, opened_at="2026-11-16T01:00:00Z")
    cancel.seed("p1", "WP-1")
    review.assign()
    assert tracker.read_issue("p1", "WP-1").get("state") == "cancelled"
    assert review.get("rv-0001")["state"] == "abandoned"


# Codex on the landing fix, review.py:364: a reopen in the same second as the opening intent is still seen (record
# position, not time); the review is abandoned and the package keeps its reopened state.
def test_a_reopen_in_the_same_second_as_the_opening_abandons_it(world):
    from yggdrasil import review, tracker
    package()
    issue = tracker.read_issue("p1", "WP-1")
    _store(state="queued", opening={"by": "tl1", "via": "ready", "records_at": len(issue.records)},
           opened_at=util.iso())
    issue.records.append(tracker.record("reopened", "pm1"))  # same YGG_NOW second as the intent
    issue.save()
    review.assign()
    issue = tracker.read_issue("p1", "WP-1")
    assert issue.get("state") == "authorised"
    assert not any(r.get("kind") == "review-opened" for r in issue.records)
    assert review.get("rv-0001")["state"] == "abandoned"
    assert not any(s.get("review") == "rv-0001" for s in issue.header.get("owed") or [])


# Codex on the landing fix, review.py:373: opened, intent not yet cleared, then suspended: recovery revalidates, the
# review is abandoned and assign stamps nothing.
def test_an_opened_review_whose_package_was_suspended_is_not_assigned(world):
    from yggdrasil import review, tracker
    package()
    issue = tracker.read_issue("p1", "WP-1")
    records_at = len(issue.records)
    issue.header["state"] = "in-review"
    issue.records.append(tracker.record("review-opened", "tl1", review="rv-0001", base="a" * 40, head="b" * 40))
    issue.save()
    _store(state="queued", opening={"by": "tl1", "via": "ready", "records_at": records_at})
    _set_state("suspended")  # a morning-go `not ok`, before the intent was cleared
    review.assign()
    issue = tracker.read_issue("p1", "WP-1")
    assert issue.get("state") == "suspended"
    assert review.get("rv-0001")["state"] == "abandoned"
    assert not any(s.get("review") == "rv-0001" for s in issue.header.get("owed") or [])


def test_assign_revalidates_a_queued_review_under_its_lock(world):
    from yggdrasil import review, tracker
    package()
    _set_state("suspended")
    _store(state="queued")
    review.assign()
    issue = tracker.read_issue("p1", "WP-1")
    assert review.get("rv-0001")["state"] == "abandoned"
    assert not any(s.get("review") == "rv-0001" for s in issue.header.get("owed") or [])
