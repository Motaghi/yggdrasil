"""A11 lane 4, review round 1: the merge-lane and merge-main races, divergent accepted heads, crash-safe
merge-check effects, the corrupt delivery journal, old alerts, setup that changes the staged work, and
merge_check_setup only from the approved snapshot."""

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from test_round2 import world  # noqa: F401  (shared fixture: a real files tracker with pm1)

from yggdrasil import alertdelivery, config, gitops, mergecheck, state, util


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text, message):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "t")
    git(path, "config", "user.email", "t@example.invalid")
    commit(path, "a.txt", "a\n", "base")
    return path


def journal_depth():
    return util._held()[0].get("journal", 0)


# ---------------------------------------------------------------- races (journal lock across check and action)

def test_merge_lane_runs_its_gate_and_merge_under_the_journal_lock(repo, monkeypatch):
    git(repo, "branch", "ygg/p1/lane/tl1")
    git(repo, "branch", "ygg/p1/_integration")
    seen = []
    monkeypatch.setattr("yggdrasil.commands._project_for", lambda name: {"id": name, "pm": "pm1"})
    monkeypatch.setattr(gitops, "_repo_entry", lambda project, path: {"path": str(path)})
    monkeypatch.setattr(util, "helper_refusal", lambda path, label: None)
    monkeypatch.setattr(mergecheck, "lane_gate", lambda *args: seen.append(("gate", journal_depth())))

    def merge(*args, **kwargs):
        seen.append(("merge", journal_depth()))
        return "sha"
    monkeypatch.setattr(gitops, "_merge_into", merge)
    monkeypatch.setattr("yggdrasil.tracker.issues", lambda project: [])
    monkeypatch.setattr("yggdrasil.envelopes.effective_envelope", lambda project: None)
    monkeypatch.setattr("yggdrasil.t3.close_lane", lambda project, lane: None)
    gitops.merge_lane("p1", "tl1", str(repo))
    assert seen == [("gate", 1), ("merge", 1)]


def test_merge_main_checks_its_pass_under_the_journal_lock(monkeypatch):
    seen = []
    monkeypatch.setattr(state, "check_user_only", lambda verb: None)
    monkeypatch.setattr("yggdrasil.commands._project_for", lambda name: {"id": name})
    monkeypatch.setattr(mergecheck, "resolve_repo", lambda project, repo: "/repo")
    monkeypatch.setattr(util, "helper_refusal", lambda path, label: None)

    def passed(project, path, repo):
        seen.append(("pass", journal_depth()))
        return ("m" * 40, "i" * 40)
    monkeypatch.setattr(mergecheck, "_passed_pair", passed)

    def merge(repo, target, source, message, **kwargs):
        seen.append(("merge", journal_depth(), kwargs.get("expect")))
        return "new"
    monkeypatch.setattr(gitops, "_merge_into", merge)
    mergecheck.merge_main("p1", None)
    assert seen == [("pass", 1), ("merge", 1, "m" * 40)]


def test_merge_into_refuses_when_the_target_moved_since_the_pass(repo):
    git(repo, "checkout", "-q", "-b", "work")
    work = commit(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "main")
    passed_main = git(repo, "rev-parse", "main")
    moved = commit(repo, "c.txt", "c\n", "main moved")
    with pytest.raises(util.YggError) as error:
        gitops._merge_into(repo, "main", work, "merge", no_ff=True, expect=passed_main)
    assert error.value.code == util.EXIT_REFUSED and "moved since its merge check passed" in error.value.message
    assert git(repo, "rev-parse", "main") == moved


# ---------------------------------------------------------------- divergent accepted heads (ruling 137)

def test_divergent_accepted_heads_are_refused(repo):
    base = git(repo, "rev-parse", "HEAD")
    first = commit(repo, "b.txt", "b\n", "WP-1")
    git(repo, "checkout", "-q", base)
    second = commit(repo, "c.txt", "c\n", "WP-2")
    pending = [("WP-1", first, "2026-11-19T12:00:00Z"), ("WP-2", second, "2026-11-19T12:05:00Z")]
    with pytest.raises(util.YggError) as error:
        mergecheck._newest(str(repo), pending)
    assert "the lane head isn't the accepted commit" in error.value.message and "WP-1" in error.value.message


def test_the_head_holding_every_acceptance_is_the_newest(repo):
    first = commit(repo, "b.txt", "b\n", "WP-1")
    second = commit(repo, "c.txt", "c\n", "WP-2")
    pending = [("WP-1", first, "2026-11-19T12:05:00Z"), ("WP-2", second, "2026-11-19T12:00:00Z")]
    assert mergecheck._newest(str(repo), pending) == second


# ---------------------------------------------------------------- crash-safe merge-check effects (A7 I4)

@pytest.fixture
def effects(monkeypatch):
    sent = []
    seen = {}

    def send(sender, recipient, kind, body, **kwargs):
        dispatch = kwargs.get("dispatch")
        if dispatch and dispatch in seen:
            return seen[dispatch]  # as ratatoskr dedupes on the journal entry for a dispatch id
        sent.append(body)
        identifier = f"m-{len(sent):04d}"
        items = state.inbox(recipient)
        items.append({"msg_id": identifier, "kind": kind, "body": body, "acked_at": None})
        state.save_inbox(recipient, items)
        if dispatch:
            seen[dispatch] = identifier
        return identifier
    monkeypatch.setattr("yggdrasil.ratatoskr.send", send)
    monkeypatch.setattr(mergecheck, "_back_to_executing", lambda project: None)
    return sent


def failed_record():
    return {"id": "p1-abc", "project": "p1", "main": "m" * 40, "integration": "i" * 40, "state": "running"}


def test_a_failure_whose_effects_were_cut_short_is_finished_once(effects, monkeypatch):
    project = {"id": "p1", "pm": "pm1"}
    original = state.alert

    def crash(kind, message, **fields):
        raise util.YggError(util.EXIT_REFUSED, "crash before the alert")
    monkeypatch.setattr(state, "alert", crash)
    data = failed_record()
    mergecheck.save_record("p1", dict(data))  # the running check owns the record
    with pytest.raises(util.YggError):
        mergecheck._finish(project, data, {"state": "failed", "reason": "the project's checks failed",
                                           "tail": "6 tests failed"})
    assert mergecheck.record("p1")["effects_pending"] is True
    monkeypatch.setattr(state, "alert", original)
    mergecheck._failure_effects(project, mergecheck.record("p1"))
    mergecheck._failure_effects(project, mergecheck.record("p1"))
    assert len(effects) == 1 and "6 tests failed" in effects[0]
    assert len([a for a in state.alerts() if a["kind"] == "ygg-merge-check-failed"]) == 1
    assert mergecheck.record("p1")["effects_pending"] is False


