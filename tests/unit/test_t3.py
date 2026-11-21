"""T3 evidence, messages and session settings (rulings 105, 106 and 108; Batch B B30, B31)."""

import json
import os
import subprocess

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


def thread(status="completed", active=None, archived=False, pending=0, title="tl1 act-0001",
           messages=(("user", "[ygg act-0001 launch]\nnotice"),)):
    return {"thread": {"threadId": "th-1", "title": title, "status": status, "activeRunId": active,
                       "latestRunId": "run-1", "archived": archived, "pendingRequestCount": pending,
                       "projectId": "proj-1"},
            "messages": [{"role": role, "text": text, "runId": "run-1"} for role, text in messages]}


IDENTITY = {"title": "tl1 act-0001", "project_id": "proj-1"}


def test_ruling_106_maps_thread_status_to_evidence(world):
    from yggdrasil import t3
    assert t3.classify(thread("running", "run-1"), "act-0001", IDENTITY)["state"] == "alive"
    assert t3.classify(thread("starting", "run-1"), "act-0001", IDENTITY)["state"] == "alive"
    done = t3.classify(thread("completed"), "act-0001", IDENTITY)
    assert done == {"state": "ended", "exit_code": 0, "text": ""}
    failed = t3.classify(thread("failed", messages=(("user", "[ygg act-0001 launch]"), ("assistant", "boom"))),
                         "act-0001", IDENTITY)
    assert failed == {"state": "ended", "exit_code": 1, "text": "boom"}
    for case in (thread("completed", archived=True), thread("completed", pending=1), thread("interrupted"),
                 thread("completed", title="someone else's thread")):
        assert t3.classify(case, "act-0001", IDENTITY)["state"] == "unknown"


def test_a_newer_marker_or_a_missing_marker_settles_nothing(world):
    from yggdrasil import t3
    newer = thread("completed", messages=(("user", "[ygg act-0001 launch]"), ("user", "[ygg act-0002 m-0010]")))
    assert t3.classify(newer, "act-0001", IDENTITY)["state"] == "unknown"
    assert t3.classify(thread("completed"), "act-0009", IDENTITY)["state"] == "no-marker"


def test_an_unseen_status_is_alerted_once(world):
    from yggdrasil import state, t3
    t3.classify(thread("interrupted"), "act-0001", IDENTITY)
    t3.classify(thread("interrupted"), "act-0001", IDENTITY)
    assert len([a for a in state.alerts() if a["kind"] == "ygg-t3-status"]) == 1


def test_a_failed_run_on_a_provider_429_is_a_capacity_exit(world):
    from yggdrasil import launcher, t3
    seen = t3.classify(thread("failed", messages=(("user", "[ygg act-0001 launch]"),
                                                  ("assistant", "API Error: 429 Too Many Requests"))),
                       "act-0001", IDENTITY)
    record = {"exit_code": seen["exit_code"], "exited_at": util.iso(), "stderr_tail": seen["text"]}
    assert launcher.exit_fields(record)["capacity"] is True
    record["stderr_tail"] = "API Error: 400 unknown provider for model x"
    assert launcher.exit_fields(record) == {"capacity": False}


def test_a_t3_message_carries_no_prompt_and_the_prompt_file_is_private(world):
    from yggdrasil import t3
    path = t3.prompt_file("act-0001", "You are Yggdrasil node tl1. Brief marker zebra-5813")
    text = t3.message("act-0001", "m-0010", "tl1", path)
    assert text.splitlines()[0] == "[ygg act-0001 m-0010]"
    assert "zebra-5813" not in text and "You are Yggdrasil node" not in text and str(path) in text
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_the_session_settings_carry_only_named_variables(world, monkeypatch):
    from yggdrasil import t3
    monkeypatch.setenv("YGG_LINEAR_FAKE", "/tmp/fake")
    monkeypatch.setenv("YGG_SECRET_THING", "x")
    node = {"id": "tl1", "programme": "alpha", "project": "p1"}
    environment = t3.session_environment(node, "act-0001", {"YGG_STATE_ROOT": "/s", "YGG_CONFIG": "/c"})
    assert set(environment) <= {"YGG_NODE", "YGG_ACTIVATION", "YGG_PROGRAMME", "YGG_PROJECT", "YGG_STATE_ROOT",
                                "YGG_FILES_ROOT", "YGG_PROFILE", "YGG_CONFIG", "YGG_TRACKER"}
    assert environment["YGG_NODE"] == "tl1" and "YGG_TEST_MODE" not in environment


