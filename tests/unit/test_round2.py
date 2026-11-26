"""Unit tests for the round-2 review findings (dev #2 and QA)."""

import json
import subprocess
import sys

import pytest
import yaml

from yggdrasil import config, util


def clear_caches():
    config.effective.cache_clear()
    config.file_layers.cache_clear()
    config.profile.cache_clear()


@pytest.fixture
def world(tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    agent.write_text("#!" + sys.executable + "\nimport os, sys, time\n"
                     "open(os.environ['YGG_UNIT_AGENT_LOG'], 'a').write(os.environ.get('YGG_NODE', '') + ' ' + "
                     "' '.join(sys.argv[1:4]) + '\\n')\ntime.sleep(float(os.environ.get('YGG_UNIT_AGENT_SLEEP', '0')))\n")
    agent.chmod(0o755)
    cfg = tmp_path / "config.yaml"
    # Agents use the user's own Claude setup here unless a test sets agent_claude_config_dir in its config (the
    # A11 core default, Yggdrasil's own folder, needs the user's agreement first). The config says so for `ygg`
    # subprocesses; in this process a core file with that one line changed covers tests that write their own
    # config, which still override it.
    cfg.write_text("launch_confirm_seconds: 0.5\nsettle_delay_seconds: 0\nagent_claude_config_dir: ''\n")
    core = tmp_path / "core-yggdrasil.yaml"
    core.write_text("".join(line if not line.startswith("agent_claude_config_dir:") else "agent_claude_config_dir: ''\n"
                            for line in config.CORE.read_text().splitlines(keepends=True)))
    monkeypatch.setattr(config, "CORE", core)
    for key in list(__import__("os").environ):
        if key.startswith("YGG_"):
            monkeypatch.delenv(key, raising=False)
    user_home = tmp_path / "user-home"  # nothing a unit test does reaches the real home folder
    user_home.mkdir()
    # A fresh home has no git identity; macOS git guesses one from the host name, Linux git refuses to commit.
    (user_home / ".gitconfig").write_text("[user]\n\tname = t\n\temail = t@t\n")
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "tracker"))
    monkeypatch.setenv("YGG_TRACKER", "files")
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.setenv("YGG_NOW", "2026-11-16T02:00:00Z")
    monkeypatch.setenv("YGG_CONFIG", str(cfg))
    monkeypatch.setenv("YGG_CLAUDE_BIN", str(agent))
    monkeypatch.setenv("YGG_CODEX_BIN", str(agent))
    monkeypatch.setenv("YGG_UNIT_AGENT_LOG", str(tmp_path / "agent.log"))
    load = tmp_path / "load"
    load.write_text("0.1")
    monkeypatch.setenv("YGG_HOST_LOAD_FILE", str(load))
    monkeypatch.setenv("YGG_PLATFORM_STATE", str(tmp_path / "platform"))
    monkeypatch.chdir(tmp_path)
    clear_caches()
    from yggdrasil import commands
    home = tmp_path / "home"
    home.mkdir()
    commands.programme_create("alpha", "alpha", str(home), "vault-docs")
    commands.project_create("p1", "alpha", "pm1")
    commands.node_add("tl1", "tl", "pm1")
    commands.node_add("su1", "su", "pm1", "tl1")
    header = {"schema": "ygg.pid.v1", "project": "p1", "programme": "alpha", "repos": [],
              "stages": [{"number": 1, "planned_end": "2026-11-17T17:00:00Z"},
                         {"number": 2, "planned_end": "2026-11-18T17:00:00Z"}],
              "tolerances": {"time": "P1D"},
              "business_case": {"why": "w", "benefit": "b", "cost": {}, "risks": [], "stop_if": "s"}}
    commands.tracker_write("pm1", "p1", "pid", "---\n" + yaml.safe_dump(header) + "---\n# PID\n")
    commands.pid_approve("p1")
    yield tmp_path
    clear_caches()


def wait_exit(activation, timeout=10):
    import time
    from yggdrasil import launcher
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if launcher.receipt(activation).get("exited_at"):
            return
        time.sleep(0.05)


def package(wp="WP-1", stage=1):
    from yggdrasil import commands
    commands.tracker_write("pm1", "p1", "work-package", "Build", stage=str(stage), package=wp)
    commands.tracker_write("su1", "p1", "criteria", "Test", package=wp)
    commands.wp_authorise("p1", wp, "tl1")