def test_the_merge_ready_alert_is_raised_once_per_entry():
    data = {"main": "m" * 40, "integration": "i" * 40}
    mergecheck._ensure_ready_alert("p1", data)
    mergecheck._ensure_ready_alert("p1", data)
    assert len([a for a in state.alerts() if a["kind"] == "ygg-merge-ready"]) == 1
    state.alert("ygg-merge-check-failed", "failed", project="p1")
    mergecheck._ensure_ready_alert("p1", data)
    assert len([a for a in state.alerts() if a["kind"] == "ygg-merge-ready"]) == 2


# ---------------------------------------------------------------- alert delivery journal and backlog

@pytest.fixture
def auto(monkeypatch):
    values = {"alert_forwarder": "auto", "alert_t3_thread": None, "alert_macos": "on", "alert_max_attempts": 10,
              "alert_send_timeout_seconds": 5, "alert_tick_budget_seconds": 30, "tick_seconds": 15}
    monkeypatch.setattr(config, "get", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(alertdelivery, "escalate_uncertain", lambda: None)
    return values


def test_a_corrupt_delivery_journal_stops_delivery_and_is_reported(auto, monkeypatch):
    state.alert("ygg-launch-refused", "Launch of pm1tl1 refused: x", node="pm1tl1")
    util.ensure_dir(state.root())
    alertdelivery.delivered_path().write_text('{"alert": "al-1", "channel": "t3", "step": "timed-out"}\n{"alert"')
    monkeypatch.setattr(alertdelivery, "_send", lambda *args: pytest.fail("nothing is sent from a corrupt journal"))
    alertdelivery.tick_step()
    alertdelivery.tick_step()
    assert len([a for a in state.alerts() if a["kind"] == "ygg-corrupt-evidence"]) == 1
    assert alertdelivery.status_lines() == [alertdelivery.CORRUPT]


def test_an_old_undelivered_alert_is_still_delivered(auto, monkeypatch):
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.setenv("YGG_NOW", old)
    state.alert("ygg-stage-approval", "Stage 1 waits", project="p1")
    monkeypatch.delenv("YGG_NOW")
    calls = []
    def send(argv, stdin, timeout, alert_id, started=None):
        calls.append(argv)
        if started:
            started()
        return "delivered"
    monkeypatch.setattr(alertdelivery, "_send", send)
    alertdelivery.tick_step()
    assert len(calls) == 1 and calls[0][0] == "osascript"
    rows = [json.loads(line) for line in alertdelivery.delivered_path().read_text().splitlines()]
    assert rows[-1]["step"] == "delivered"


# ---------------------------------------------------------------- merge check: setup boundary and staged work

def test_merge_check_setup_comes_only_from_the_approved_snapshot(monkeypatch):
    monkeypatch.setattr(config, "project_get", lambda key, project, default=None: ["/tmp/unapproved"])
    monkeypatch.setattr("yggdrasil.commands.approved_pid", lambda project: {"guarded": {"repos": []}})
    assert mergecheck.approved_setup("p1") is None
    monkeypatch.setattr("yggdrasil.commands.approved_pid",
                        lambda project: {"guarded": {"merge_check_setup": ["/opt/approved"]}})
    assert mergecheck.approved_setup("p1") == ["/opt/approved"]


@pytest.mark.parametrize("hook", [False, True])
def test_setup_that_discards_the_staged_merge_fails_the_check(repo, tmp_path, monkeypatch, hook):
    main = git(repo, "rev-parse", "main")
    git(repo, "checkout", "-q", "-b", "ygg/p1/_integration")
    integration = commit(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "main")
    if hook:
        (repo / ".git/hooks/pre-commit").write_text("#!/bin/sh\nexit 0\n")
        (repo / ".git/hooks/pre-commit").chmod(0o755)
    setup = tmp_path / "setup"
    setup.write_text("#!/bin/sh\ngit reset -q --hard HEAD\n")
    setup.chmod(0o755)
    monkeypatch.setattr(mergecheck, "approved_setup", lambda project: [str(setup)])
    monkeypatch.setattr(util, "helper_refusal", lambda path, label: None)
    result = mergecheck.run_check("p1", str(repo), main, integration)
    assert result["state"] == "failed" and result["reason"] == "the project's checks changed files"


# ---------------------------------------------------------------- the lane-merge refusal carries both texts

def test_an_unaccepted_lane_package_refusal_names_the_head_and_the_package(monkeypatch):
    class Issue:
        id, records = "WP-41", []

        def get(self, key, default=None):
            return {"kind": "work-package", "lane": "pm1tl1", "state": "in-review"}.get(key, default)
    monkeypatch.setattr("yggdrasil.commands.paired_seniors", lambda lane, nodes: ["pm1su1"])
    monkeypatch.setattr(state, "all_nodes", dict)
    monkeypatch.setattr("yggdrasil.tracker.issues", lambda project: [Issue()])
    monkeypatch.setattr(mergecheck, "_refuse_interrupted_cancel", lambda project, lane: None)
    with pytest.raises(util.YggError) as error:
        mergecheck.lane_gate("alpha-p1", "pm1tl1", "/repo", "b" * 40)
    assert error.value.message.startswith("REFUSED: no acceptance for " + "b" * 40)
    assert "WP-41 on lane pm1tl1 is not accepted" in error.value.message


def test_merge_main_print_keeps_the_prepare_mode_text(monkeypatch):
    monkeypatch.setattr(state, "check_user_only", lambda verb: None)
    monkeypatch.setattr("yggdrasil.commands._project_for", lambda name: {"id": name})
    monkeypatch.setattr(mergecheck, "resolve_repo", lambda project, repo: "/repo")
    monkeypatch.setattr(util, "helper_refusal", lambda path, label: None)
    monkeypatch.setattr(mergecheck, "_passed_pair", lambda project, path, repo: ("m" * 40, "i" * 40))
    monkeypatch.setattr(gitops, "merge_mode", lambda project: "prepare")
    output = mergecheck.merge_main("alpha-p1", None, print_only=True)
    assert output.startswith("Merge mode prepare: main is unchanged.")
    assert "git -C /repo merge --no-ff ygg/alpha-p1/_integration" in output


# ---------------------------------------------------------------- review round 2

def test_merge_main_merges_from_the_passed_commit_with_a_compare_and_swap(repo):
    git(repo, "checkout", "-q", "-b", "work")
    work = commit(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "main")
    passed = git(repo, "rev-parse", "main")
    new = gitops._merge_into(repo, "main", work, "merge", no_ff=True, expect=passed)
    assert git(repo, "rev-parse", "main") == new
    assert git(repo, "rev-list", "--parents", "-1", new).split()[1:] == [passed, work]
    assert (repo / "b.txt").read_text() == "b\n" and git(repo, "status", "--porcelain") == ""


def test_merge_main_refuses_when_main_moves_after_its_first_check(repo, monkeypatch):
    git(repo, "checkout", "-q", "-b", "work")
    work = commit(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "main")
    passed = git(repo, "rev-parse", "main")
    real = gitops.checkout_of
    moved = []

    def commit_meanwhile(path, branch):
        if not moved:  # once, after the first look and before the swap
            moved.append(commit(repo, "c.txt", "c\n", "a plain commit lands on main meanwhile"))
        return real(path, branch)
    monkeypatch.setattr(gitops, "checkout_of", commit_meanwhile)
    with pytest.raises(util.YggError) as error:
        gitops._merge_into(repo, "main", work, "merge", no_ff=True, expect=passed)
    assert "moved since its merge check passed" in error.value.message
    assert git(repo, "rev-parse", "main") == moved[0]
    assert not (repo / "b.txt").exists() and git(repo, "status", "--porcelain") == ""


def pending_failure():
    return {"id": "p1-old", "project": "p1", "main": "m" * 40, "integration": "i" * 40, "state": "failed",
            "reason": "the project's checks failed", "effects_pending": True, "tail": "6 tests failed"}


def test_a_record_with_pending_effects_is_never_replaced():
    mergecheck.save_record("p1", pending_failure())
    with pytest.raises(util.YggError):
        mergecheck.save_record("p1", {"id": "p1-new", "state": "running"})
    assert mergecheck.record("p1")["id"] == "p1-old"


def test_a_manual_check_first_finishes_the_pending_effects(effects, monkeypatch):
    mergecheck.save_record("p1", pending_failure())
    monkeypatch.setattr("yggdrasil.commands._project_for", lambda name: {"id": name, "pm": "pm1"})
    monkeypatch.setattr(gitops, "_caller_is_pm", lambda project: None)
    monkeypatch.setattr(mergecheck, "resolve_repo", lambda project, repo: "/repo")
    monkeypatch.setattr(mergecheck, "pair", lambda path, project: ("a" * 40, "b" * 40))
    monkeypatch.setattr(mergecheck, "run_check", lambda *args: {"state": "passed", "reason": "", "tail": "",
                                                                "finished_at": util.iso()})
    mergecheck.check_verb("p1", None)
    assert len(effects) == 1 and "(check p1-old)" in effects[0]
    assert [a["check"] for a in state.alerts() if a["kind"] == "ygg-merge-check-failed"] == ["p1-old"]
    assert mergecheck.record("p1")["state"] == "passed"


def test_racing_effect_runs_send_one_message_and_one_alert(effects, monkeypatch):
    import threading
    import time
    mergecheck.save_record("p1", pending_failure())
    send = __import__("yggdrasil.ratatoskr", fromlist=["send"]).send

    def slow(*args, **kwargs):
        time.sleep(0.2)
        return send(*args, **kwargs)
    monkeypatch.setattr("yggdrasil.ratatoskr.send", slow)
    project = {"id": "p1", "pm": "pm1"}
    threads = [threading.Thread(target=mergecheck._failure_effects, args=(project, pending_failure()))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(effects) == 1
    assert len([a for a in state.alerts() if a["kind"] == "ygg-merge-check-failed"]) == 1


# ---------------------------------------------------------------- review round 3

def test_a_crash_right_after_publication_is_finished_by_the_next_command(repo, monkeypatch):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    git(repo, "checkout", "-q", "-b", "work")
    work = commit(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "main")
    passed = git(repo, "rev-parse", "main")
    real = gitops._finish_publication

    def crash(intent):
        raise SystemExit("killed between publishing main and updating its checkout")
    monkeypatch.setattr(gitops, "_finish_publication", crash)
    with pytest.raises(SystemExit):
        gitops._merge_into(repo, "main", work, "merge", no_ff=True, expect=passed)
    merged = git(repo, "rev-parse", "main")
    assert merged != passed and not (repo / "b.txt").exists()  # published, checkout still at the old commit
    monkeypatch.setattr(gitops, "_finish_publication", real)
    gitops.finish_publications()
    assert (repo / "b.txt").read_text() == "b\n" and git(repo, "status", "--porcelain") == ""
    assert not list((state.root() / "merge-publish").glob("*.json"))


def test_the_project_waits_while_an_interrupted_merge_cant_be_finished(repo, monkeypatch):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    git(repo, "checkout", "-q", "-b", "work")
    work = commit(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "main")
    passed = git(repo, "rev-parse", "main")
    real = gitops._finish_publication
    monkeypatch.setattr(gitops, "_finish_publication", lambda intent: (_ for _ in ()).throw(SystemExit("crash")))
    with pytest.raises(SystemExit):
        gitops._merge_into(repo, "main", work, "merge", no_ff=True, expect=passed)
    monkeypatch.setattr(gitops, "_finish_publication", real)
    (repo / "a.txt").write_text("someone else's edit\n")
    monkeypatch.setattr(mergecheck, "approved_repo_paths", lambda project: [str(repo)])
    monkeypatch.setattr("yggdrasil.tracker.project", lambda name: pytest.fail("the project must not close"))
    with pytest.raises(util.YggError) as error:
        mergecheck._close("p1")
    assert "interrupted merge" in error.value.message
    assert list((state.root() / "merge-publish").glob("*.json"))


def test_a_replacement_and_a_finish_never_lose_a_failure(effects, monkeypatch):
    import threading
    old = {"id": "p1-old", "project": "p1", "main": "m" * 40, "integration": "i" * 40, "state": "running"}
    mergecheck.save_record("p1", old)
    inside, release = threading.Event(), threading.Event()
    real_record = mergecheck.record
    calls = {"n": 0}

    def paused_record(project_id):
        found = real_record(project_id)
        calls["n"] += 1
        if calls["n"] == 1:  # the replacement has read the old running record: hold it here
            inside.set()
            release.wait(2)
        return found
    monkeypatch.setattr(mergecheck, "record", paused_record)
    errors = []
    replacement = threading.Thread(target=lambda: mergecheck.save_record("p1", {"id": "p1-new", "state": "running"}))
    replacement.start()
    inside.wait(2)
    finisher = threading.Thread(target=lambda: _run(errors, mergecheck._finish, {"id": "p1", "pm": "pm1"}, dict(old),
                                                    {"state": "failed", "reason": "the project's checks failed"}))
    finisher.start()
    release.set()
    replacement.join()
    finisher.join()
    assert not errors
    final = real_record("p1")
    assert final["id"] == "p1-new" and not final.get("effects_pending")  # the old check was superseded first
    assert effects == []


def _run(errors, function, *args):
    try:
        function(*args)
    except Exception as error:  # noqa: BLE001
        errors.append(error)


def test_a_crash_after_the_journal_write_sends_one_message_through_merge_check(world, monkeypatch):  # noqa: F811
    from yggdrasil import ratatoskr  # noqa: F401  (the real one: dedupe on the durable journal entry)
    monkeypatch.setattr(mergecheck, "_back_to_executing", lambda project: None)
    project = {"id": "p1", "pm": "pm1"}
    mergecheck.save_record("p1", pending_failure() | {"project": "p1"})
    real = state.save_inbox

    def crash(node_id, items):
        raise SystemExit("killed after the inbox journal write, before the local inbox")
    monkeypatch.setattr(state, "save_inbox", crash)
    with pytest.raises(SystemExit):
        mergecheck._failure_effects(project, mergecheck.record("p1"))
    monkeypatch.setattr(state, "save_inbox", real)
    monkeypatch.setattr("yggdrasil.commands._project_for", lambda name: project)
    monkeypatch.setattr(gitops, "_caller_is_pm", lambda p: None)
    monkeypatch.setattr(mergecheck, "resolve_repo", lambda p, r: "/repo")
    monkeypatch.setattr(mergecheck, "pair", lambda path, p: ("a" * 40, "b" * 40))
    monkeypatch.setattr(mergecheck, "run_check", lambda *args: {"state": "passed", "reason": "", "tail": "",
                                                                "finished_at": util.iso()})
    mergecheck.check_verb("p1", None)
    marker = "(check p1-old)"
    assert len([i for i in state.inbox("pm1") if marker in str(i.get("body"))]) == 1
    assert len([a for a in state.alerts() if a["kind"] == "ygg-merge-check-failed"]) == 1


# ---------------------------------------------------------------- forged publication intents (security review)

FORGERIES = {
    "branch": {"branch": "release"},
    "expect": {"expect": "HEAD~1"},
    "merged": {"merged": "abc123"},
    "repo": {"repo": "/somewhere/else"},
    "filename": {},
    "location": {"location": {"not": "a path"}},
}


@pytest.mark.parametrize("field", sorted(FORGERIES))
def test_a_forged_intent_is_set_aside_and_never_acted_on(repo, monkeypatch, field):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    sha = git(repo, "rev-parse", "HEAD")
    intent = {"repo": str(repo), "branch": "main", "expect": sha, "merged": sha, "location": str(repo)}
    intent.update(FORGERIES[field])
    path = gitops._intent_path(repo, "main")
    if field == "filename":
        path = path.with_name("0000000000000000.json")
    path.write_text(json.dumps(intent))
    calls = []
    real = gitops.git
    monkeypatch.setattr(gitops, "git", lambda *args: calls.append(args[1:]) or real(*args))
    monkeypatch.setattr(gitops, "_finish_publication", lambda found: pytest.fail("a forged intent was acted on"))
    gitops.finish_publications()
    assert not path.exists() and (path.parent / "rejected" / path.name).exists()
    rejected = [a for a in state.alerts() if a["kind"] == "ygg-merge-intent-rejected"]
    assert len(rejected) == 1 and rejected[0]["class"] == "log" and path.name in rejected[0]["message"]
    assert calls == []  # nothing the file names reaches git before its repo, branch, ids and path check out


def test_a_sha256_intent_id_counts_as_a_full_commit_id(repo, monkeypatch):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    long = "a" * 64
    monkeypatch.setattr(gitops, "git", lambda *args: subprocess.CompletedProcess(args, 0, long + "\n", ""))
    intent = {"repo": str(repo), "branch": "main", "expect": long, "merged": long, "location": str(repo)}
    assert gitops._intent_problem(gitops._intent_path(repo, "main"), intent) is None


# ---------------------------------------------------------------- review round 4

def detached_home_with_work(repo):
    git(repo, "checkout", "-q", "-b", "work")
    work = commit(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "--detach", "main")  # main is checked out nowhere at the first check
    return work, git(repo, "rev-parse", "main")


def checkout_main_before_the_swap(repo, monkeypatch):
    real = gitops.git

    def git_spy(path, *args):
        result = real(path, *args)
        if args[:2] == ("worktree", "prune"):  # the temporary merge worktree is gone; the swap comes next
            subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
        return result
    monkeypatch.setattr(gitops, "git", git_spy)


def crash_after_the_swap(monkeypatch):
    real = gitops._finish_publication
    monkeypatch.setattr(gitops, "_finish_publication",
                        lambda intent: (_ for _ in ()).throw(SystemExit("killed after the swap")))
    return real


def test_a_checkout_made_before_the_swap_is_repaired_after_a_crash(repo, monkeypatch):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    work, passed = detached_home_with_work(repo)
    checkout_main_before_the_swap(repo, monkeypatch)
    real = crash_after_the_swap(monkeypatch)
    with pytest.raises(SystemExit):
        gitops._merge_into(repo, "main", work, "merge", no_ff=True, expect=passed)
    intent = json.loads(gitops._intent_path(repo, "main").read_text())
    assert os.path.realpath(intent["location"]) == os.path.realpath(repo)  # found again just before the swap
    monkeypatch.setattr(gitops, "_finish_publication", real)
    monkeypatch.setattr(gitops, "git", subprocess_free_git())
    gitops.finish_publications()
    assert (repo / "b.txt").read_text() == "b\n" and git(repo, "status", "--porcelain") == ""
    assert not gitops._intent_path(repo, "main").exists()


def test_an_interrupted_merge_is_never_cleared_while_the_checkout_holds_other_work(repo, monkeypatch):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    work, passed = detached_home_with_work(repo)
    checkout_main_before_the_swap(repo, monkeypatch)
    real = crash_after_the_swap(monkeypatch)
    with pytest.raises(SystemExit):
        gitops._merge_into(repo, "main", work, "merge", no_ff=True, expect=passed)
    monkeypatch.setattr(gitops, "_finish_publication", real)
    monkeypatch.setattr(gitops, "git", subprocess_free_git())
    (repo / "a.txt").write_text("edited after the crash\n")
    with pytest.raises(util.YggError):
        gitops.finish_publications(repo)
    assert gitops._intent_path(repo, "main").exists()


def subprocess_free_git():
    return lambda path, *args: util.trusted_git("-C", path, *args)


def test_recovery_refuses_a_checkout_with_a_repository_filter_and_keeps_the_intent(repo, monkeypatch):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    git(repo, "checkout", "-q", "-b", "work")
    work = commit(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "main")
    passed = git(repo, "rev-parse", "main")
    real = crash_after_the_swap(monkeypatch)
    with pytest.raises(SystemExit):
        gitops._merge_into(repo, "main", work, "merge", no_ff=True, expect=passed)
    monkeypatch.setattr(gitops, "_finish_publication", real)
    git(repo, "config", "filter.x.smudge", "ygg send --from pm1 --to pm1tl1 --kind work --body x")
    for _ in range(2):
        with pytest.raises(util.YggError) as error:
            gitops.finish_publications(repo)
        assert "can't be finished" in error.value.message
    assert gitops._intent_path(repo, "main").exists()
    held = [a for a in state.alerts() if a["kind"] == "ygg-merge-intent-held"]
    assert len(held) == 1 and held[0]["class"] == "log"


def test_a_start_that_loses_the_record_to_a_newer_check_stops_its_job(monkeypatch):
    stopped = []

    class Process:
        pid = 4242

    def popen(*args, **kwargs):
        mergecheck.save_record("p1", {"id": "p1-newer", "state": "running"})  # a newer check claims meanwhile
        return Process()
    monkeypatch.setattr(mergecheck.subprocess, "Popen", popen)
    monkeypatch.setattr(mergecheck.util, "process_start", lambda pid: "s:1.000001")  # v1.1: the job's identity
    monkeypatch.setattr(mergecheck, "_stop_job", lambda data: stopped.append(data["id"]))
    started = mergecheck._start("p1", "/repo", ("m" * 40, "i" * 40))
    assert mergecheck.record("p1")["id"] == "p1-newer"
    assert stopped == [started["id"]]


def test_a_stale_cancel_never_overwrites_a_newer_check_whose_failure_still_counts(effects):
    stale = {"id": "p1-a", "project": "p1", "main": "m" * 40, "integration": "i" * 40, "state": "running"}
    newer = dict(stale, id="p1-b")
    mergecheck.save_record("p1", newer)
    assert mergecheck.save_if_owner("p1", "p1-a", dict(stale, state="cancelled")) is False
    assert mergecheck.record("p1")["id"] == "p1-b"
    mergecheck._finish({"id": "p1", "pm": "pm1"}, dict(newer),
                       {"state": "failed", "reason": "the project's checks failed", "tail": "2 tests failed"})
    assert len(effects) == 1 and "(check p1-b)" in effects[0]


@pytest.mark.parametrize("replacement", ["running", "passed", "failed"])
def test_a_superseded_manual_check_reports_plainly(monkeypatch, replacement):
    monkeypatch.setattr("yggdrasil.commands._project_for", lambda name: {"id": name, "pm": "pm1"})
    monkeypatch.setattr(gitops, "_caller_is_pm", lambda project: None)
    monkeypatch.setattr(gitops, "finish_publications", lambda repo=None: None)
    monkeypatch.setattr("yggdrasil.rehydrate.replay_journal", lambda: 0)
    monkeypatch.setattr(mergecheck, "resolve_repo", lambda project, repo: "/repo")
    monkeypatch.setattr(mergecheck, "pair", lambda path, project: ("a" * 40, "b" * 40))

    def run(*args):
        mergecheck.save_record("p1", {"id": "p1-newer", "state": replacement})
        return {"state": "passed", "reason": "", "tail": "", "finished_at": util.iso()}
    monkeypatch.setattr(mergecheck, "run_check", run)
    assert mergecheck.check_verb("p1", None) == "merge check: superseded by a newer check\n"


# ---------------------------------------------------------------- second security note

def a_valid_intent(repo, **fields):
    sha = git(repo, "rev-parse", "main")
    intent = {"repo": str(repo), "branch": "main", "expect": sha, "merged": sha, "location": str(repo)}
    intent.update(fields)
    return intent


def test_main_at_neither_commit_makes_an_intent_moot(repo, monkeypatch):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    path = gitops._intent_path(repo, "main")
    path.write_text(json.dumps(a_valid_intent(repo, expect="1" * 40, merged="2" * 40)))  # ids that don't exist
    calls = []
    real = gitops.git
    monkeypatch.setattr(gitops, "git", lambda *args: calls.append(args[1:]) or real(*args))
    gitops.finish_publications(repo)  # set aside, never a refusal on every run
    assert (path.parent / "rejected" / path.name).exists()
    assert not any(args and args[0] in ("read-tree", "update-ref", "write-tree") for args in calls)


@pytest.mark.parametrize("content", ["[1, 2]", "not json at all", '{"repo": 7, "branch": "main"}'])
def test_an_odd_intent_file_is_set_aside_and_never_raises(repo, monkeypatch, content):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    path = gitops._intent_path(repo, "main")
    path.write_text(content)
    gitops.finish_publications(repo)
    assert (path.parent / "rejected" / path.name).exists()
    assert len([a for a in state.alerts() if a["kind"] == "ygg-merge-intent-rejected"]) == 1


def test_one_stuck_intent_blocks_only_its_own_repo(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    git(other, "config", "user.name", "t")
    git(other, "config", "user.email", "t@example.invalid")
    commit(other, "x.txt", "x\n", "base")
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve()), str(other.resolve())})
    git(repo, "checkout", "-q", "-b", "work")
    work = commit(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "main")
    passed = git(repo, "rev-parse", "main")
    real = crash_after_the_swap(monkeypatch)
    with pytest.raises(SystemExit):
        gitops._merge_into(repo, "main", work, "merge", no_ff=True, expect=passed)
    monkeypatch.setattr(gitops, "_finish_publication", real)
    (repo / "a.txt").write_text("edited after the crash\n")  # this repo's intent is stuck
    gitops.finish_publications(other)  # another repo's merge is never blocked
    gitops.finish_publications()  # nor the tick
    with pytest.raises(util.YggError):
        gitops.finish_publications(repo)  # only its own repo waits
    assert gitops._intent_path(repo, "main").exists()