def test_a_tracked_settings_file_is_never_replaced(world, tmp_path):
    from yggdrasil import t3
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text('{"own": true}\n')
    subprocess.run(["git", "-C", str(repo), "add", ".claude/settings.json"], check=True)
    with pytest.raises(util.YggError):
        t3.prepare_worktree(repo, {"id": "tl1"}, "act-0001", {})
    assert json.loads((repo / ".claude" / "settings.json").read_text()) == {"own": True}


def test_the_t3_binary_is_never_searched_on_path(world, monkeypatch, tmp_path):
    from yggdrasil import t3
    fake = tmp_path / "t3"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("YGG_T3_BIN", raising=False)
    assert t3.binary() is None


def _worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def test_a_linked_claude_folder_settings_file_or_marker_is_refused(world, tmp_path):
    from yggdrasil import t3
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for link in (".claude", ".claude/settings.json", ".ygg-managed"):
        repo = _worktree(tmp_path / link.replace("/", "_"))
        if link != ".claude":
            (repo / ".claude").mkdir()
        os.symlink(elsewhere / "target", repo / link)
        with pytest.raises(util.YggError) as error:
            t3.prepare_worktree(repo, {"id": "tl1"}, "act-0001", {})
        assert "hook injection failed" in error.value.message
    assert not (elsewhere / "target").exists()


def test_the_exclude_entry_goes_to_the_programme_repository_only(world, tmp_path):
    from yggdrasil import t3
    repo = _worktree(tmp_path)
    t3.prepare_worktree(repo, {"id": "tl1"}, "act-0001", {}, home=str(repo))
    assert "/.claude/settings.json" in (repo / ".git" / "info" / "exclude").read_text()
    other = _worktree(tmp_path / "other")
    t3.prepare_worktree(other, {"id": "tl1"}, "act-0001", {}, home=str(tmp_path / "no-such-home"))
    assert "/.claude/settings.json" not in util.read_text(other / ".git" / "info" / "exclude")


def test_changed_or_overridden_guard_settings_stop_the_node_once(world, tmp_path):
    from yggdrasil import launcher, safety, state, t3
    repo = _worktree(tmp_path)
    digest = t3.prepare_worktree(repo, {"id": "tl1"}, "act-0001", {})
    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    assert settings["disableAllHooks"] is False
    assert t3.settings_problem(repo, digest) is None
    (repo / ".claude" / "settings.local.json").write_text('{"disableAllHooks": true}\n')
    assert "settings.local.json" in t3.settings_problem(repo, digest)
    (repo / ".claude" / "settings.local.json").unlink()
    (repo / ".claude" / "settings.json").write_text(json.dumps(dict(settings, hooks={})) + "\n")
    problem = t3.settings_problem(repo, digest)
    assert problem and "changed" in problem
    launcher.save_receipt("act-0001", {"node": "tl1", "activation": "act-0001"})
    t3.tampered("tl1", "act-0001", problem)
    t3.tampered("tl1", "act-0001", problem)
    assert any(m.get("scope") == "tl1" and m.get("kind") == "stop" for m in safety.markers())
    assert len([a for a in state.alerts() if a["kind"] == "ygg-t3-settings-tampered"]) == 1



def test_a_thread_outside_the_activation_project_or_missing_fields_is_unknown(world):
    from yggdrasil import t3
    moved = thread("completed")
    moved["thread"]["projectId"] = "proj-other"
    assert t3.classify(moved, "act-0001", IDENTITY)["state"] == "unknown"
    for field in ("activeRunId", "archived", "pendingRequestCount", "status"):
        partial = thread("completed")
        partial["thread"].pop(field)
        assert t3.classify(partial, "act-0001", IDENTITY)["state"] == "unknown", field
    no_run = thread("completed")
    no_run["messages"][0]["runId"] = ""
    assert t3.classify(no_run, "act-0001", IDENTITY)["state"] == "unknown"