# dev2 #0 (blocker): a live process found by reconciliation stays running and is never launchable.
def test_reconcile_keeps_live_uncertain_activation_running(world, monkeypatch):
    from yggdrasil import conductor, launcher, state, tracker
    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        activation = tracker.allocate("activation")
        launcher.save_receipt(activation, {"node": "tl1", "activation": activation, "pid": sleeper.pid,
                                           "process_started_at": util.process_start(sleeper.pid),
                                           "outcome": "uncertain", "started_at": util.iso()})
        state.save_runtime("tl1", {"state": "idle", "activation": activation, "outcome": "uncertain"})
        state.wake("tl1", "message")
        conductor.reconcile()
        runtime = state.runtime("tl1")
        assert runtime["state"] == "running" and runtime["outcome"] == "confirmed"
        assert launcher.receipt(activation)["outcome"] == "confirmed"
        assert "tl1" not in conductor.candidates()
    finally:
        sleeper.kill()


# dev2 #0: an activation with no evidence stays held (running, uncertain), never launchable.
def test_unresolved_uncertain_stays_held(world):
    from yggdrasil import conductor, launcher, state, tracker
    activation = tracker.allocate("activation")
    launcher.save_receipt(activation, {"node": "tl1", "activation": activation, "pid": None, "outcome": "uncertain"})
    state.save_runtime("tl1", {"state": "idle", "activation": activation, "outcome": "uncertain"})
    state.wake("tl1", "message")
    conductor.reconcile()
    assert state.runtime("tl1")["state"] == "running"
    assert "tl1" not in conductor.candidates()
    with pytest.raises(util.YggError):
        launcher.launch("tl1")


# dev2 #1: a startup reservation is never a liveness retry; past the grace it becomes uncertain and alerts.
def test_pending_reservation_is_not_relaunched(world, monkeypatch):
    from yggdrasil import conductor, launcher, state, tracker
    activation = tracker.allocate("activation")
    launcher.save_receipt(activation, {"node": "tl1", "activation": activation, "pid": None, "outcome": "pending",
                                       "phase": "reserved", "reserved_at": "2026-11-16T02:00:00Z",
                                       "started_at": "2026-11-16T02:00:00Z"})
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "pending"})
    monkeypatch.setenv("YGG_NOW", "2026-11-16T02:00:30Z")
    conductor.heimdall_tick()
    assert state.runtime("tl1")["activation"] == activation
    assert state.runtime("tl1")["outcome"] == "pending"
    monkeypatch.setenv("YGG_NOW", "2026-11-16T03:00:00Z")
    conductor.heimdall_tick()
    assert state.runtime("tl1")["activation"] == activation
    assert state.runtime("tl1")["outcome"] == "uncertain"
    assert any(a["kind"] == "ygg-uncertain" for a in state.alerts())
    assert not (world / "agent.log").exists()


# dev2 #2: reconciliation persists the receipt outcome, so a failed process reaches the liveness path.
def test_reconcile_persists_receipt_outcome_for_failed_process(world):
    from yggdrasil import conductor, launcher, state, tracker
    activation = tracker.allocate("activation")
    launcher.save_receipt(activation, {"node": "tl1", "activation": activation, "pid": 999999,
                                       "outcome": "uncertain", "exited_at": util.iso(), "exit_code": 1,
                                       "started_at": "2026-11-16T01:00:00Z", "retries_used": 0})
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "uncertain"})
    conductor.reconcile()
    assert launcher.receipt(activation)["outcome"] == "confirmed"
    assert state.runtime("tl1")["state"] == "running"


# dev2 #4: launcher_cli=codex picks the Codex model map; an incompatible Claude model is refused.
def test_codex_cli_uses_codex_model_map(world, monkeypatch):
    from yggdrasil import launcher, models, state
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nlauncher_cli: codex\n")
    clear_caches()
    assert models.resolve("pm1")["model"].startswith("gpt")
    # v1.1.1 (CB-D1): a PM on a Codex model runs on Codex; D1's refusal is gone.
    assert launcher.model_for(state.all_nodes()["pm1"])[2] == "codex"
    (world / "config.yaml").write_text("launcher_cli: codex\ncodex_models: {model: claude-opus-5-5, effort: high}\n")
    clear_caches()
    with pytest.raises(util.YggError) as error:
        launcher.model_for(state.all_nodes()["pm1"])
    assert "launcher_cli is codex" in error.value.message