def test_a_genuine_intent_follows_a_checkout_that_moved(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    git(repo, "checkout", "-q", "-b", "work")
    work = commit(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "main")
    passed = git(repo, "rev-parse", "main")
    real = crash_after_the_swap(monkeypatch)
    with pytest.raises(SystemExit):
        gitops._merge_into(repo, "main", work, "merge", no_ff=True, expect=passed)
    monkeypatch.setattr(gitops, "_finish_publication", real)
    git(repo, "reset", "-q", "--hard")  # drop the staged reverse in the old checkout
    git(repo, "checkout", "-q", "--detach")
    moved = tmp_path / "moved"
    git(repo, "worktree", "add", "-q", str(moved), "main")  # main now lives elsewhere, at the merge
    (moved / "b.txt").write_text("not yet matching\n")
    with pytest.raises(util.YggError):
        gitops.finish_publications(repo)  # followed, not rejected: the project stays open until it matches
    assert gitops._intent_path(repo, "main").exists()
    assert not (gitops._intents_dir() / "rejected").exists()
    git(moved, "checkout", "--", "b.txt")
    gitops.finish_publications(repo)
    assert not gitops._intent_path(repo, "main").exists()


# ---------------------------------------------------------------- review round 5

def test_a_start_from_a_record_that_changed_since_it_was_read_is_skipped(monkeypatch):
    mergecheck.save_record("p1", {"id": "p1-a", "state": "failed", "main": "m" * 40, "integration": "i" * 40})
    seen = mergecheck.record("p1")
    mergecheck.save_record("p1", dict(seen, note="moved on"))  # same owner, newer revision
    monkeypatch.setattr(mergecheck.subprocess, "Popen", lambda *a, **k: pytest.fail("no job may start"))
    assert mergecheck._start("p1", "/repo", ("m" * 40, "j" * 40), seen) is None
    assert mergecheck.record("p1")["id"] == "p1-a"


def test_a_stale_cancel_of_the_same_check_never_erases_pending_effects(effects, monkeypatch):
    running = {"id": "p1-a", "project": "p1", "main": "m" * 40, "integration": "i" * 40, "state": "running"}
    mergecheck.save_record("p1", running)
    seen = mergecheck.record("p1")
    monkeypatch.setattr(mergecheck, "_failure_effects", lambda project, data: None)  # a crash before the effects
    mergecheck._finish({"id": "p1", "pm": "pm1"}, dict(seen), {"state": "failed", "reason": "x", "tail": ""})
    assert mergecheck.record("p1")["effects_pending"] is True
    cancelled = dict(seen, state="cancelled")
    assert mergecheck.save_if_owner("p1", "p1-a", cancelled, revision=seen["revision"]) is False  # stale
    current = mergecheck.record("p1")
    assert mergecheck.save_if_owner("p1", "p1-a", dict(current, state="cancelled", effects_pending=False),
                                    revision=current["revision"]) is False  # only the completion clears them
    assert mergecheck.record("p1")["effects_pending"] is True


@pytest.mark.parametrize("field,value", [("location", ["x"]), ("location", 7), ("expect", 5), ("repo", ["/r"]),
                                         ("branch", None), ("merged", {"a": 1})])
def test_malformed_intent_field_types_are_set_aside(repo, monkeypatch, field, value):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    sha = git(repo, "rev-parse", "main")
    intent = {"repo": str(repo), "branch": "main", "expect": sha, "merged": sha, "location": str(repo)}
    intent[field] = value
    path = gitops._intent_path(repo, "main")
    path.write_text(json.dumps(intent))
    gitops.finish_publications(repo)
    assert (path.parent / "rejected" / path.name).exists()


# ---------------------------------------------------------------- third security note

def crashed_merge(repo, monkeypatch):
    """A merge into checked-out main killed right after the swap; returns (passed, merged)."""
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    git(repo, "checkout", "-q", "-b", "work")
    work = commit(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "main")
    passed = git(repo, "rev-parse", "main")
    real = crash_after_the_swap(monkeypatch)
    with pytest.raises(SystemExit):
        gitops._merge_into(repo, "main", work, "merge", no_ff=True, expect=passed)
    monkeypatch.setattr(gitops, "_finish_publication", real)
    return passed, git(repo, "rev-parse", "main")


def held(path):
    return [a for a in state.alerts() if a["kind"] == "ygg-merge-intent-held" and a.get("path") == str(path)]


def test_a_failed_rev_parse_keeps_the_intent(repo, monkeypatch):
    crashed_merge(repo, monkeypatch)
    real = gitops.git

    def broken(path, *args):
        if args[:1] == ("rev-parse",):
            return subprocess.CompletedProcess(args, 128, "", "fatal: environment trouble")
        return real(path, *args)
    monkeypatch.setattr(gitops, "git", broken)
    path = gitops._intent_path(repo, "main")
    for _ in range(2):
        with pytest.raises(util.YggError):
            gitops.finish_publications(repo)
    gitops.finish_publications("/another/repo")  # other repos go on
    assert path.exists() and not (path.parent / "rejected").exists()
    assert len(held(path)) == 1


def test_main_moved_on_past_the_merge_with_a_matching_checkout_clears_the_intent(repo, monkeypatch):
    crashed_merge(repo, monkeypatch)
    git(repo, "reset", "-q", "--hard")  # the checkout follows main
    commit(repo, "c.txt", "c\n", "more work after the merge")
    gitops.finish_publications(repo)
    assert not gitops._intent_path(repo, "main").exists()
    assert not (gitops._intents_dir() / "rejected").exists()


def test_main_moved_on_past_the_merge_with_a_stale_checkout_refuses_and_keeps_it(repo, monkeypatch):
    _, merged = crashed_merge(repo, monkeypatch)
    tree = git(repo, "rev-parse", merged + "^{tree}")
    later = git(repo, "commit-tree", tree, "-p", merged, "-m", "moved on")
    git(repo, "update-ref", "refs/heads/main", later)  # main moves on; the checkout still has the old tree
    with pytest.raises(util.YggError) as error:
        gitops.finish_publications(repo)
    assert "moved on" in error.value.message
    assert gitops._intent_path(repo, "main").exists()


def test_main_at_neither_with_the_merge_not_landed_is_rejected(repo, monkeypatch):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    base = git(repo, "rev-parse", "main")
    git(repo, "checkout", "-q", "-b", "elsewhere")
    stray = commit(repo, "s.txt", "s\n", "never merged")
    git(repo, "checkout", "-q", "main")
    commit(repo, "m.txt", "m\n", "main moved")
    path = gitops._intent_path(repo, "main")
    path.write_text(json.dumps({"repo": str(repo), "branch": "main", "expect": base, "merged": stray,
                                "location": str(repo)}))
    gitops.finish_publications(repo)
    assert (path.parent / "rejected" / path.name).exists()


def test_a_tracker_error_during_validation_keeps_the_intent(repo, monkeypatch):
    crashed_merge(repo, monkeypatch)

    def tracker_down():
        raise RuntimeError("tracker unreadable")
    monkeypatch.setattr(gitops, "_approved_repo_paths", tracker_down)
    path = gitops._intent_path(repo, "main")
    with pytest.raises(util.YggError):
        gitops.finish_publications(repo)
    gitops.finish_publications()  # the tick never refuses
    assert path.exists() and not (path.parent / "rejected").exists()
    assert len(held(path)) == 1


def test_the_manual_check_answer_comes_from_the_snapshot_that_decided_ownership(monkeypatch):
    monkeypatch.setattr("yggdrasil.commands._project_for", lambda name: {"id": name, "pm": "pm1"})
    monkeypatch.setattr(gitops, "_caller_is_pm", lambda project: None)
    monkeypatch.setattr(gitops, "finish_publications", lambda repo=None: None)
    monkeypatch.setattr("yggdrasil.rehydrate.replay_journal", lambda: 0)
    monkeypatch.setattr(mergecheck, "resolve_repo", lambda project, repo: "/repo")
    monkeypatch.setattr(mergecheck, "pair", lambda path, project: ("a" * 40, "b" * 40))
    monkeypatch.setattr(mergecheck, "run_check", lambda *args: {"state": "passed", "reason": "no project checks",
                                                                "tail": "", "finished_at": util.iso()})
    real = mergecheck.record
    reads = {"n": 0}

    def record_then_replace(project_id):
        found = real(project_id)
        reads["n"] += 1
        if found.get("state") == "passed" and reads.get("replaced") is None:
            reads["replaced"] = True  # a newer check takes the record right after this read
            mergecheck.save_record(project_id, {"id": "p1-newer", "state": "running"})
        return found
    monkeypatch.setattr(mergecheck, "record", record_then_replace)
    assert mergecheck.check_verb("p1", None) == "merge check: passed (no project checks)\n"


# ---------------------------------------------------------------- review round 7: only a positive answer rejects or clears

def test_a_nul_path_intent_is_rejected_and_never_blocks_another_repo(repo, monkeypatch):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    sha = git(repo, "rev-parse", "main")
    path = gitops._intents_dir() / "forged-nul.json"
    path.write_text(json.dumps({"repo": "/a\u0000b", "branch": "main", "expect": sha, "merged": sha,
                                "location": None}))
    gitops.finish_publications(repo)  # an unrelated repo's command goes on
    gitops.finish_publications()
    assert (path.parent / "rejected" / path.name).exists()


def broken_git(monkeypatch, match, returncode, stderr):
    real = gitops.git

    def git_with_failure(where, *args):
        if args[:len(match)] == match:
            return subprocess.CompletedProcess(args, returncode, "", stderr)
        return real(where, *args)
    monkeypatch.setattr(gitops, "git", git_with_failure)


def test_a_cat_file_failure_is_not_a_missing_merge(repo, monkeypatch):
    crashed_merge(repo, monkeypatch)
    git(repo, "reset", "-q", "--hard")
    commit(repo, "c.txt", "c\n", "main moved on")  # main at neither commit: the merge's existence is looked up
    broken_git(monkeypatch, ("cat-file", "-e"), 128, "error: unable to open object: Permission denied")
    path = gitops._intent_path(repo, "main")
    with pytest.raises(util.YggError):
        gitops.finish_publications(repo)
    assert path.exists() and not (path.parent / "rejected").exists()
    assert len(held(path)) == 1


def test_a_failed_worktree_listing_keeps_the_intent(repo, monkeypatch):
    crashed_merge(repo, monkeypatch)
    broken_git(monkeypatch, ("worktree", "list"), 128, "fatal: unable to read worktrees")
    path = gitops._intent_path(repo, "main")
    with pytest.raises(util.YggError):
        gitops.finish_publications(repo)
    assert path.exists() and not (path.parent / "rejected").exists()
    assert len(held(path)) == 1


def test_a_failed_index_read_or_file_compare_keeps_the_intent(repo, monkeypatch):
    crashed_merge(repo, monkeypatch)
    broken_git(monkeypatch, ("write-tree",), 128, "fatal: index file corrupt")
    path = gitops._intent_path(repo, "main")
    with pytest.raises(util.YggError):
        gitops.finish_publications(repo)
    assert path.exists() and len(held(path)) == 1


# ---------------------------------------------------------------- review round 8

def test_an_unreadable_intent_file_is_kept_and_refuses_only_its_own_repo(repo, monkeypatch):
    crashed_merge(repo, monkeypatch)
    path = gitops._intent_path(repo, "main")
    path.chmod(0)
    try:
        with pytest.raises(util.YggError):
            gitops.finish_publications(repo)
        gitops.finish_publications("/another/repo")  # matched by its file-name key, so others go on
        gitops.finish_publications()
        assert path.exists() and not (path.parent / "rejected").exists()
        assert len(held(path)) == 1
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("listing", ["worktree /x\nHEAD " + "a" * 40 + "\n",
                                     "worktree /x\nHEAD " + "a" * 40 + "\ndetached\nbranch refs/heads/main\n",
                                     "HEAD " + "a" * 40 + "\nbranch refs/heads/main\n"])
def test_an_incomplete_worktree_record_keeps_the_intent(repo, monkeypatch, listing):
    crashed_merge(repo, monkeypatch)
    real = gitops.git
    monkeypatch.setattr(gitops, "git", lambda where, *args: subprocess.CompletedProcess(args, 0, listing, "")
                        if args[:2] == ("worktree", "list") else real(where, *args))
    path = gitops._intent_path(repo, "main")
    with pytest.raises(util.YggError):
        gitops.finish_publications(repo)
    assert path.exists() and len(held(path)) == 1


def test_a_failed_read_tree_keeps_the_intent_with_the_held_alert(repo, monkeypatch):
    crashed_merge(repo, monkeypatch)
    broken_git(monkeypatch, ("read-tree",), 128, "fatal: unable to write new index file")
    path = gitops._intent_path(repo, "main")
    with pytest.raises(util.YggError):
        gitops.finish_publications(repo)
    assert path.exists() and len(held(path)) == 1


def test_no_readable_approved_repo_keeps_the_intent(repo, monkeypatch):
    crashed_merge(repo, monkeypatch)
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: set())
    path = gitops._intent_path(repo, "main")
    with pytest.raises(util.YggError):
        gitops.finish_publications(repo)
    assert path.exists() and not (path.parent / "rejected").exists()