def _spawned_t3(node, activation, **fields):
    from yggdrasil import lifecycle, launcher
    lifecycle.commit(node, activation, "reserved", launcher_pid=1, launcher_started_at="x", work={})
    lifecycle.commit(node, activation, "spawned", launcher="t3", t3_kind="start", t3_title=node + " " + activation,
                     t3_worktree="/tmp/wt", t3_project_id="proj-1", supervisor_pid=1, supervisor_started_at="x")
    launcher.save_receipt(activation, dict({"node": node, "activation": activation, "launcher": "t3",
                                            "t3_pending": "start", "t3_text": "[ygg " + activation + " launch]"},
                                           **fields))


def test_a_call_still_owned_by_its_dispatcher_is_never_retried(world, monkeypatch):
    from yggdrasil import lifecycle, t3
    _spawned_t3("tl1", "act-0001", t3_dispatcher=lifecycle.own_identity())  # this live process owns it
    calls = []
    monkeypatch.setattr(t3, "run", lambda *args, **kw: calls.append(args) or
                        subprocess.CompletedProcess(args, 0, "[]", ""))
    monkeypatch.setattr(t3, "dispatch", lambda *args, **kw: calls.append(("dispatch",)))
    t3.reconcile("tl1", "act-0001")
    assert calls == []  # neither the list nor a retry while the dispatcher lives


def test_the_one_retry_is_claimed_exclusively(world, monkeypatch):
    from yggdrasil import launcher, t3
    _spawned_t3("tl1", "act-0001", t3_dispatcher={"pid": 999999, "started_at": "gone"})
    sent = []
    monkeypatch.setattr(t3, "dispatch", lambda *args, **kw: sent.append(args))
    ident = t3.identity("tl1", "act-0001")
    t3._retry("tl1", "act-0001", "start", ident)
    t3._retry("tl1", "act-0001", "start", ident)
    assert len(sent) == 1 and launcher.receipt("act-0001")["t3_retried"] is True


def test_a_stop_published_before_admission_prevents_the_call(world, monkeypatch, tmp_path):
    from yggdrasil import launcher, safety, t3
    _spawned_t3("tl1", "act-0001", t3_settings_sha="unused")
    monkeypatch.setattr(t3, "settings_problem", lambda worktree, expected: None)
    monkeypatch.setattr(t3, "host_usable", lambda: True)
    started = []
    monkeypatch.setattr(t3, "_spawn", lambda args: started.append(args))
    safety.publish_marker("stop", "tl1")
    with pytest.raises(util.YggError):
        t3.dispatch("tl1", "act-0001", "start", title="tl1 act-0001", worktree=str(tmp_path), thread=None,
                    model="m", text="[ygg act-0001 launch]")
    assert started == []
    assert launcher.receipt("act-0001").get("t3_dispatcher") is None


def test_the_exit_fact_keeps_the_thread_so_a_wipe_does_not_lose_it(world, monkeypatch):
    from yggdrasil import launcher, lifecycle, t3
    _spawned_t3("tl1", "act-0001", t3_pending=None, t3_thread="th-4")
    t3._commit_exit("tl1", "act-0001", {"state": "ended", "exit_code": 0, "text": ""})
    assert lifecycle.phase_fields(lifecycle.facts("alpha", "act-0001"), "exited").get("t3_thread") == "th-4"
    (launcher.receipt_path("act-0001") if hasattr(launcher, "receipt_path") else
     __import__("pathlib").Path(launcher.state.root() / "activations" / "act-0001.json")).unlink()
    monkeypatch.setattr(t3, "_find_by_title", lambda *args: None)
    assert t3.node_thread("tl1")[0] == "th-4"



@pytest.mark.parametrize("name", ["Settings.Local.json", "SETTINGS.LOCAL.JSON", "settings.local.json",
                                  "notes.md", "commands"])
def test_any_other_entry_in_the_claude_folder_is_tamper_in_any_case(world, tmp_path, name):
    from yggdrasil import t3
    repo = _worktree(tmp_path)
    digest = t3.prepare_worktree(repo, {"id": "tl1"}, "act-0001", {})
    target = repo / ".claude" / name
    target.mkdir() if name == "commands" else target.write_text("{}\n")
    assert t3.settings_problem(repo, digest) is not None