# dev2 #5: senior users and reviewers get workspace-write rooted in scratch. v1.1.1 (CB-D3): the state and
# tracker roots are no longer writable; a senior user's ygg calls go through the relay.
def test_codex_sandbox_for_senior_users_adds_only_state_and_tracker(world):
    from yggdrasil import launcher
    data = {"activation": "act-0099", "role": "su",
            "pinned": {"YGG_STATE_ROOT": "/s", "YGG_FILES_ROOT": "/t", "YGG_CONFIG": "none"}}
    args = launcher.codex_sandbox(data, {"role": "su"})
    assert args[:2] == ["--sandbox", "workspace-write"]
    assert args[args.index("--cd") + 1].endswith("scratch/act-0099")
    assert [args[i + 1] for i, a in enumerate(args) if a == "--add-dir"] == []
    repo = world / "gitcheckout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert launcher.codex_sandbox({"activation": "a", "role": "tl", "cwd": str(repo)},
                                  {"role": "tl"}) == ["--sandbox", "workspace-write"]


# dev2 #6: the senior user's checkout is the lane head of the package waiting for testing.
def test_senior_user_checkout_targets_ready_lane_head(tmp_path, monkeypatch):
    from yggdrasil import launcher
    home = tmp_path / "repo"
    home.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(home), *a], check=True, capture_output=True, text=True)
    run("init", "-b", "main"); run("config", "user.name", "t"); run("config", "user.email", "t@example.invalid")
    run("-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", "base")
    run("branch", "ygg/p1/_integration")
    run("checkout", "-b", "ygg/p1/lane/tl1")
    run("-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", "lane work")
    lane_head = run("rev-parse", "HEAD").stdout.strip()
    run("checkout", "main")
    path = launcher.detached_worktree(str(home), tmp_path / "su", "ygg/p1/lane/tl1")
    assert subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip() == lane_head
    assert run("branch", "--show-current").stdout.strip() == "main"


def test_testing_target_picks_ready_package(world):
    from yggdrasil import commands, launcher, state
    package()
    commands.wp_complete("tl1", "WP-1")
    target = launcher._testing_target(state.all_nodes()["su1"], state.all_nodes())
    assert target == {"package": "WP-1", "lane": "tl1", "branch": "ygg/p1/lane/tl1"}


# QA #0: a stop scope survives a rebuild from the tracker.
def test_stop_scope_survives_rehydrate(world):
    import shutil
    from yggdrasil import launcher, rehydrate, state, stop
    stop.stop("alpha")
    shutil.rmtree(world / "state")
    rehydrate.rehydrate_all()
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1")
    assert error.value.code == util.EXIT_PAUSED


# QA #1: checkpoint batching state survives a rebuild, so the PM still wakes after four hours.
def test_checkpoint_batch_survives_rehydrate(world, monkeypatch):
    import shutil
    from yggdrasil import commands, rehydrate, state
    package()
    commands.tracker_write("tl1", "p1", "checkpoint", "60 percent", package="WP-1")
    shutil.rmtree(world / "state")
    rehydrate.rehydrate_all()
    monkeypatch.setenv("YGG_NOW", "2026-11-16T07:00:00Z")
    assert "pm1" in state.wakes()


# QA #3: a package rejected by the senior user still counts toward the WIP limit.
def test_rejected_package_counts_toward_wip(world):
    from yggdrasil import commands, status
    from yggdrasil.commands import pid_document
    header = pid_document("p1").header
    assert header  # PID exists
    path = world / "tracker/projects/p1/documents/pid.md"
    text = path.read_text().replace("schema: ygg.pid.v1", "schema: ygg.pid.v1\nwip_limit: 1", 1)
    path.write_text(text)
    package()
    commands.wp_complete("tl1", "WP-1")
    commands.acceptance_record("su1", "WP-1", "0" * 40, "reject")
    commands.tracker_write("pm1", "p1", "work-package", "Next", stage="1", package="WP-2")
    commands.tracker_write("su1", "p1", "criteria", "Test", package="WP-2")
    with pytest.raises(util.YggError) as error:
        commands.wp_authorise("p1", "WP-2", "tl1")
    assert "wip-limit" in error.value.message
    delivery = status.build_status()["projects"][0]["delivery"]
    assert delivery["wip"] == {"limit": 1, "in_progress": 1}


# Task item 4: the conductor tick repairs lost local state from the journal (same trusted rule).
def test_tick_replays_journal_without_rewaking_acked_messages(world):
    import shutil
    from yggdrasil import ratatoskr, rehydrate, state
    msg = ratatoskr.send("pm1", "tl1", "note", "hello")
    ratatoskr.inbox("tl1", ack=msg)
    rehydrate.replay_journal()
    assert "tl1" not in state.wakes()
    ratatoskr.send("pm1", "tl1", "note", "second")
    shutil.rmtree(world / "state" / "inboxes")
    rehydrate.replay_journal()
    assert "second" in ratatoskr.inbox("tl1") and "hello" not in ratatoskr.inbox("tl1")
    assert "tl1" in state.wakes()