# ---------------------------------------------------------------- review round 9

def test_checkout_of_parses_every_record_strictly(repo, monkeypatch):
    sha = "a" * 40
    real = gitops.git

    def listing(text):
        monkeypatch.setattr(gitops, "git", lambda where, *args: subprocess.CompletedProcess(args, 0, text, "")
                            if args[:2] == ("worktree", "list") else real(where, *args))
    good = ("worktree /home\nHEAD " + sha + "\nbranch refs/heads/main\n\n"
            "worktree /other\nHEAD " + sha + "\ndetached\nlocked moved disk\nprunable gitdir gone\n")
    listing(good)
    assert gitops.checkout_of(repo, "main") == "/home"
    for bad in ["worktree /a\nworktree /b\nHEAD " + sha + "\nbranch refs/heads/main\n",
                "worktree /a\nHEAD " + sha + "\nHEAD " + sha + "\nbranch refs/heads/main\n",
                "worktree /a\nHEAD " + sha + "\nbranch refs/heads/main\nsomething else\n",
                "worktree /a\nbranch refs/heads/main\n\nworktree /b\nbranch refs/heads/main\n"]:
        listing(bad)
        with pytest.raises(util.YggError):
            gitops.checkout_of(repo, "main")


def test_two_worktree_lines_in_one_record_keep_the_intent(repo, monkeypatch):
    crashed_merge(repo, monkeypatch)
    real = gitops.git
    text = "worktree /a\nworktree /b\nHEAD " + "a" * 40 + "\nbranch refs/heads/other\n"
    monkeypatch.setattr(gitops, "git", lambda where, *args: subprocess.CompletedProcess(args, 0, text, "")
                        if args[:2] == ("worktree", "list") else real(where, *args))
    path = gitops._intent_path(repo, "main")
    with pytest.raises(util.YggError):
        gitops.finish_publications(repo)
    assert path.exists() and len(held(path)) == 1