def test_a_worktree_whose_claude_folder_holds_more_is_refused_at_preparation(world, tmp_path):
    from yggdrasil import t3
    repo = _worktree(tmp_path)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "Settings.Local.json").write_text('{"disableAllHooks": true}\n')
    with pytest.raises(util.YggError) as error:
        t3.prepare_worktree(repo, {"id": "tl1"}, "act-0001", {})
    assert "may hold only" in error.value.message
    assert not (repo / ".claude" / "settings.json").exists()


def test_an_open_activation_thread_is_found_again_by_title_after_a_wipe(world, monkeypatch):
    from yggdrasil import launcher, t3
    _spawned_t3("tl1", "act-0001", t3_pending=None)
    listing = json.dumps([{"threadId": "th-9", "title": "tl1 act-0001", "projectId": "proj-1"}])
    monkeypatch.setattr(t3, "run", lambda *args, **kw: subprocess.CompletedProcess(args, 0, listing, ""))
    assert t3.node_thread("tl1")[0] == "th-9"
    assert launcher.receipt("act-0001")["t3_thread"] == "th-9"


def test_an_uncertain_send_is_not_retried_on_untrusted_thread_details(world, monkeypatch):
    from yggdrasil import t3
    _spawned_t3("tl1", "act-0001", t3_pending="send", t3_thread="th-4",
                t3_dispatcher={"pid": 999999, "started_at": "gone"})
    retried = []
    monkeypatch.setattr(t3, "_retry", lambda *args: retried.append(args))
    for change in ({"projectId": "proj-other"}, {"activeRunId": None, "_drop": "activeRunId"}):
        payload = thread("completed", title="tl1 act-0001", messages=(("user", "[ygg act-0000 launch]"),))
        drop = change.pop("_drop", None)
        payload["thread"].update(change)
        if drop:
            payload["thread"].pop(drop)
        monkeypatch.setattr(t3, "_show", lambda thread_id, payload=payload: ("json", payload, ""))
        t3.reconcile("tl1", "act-0001")
    assert retried == []


def test_unreadable_settings_are_tamper(world, tmp_path):
    from yggdrasil import t3
    repo = _worktree(tmp_path)
    digest = t3.prepare_worktree(repo, {"id": "tl1"}, "act-0001", {})
    (repo / ".claude" / "settings.json").write_bytes(b"\xff\xfe not utf-8")
    assert "unreadable" in t3.settings_problem(repo, digest)


def test_the_exclude_entry_is_never_written_through_a_linked_info_folder(world, tmp_path):
    from yggdrasil import t3
    repo = _worktree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    info = repo / ".git" / "info"
    if info.exists():
        import shutil
        shutil.rmtree(info)
    os.symlink(outside, info)
    t3.prepare_worktree(repo, {"id": "tl1"}, "act-0001", {}, home=str(repo))
    assert not (outside / "exclude").exists()


def test_settings_changed_between_launches_stop_the_node(world, tmp_path):
    from yggdrasil import launcher, safety, state, t3
    repo = _worktree(tmp_path)
    t3.prepare_worktree(repo, {"id": "tl1"}, "act-0001", {})
    (repo / ".claude" / "settings.json").write_text('{"hooks": {}}\n')  # rewritten by the last session
    launcher.save_receipt("act-0002", {"node": "tl1", "activation": "act-0002"})
    with pytest.raises(util.YggError):
        t3.prepare_worktree(repo, {"id": "tl1"}, "act-0002", {})
    assert any(m.get("scope") == "tl1" for m in safety.markers())
    assert [a for a in state.alerts() if a["kind"] == "ygg-t3-settings-tampered"]



@pytest.mark.parametrize("remove", ["file", "folder"])
def test_a_deleted_settings_file_or_folder_is_tamper(world, tmp_path, remove):
    import shutil
    from yggdrasil import launcher, safety, t3
    repo = _worktree(tmp_path)
    digest = t3.prepare_worktree(repo, {"id": "tl1"}, "act-0001", {})
    if remove == "file":
        (repo / ".claude" / "settings.json").unlink()
    else:
        shutil.rmtree(repo / ".claude")
    assert t3.settings_problem(repo, digest) is not None  # checked before each start or send
    launcher.save_receipt("act-0002", {"node": "tl1", "activation": "act-0002"})
    with pytest.raises(util.YggError):
        t3.prepare_worktree(repo, {"id": "tl1"}, "act-0002", {})  # and at the next preparation
    assert any(m.get("scope") == "tl1" for m in safety.markers())