def test_an_unreadable_approval_keeps_a_genuine_intent(repo, tmp_path, monkeypatch):
    real_reader = gitops._approved_repo_paths
    crashed_merge(repo, monkeypatch)
    monkeypatch.setattr(gitops, "_approved_repo_paths", real_reader)  # back to the real approved-repo reader
    projects = {"p1": tmp_path / "p1", "p2": tmp_path / "p2"}
    for name, folder in projects.items():
        folder.mkdir()
        (folder / "pid-approved.json").write_text(json.dumps({
            "actor": "user", "guarded": {"repos": [{"path": str(repo) if name == "p1" else "/other/repo"}]}}))
    monkeypatch.setattr("yggdrasil.tracker.projects", lambda: [{"id": name} for name in projects])
    monkeypatch.setattr("yggdrasil.tracker.project_dir", lambda name: projects[name])
    (projects["p1"] / "pid-approved.json").chmod(0)
    try:
        path = gitops._intent_path(repo, "main")
        with pytest.raises(util.YggError):
            gitops.finish_publications(repo)
        assert path.exists() and not (path.parent / "rejected").exists()
    finally:
        (projects["p1"] / "pid-approved.json").chmod(0o600)


def test_an_intent_file_that_isnt_text_is_rejected(repo, monkeypatch):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    path = gitops._intent_path(repo, "main")
    path.write_bytes(b"\xff\xfe not text")
    gitops.finish_publications(repo)
    assert (path.parent / "rejected" / path.name).exists()


def test_an_os_error_in_merge_mains_own_finish_is_held_and_refused(repo, monkeypatch):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    git(repo, "checkout", "-q", "-b", "work")
    work = commit(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "main")
    passed = git(repo, "rev-parse", "main")
    real = gitops.git

    def denied(where, *args):
        if args[:1] == ("read-tree",):
            raise PermissionError("index locked by the OS")
        return real(where, *args)
    monkeypatch.setattr(gitops, "git", denied)
    with pytest.raises(util.YggError) as error:
        gitops._merge_into(repo, "main", work, "merge", no_ff=True, expect=passed)
    assert error.value.code == util.EXIT_REFUSED
    path = gitops._intent_path(repo, "main")
    assert path.exists() and len(held(path)) == 1


# ---------------------------------------------------------------- review round 10

def test_an_approval_behind_an_unsearchable_folder_keeps_the_intent(repo, tmp_path, monkeypatch):
    real_reader = gitops._approved_repo_paths
    crashed_merge(repo, monkeypatch)
    monkeypatch.setattr(gitops, "_approved_repo_paths", real_reader)
    projects = {"p1": tmp_path / "p1", "p2": tmp_path / "p2"}
    for name, folder in projects.items():
        folder.mkdir()
        (folder / "pid-approved.json").write_text(json.dumps({
            "actor": "user", "guarded": {"repos": [{"path": str(repo) if name == "p1" else "/other/repo"}]}}))
    monkeypatch.setattr("yggdrasil.tracker.projects", lambda: [{"id": name} for name in projects])
    monkeypatch.setattr("yggdrasil.tracker.project_dir", lambda name: projects[name])
    projects["p1"].chmod(0)  # exists() would say False here; the read says PermissionError
    try:
        path = gitops._intent_path(repo, "main")
        with pytest.raises(util.YggError):
            gitops.finish_publications(repo)
        assert path.exists() and not (path.parent / "rejected").exists()
    finally:
        projects["p1"].chmod(0o700)