@pytest.mark.parametrize("change", ["interrupted", "no-messages", "messages-not-a-list"])
def test_an_uncertain_send_is_held_on_incomplete_or_unrecognised_details(world, monkeypatch, change):
    from yggdrasil import t3
    _spawned_t3("tl1", "act-0001", t3_pending="send", t3_thread="th-4",
                t3_dispatcher={"pid": 999999, "started_at": "gone"})
    retried = []
    monkeypatch.setattr(t3, "_retry", lambda *args: retried.append(args))
    payload = thread("interrupted" if change == "interrupted" else "completed", title="tl1 act-0001",
                     messages=(("user", "[ygg act-0000 launch]"),))
    if change == "no-messages":
        payload.pop("messages")
    elif change == "messages-not-a-list":
        payload["messages"] = {"role": "user"}
    monkeypatch.setattr(t3, "_show", lambda thread_id: ("json", payload, ""))
    t3.reconcile("tl1", "act-0001")
    assert retried == []


def test_an_unlistable_claude_folder_is_tamper(world, tmp_path):
    from yggdrasil import t3
    repo = _worktree(tmp_path)
    digest = t3.prepare_worktree(repo, {"id": "tl1"}, "act-0001", {})
    (repo / ".claude").chmod(0o300)
    try:
        assert t3.settings_problem(repo, digest) is not None
    finally:
        (repo / ".claude").chmod(0o700)


@pytest.mark.parametrize("broken", [{"role": "user"}, {"text": "[ygg act-0001 launch]"},
                                    {"role": "user", "text": 5}, {"role": "user", "text": "x", "runId": None}])
def test_a_malformed_message_makes_the_details_unknown(world, broken):
    from yggdrasil import t3
    payload = thread("completed")
    payload["messages"].append(dict(broken, **({} if "runId" in broken else {"runId": "run-1"})))
    assert t3.classify(payload, "act-0001", IDENTITY)["state"] == "unknown"


def test_a_gated_t3_call_never_released_never_reaches_t3(world, tmp_path):
    import sys
    from yggdrasil import t3
    marker = tmp_path / "t3-ran"
    fake = tmp_path / "t3"
    fake.write_text("#!/bin/sh\ntouch " + str(marker) + "\n")
    fake.chmod(0o755)
    gate = subprocess.run([sys.executable, "-I", "-c", t3.GATE, str(tmp_path / "never.go"), str(fake), "0.3",
                           "thread", "start"], capture_output=True, text=True, timeout=20)
    assert gate.returncode == t3.HELD and not marker.exists()
    token = tmp_path / "other.go"
    token.write_text("1\n")  # a token naming another process does not release it either
    gate = subprocess.run([sys.executable, "-I", "-c", t3.GATE, str(token), str(fake), "0.3"], timeout=20)
    assert gate.returncode == t3.HELD and not marker.exists()


def test_the_call_is_recorded_before_it_is_released_and_holds_recovery(world, monkeypatch, tmp_path):
    from yggdrasil import t3
    from yggdrasil import util as ygg_util
    fake = tmp_path / "t3"
    fake.write_text("#!/bin/sh\nsleep 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("YGG_T3_BIN", str(fake))
    order = []
    original = ygg_util.atomic_write

    def watching(path, text, *args, **kwargs):
        if str(path).endswith(".go"):
            order.append(("release", t3.call_record("act-0001").get("pid")))
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr(ygg_util, "atomic_write", watching)
    started = t3._spawn_gated("act-0001", ("thread", "start"))
    try:
        assert order == [("release", started.pid)]  # the identity was on record first
        assert t3.call_in_flight("act-0001")
        _spawned_t3("tl1", "act-0001", t3_dispatcher={"pid": 999999, "started_at": "gone"})
        sent = []
        monkeypatch.setattr(t3, "dispatch", lambda *args, **kw: sent.append(args))
        t3._retry("tl1", "act-0001", "start", t3.identity("tl1", "act-0001"))
        assert sent == []  # held while the recorded call may still reach T3
    finally:
        started.wait(timeout=10)
    assert not t3.call_in_flight("act-0001")