def test_an_unreachable_state_folder_right_after_the_swap_refuses_and_keeps_the_intent(repo, monkeypatch):
    monkeypatch.setattr(gitops, "_approved_repo_paths", lambda: {str(repo.resolve())})
    git(repo, "checkout", "-q", "-b", "work")
    work = commit(repo, "b.txt", "b\n", "work")
    git(repo, "checkout", "-q", "main")
    passed = git(repo, "rev-parse", "main")
    folder = gitops._intents_dir()
    real = gitops.git

    def lock_out_after_swap(where, *args):
        result = real(where, *args)
        if args[:1] == ("update-ref",) and result.returncode == 0:
            folder.chmod(0)
        return result
    monkeypatch.setattr(gitops, "git", lock_out_after_swap)
    try:
        with pytest.raises(util.YggError) as error:
            gitops._merge_into(repo, "main", work, "merge", no_ff=True, expect=passed)
        assert error.value.code == util.EXIT_REFUSED
    finally:
        folder.chmod(0o700)
    assert gitops._intent_path(repo, "main").exists()



# ---------------------------------------------------------------- sent only once the send is under way

def test_sent_is_recorded_only_once_the_channel_process_exists(auto, monkeypatch):
    import sys
    auto.update(alert_macos="off", alert_command=[sys.executable, "-c", "pass"])
    state.alert("ygg-launch-refused", "Launch of pm1tl1 refused: x", node="pm1tl1")
    real = alertdelivery.subprocess.Popen
    seen = []

    def spawn(*args, **kwargs):
        rows = [json.loads(line) for line in alertdelivery.delivered_path().read_text().splitlines()]
        seen.append([row["step"] for row in rows])
        return real(*args, **kwargs)
    monkeypatch.setattr(alertdelivery.subprocess, "Popen", spawn)
    alertdelivery.tick_step()
    assert seen == [["pending"]]  # no `sent` before the command's process exists
    steps = [json.loads(line)["step"] for line in alertdelivery.delivered_path().read_text().splitlines()]
    assert steps[:3] == ["pending", "sent", "delivered"]


def test_a_slow_sent_record_shortens_the_commands_wait(monkeypatch):
    import sys
    clock = {"extra": 0.0}
    real_monotonic = alertdelivery.time.monotonic
    monkeypatch.setattr(alertdelivery.time, "monotonic", lambda: real_monotonic() + clock["extra"])

    def slow_record():
        clock["extra"] += 1.5  # the durable append takes 1.5 s of the 2 s wait
    started = real_monotonic()
    outcome = alertdelivery._send([sys.executable, "-c", "import time; time.sleep(30)"], None, 2.0, "al-1",
                                  started=slow_record)
    assert outcome == "timed-out"
    assert real_monotonic() - started < 1.9  # the wait was cut to what was left, not a fresh 2 s


def test_a_sent_record_that_uses_the_whole_wait_times_out_at_once(monkeypatch):
    import sys
    clock = {"extra": 0.0}
    real_monotonic = alertdelivery.time.monotonic
    monkeypatch.setattr(alertdelivery.time, "monotonic", lambda: real_monotonic() + clock["extra"])

    def slower_record():
        clock["extra"] += 5.0
    started = real_monotonic()
    outcome = alertdelivery._send([sys.executable, "-c", "import time; time.sleep(30)"], None, 2.0, "al-2",
                                  started=slower_record)
    assert outcome == "timed-out" and real_monotonic() - started < 1.5
