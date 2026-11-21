"""Unit tests for the round-3 review findings (dev #2 pair review and QA)."""

import json
import subprocess
import sys

import pytest
import yaml

from yggdrasil import config, util
from test_round2 import clear_caches, package, wait_exit, world  # noqa: F401  (fixtures and helpers)


def receipt_file(activation, **fields):
    from yggdrasil import launcher
    data = {"node": fields.pop("node", "tl1"), "activation": activation, "launcher": "headless"}
    data.update(fields)
    launcher.save_receipt(activation, data)
    return data


def uncleared(kind, programme="alpha", **match):
    from yggdrasil import journal
    return [e for e in journal.entries(programme) if e.get("kind") == kind and not e.get("cleared_at")
            and all(e.get(k) == v for k, v in match.items())]


# 2: a pending activation whose launching process exited after the supervisor recorded a PID is resolved.
def test_pending_activation_with_recorded_process_is_reconciled(world, monkeypatch):
    from yggdrasil import conductor, launcher, state, tracker
    activation = tracker.allocate("activation")
    receipt_file(activation, pid=999999, process_started_at="x", outcome="pending", phase="starting",
                 reserved_at="2026-11-16T00:00:00Z", started_at="2026-11-16T00:00:00Z",
                 exited_at="2026-11-16T00:01:00Z", exit_code=1, retries_used=0)
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "pending"})
    flagged, relaunch = conductor.heimdall_scan()
    assert launcher.receipt(activation)["outcome"] == "confirmed"
    assert state.runtime("tl1")["outcome"] == "confirmed"
    assert [r["node"] for r in relaunch] == ["tl1"]  # failed: the liveness path retries it


# 2: a successful supervisor exit leaves an idle runtime with outcome pending; reconcile confirms it.
def test_idle_runtime_left_pending_after_success_is_confirmed(world):
    from yggdrasil import conductor, launcher, state, tracker
    activation = tracker.allocate("activation")
    receipt_file(activation, pid=999999, outcome="pending", phase="starting", reserved_at=util.iso(),
                 started_at=util.iso(), exited_at=util.iso(), exit_code=0)
    state.save_runtime("tl1", {"state": "idle", "activation": activation, "outcome": "pending"})
    state.wake("tl1", "message")
    conductor.reconcile()
    assert launcher.receipt(activation)["outcome"] == "confirmed"
    assert state.runtime("tl1")["outcome"] == "confirmed" and state.runtime("tl1")["state"] == "idle"
    assert "tl1" in conductor.candidates()


# 3: liveness retries share admission and the per-tick cap with woken nodes, and are reported.
def test_liveness_retries_count_toward_tick_cap_and_admission(world, monkeypatch):
    from yggdrasil import conductor, launcher, state, tracker
    for node in ("pm1", "tl1", "su1"):
        activation = tracker.allocate("activation")
        receipt_file(activation, node=node, pid=999999, process_started_at="x", outcome="confirmed",
                     started_at="2026-11-16T00:00:00Z", exited_at="2026-11-16T00:00:01Z", exit_code=1,
                     retries_used=0)
        state.save_runtime(node, {"state": "running", "activation": activation, "outcome": "confirmed"})
    calls = []
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: calls.append((node, kw.get("reason"))))
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nsettle_delay_seconds: 0\n"
                                       "load_max_age_seconds: 999999999\ntick_cap: count\nmax_launches_per_tick: 1\n")
    clear_caches()
    result = conductor.tick()
    assert len(calls) == 1 and calls[0][1] == "relaunch"
    assert result["launched"] == [calls[0][0]]
    calls.clear()
    monkeypatch.setattr(conductor, "admit", lambda: (False, "load 0.95 above 0.85"))
    result = conductor.tick()
    assert calls == [] and result["launched"] == []
    assert sorted(result["held"]) == ["pm1", "su1", "tl1"]
    assert all(state.runtime(n)["state"] == "running" for n in ("pm1", "su1", "tl1"))


# 4: an emergency stop while the worktree is prepared aborts the launch before the agent starts.
def test_stop_during_preparation_aborts_launch(world, monkeypatch):
    from yggdrasil import launcher, state, stop
    real = launcher.working_directory

    def preparing(node, nodes):
        path = real(node, nodes)
        stop.stop("alpha")
        return path

    monkeypatch.setattr(launcher, "working_directory", preparing)
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1")
    assert error.value.code == util.EXIT_PAUSED
    data = launcher.receipt(state.runtime("tl1")["activation"])
    assert data["outcome"] == "aborted" and data["pid"] is None
    assert state.runtime("tl1")["state"] == "stopped"
    assert not (world / "agent.log").exists()


# 4: the supervisor re-checks pause under the lock right before the agent process starts.
def test_supervisor_aborts_when_paused_before_agent_start(world):
    from yggdrasil import commands, launcher, state, tracker
    activation = tracker.allocate("activation")
    receipt_file(activation, outcome="pending", phase="starting", pid=None, cwd=str(world), settings="s.json",
                 model="claude-opus-5-5", effort="high", cli="claude-code", programme="alpha", project="p1",
                 role="tl", pinned={})
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "pending"})
    commands.pause("alpha")
    assert launcher.supervise(activation) == 0
    data = launcher.receipt(activation)
    assert data["outcome"] == "aborted" and data["pid"] is None
    assert state.runtime("tl1")["outcome"] == "aborted" and state.runtime("tl1")["state"] == "idle"
    assert "tl1" in state.wakes()  # starts after resume
    assert not (world / "agent.log").exists()


# 6: Codex scratch sessions skip the repository check and are told the prepared testing checkout.
def test_codex_testing_session_names_checkout_and_skips_git_check(world, monkeypatch):
    from yggdrasil import launcher, state, tracker
    argv_file = world / "argv.json"
    fake = world / "codex"
    fake.write_text("#!" + sys.executable + "\nimport json, os, sys\njson.dump({'argv': sys.argv, "
                    "'stdin': sys.stdin.read(), 'checkout': os.environ.get('YGG_TESTING_CHECKOUT')}, open(" + repr(str(argv_file)) + ", 'w'))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("YGG_CODEX_BIN", str(fake))
    activation = tracker.allocate("activation")
    receipt_file(activation, node="su1", outcome="pending", phase="starting", pid=None, cwd=str(world),
                 model="gpt-6.1-sol", effort="xhigh", cli="codex", programme="alpha", project="p1", role="su",
                 pinned={"YGG_STATE_ROOT": str(world / "state"), "YGG_FILES_ROOT": str(world / "tracker")},
                 testing={"package": "WP-1", "lane": "tl1", "branch": "ygg/p1/lane/tl1",
                          "checkout": "/repo/.ygg-worktrees/p1/_test-su1"})
    state.save_runtime("su1", {"state": "running", "activation": activation, "outcome": "pending"})
    launcher.supervise(activation)
    seen = json.loads(argv_file.read_text())
    assert "--skip-git-repo-check" in seen["argv"]
    assert "git -C /repo/.ygg-worktrees/p1/_test-su1 rev-parse HEAD" in seen["stdin"]
    # Pilot 2 run 3: the session is told to test now, not only where its checkout is.
    assert "Package WP-1 is ready for testing and this session is for it: test it now" in seen["stdin"]
    assert "ygg acceptance record --from su1 --package WP-1 --sha <that sha> --verdict accept|reject" in seen["stdin"]
    assert seen["argv"][-1] == "-"
    assert seen["checkout"] == "/repo/.ygg-worktrees/p1/_test-su1"


# 6: a failed checkout of an existing testing worktree fails the launch with a clear error.
def test_testing_checkout_failure_fails_launch(world, monkeypatch, tmp_path):
    from yggdrasil import launcher
    path = tmp_path / "wt"
    (path / ".git").mkdir(parents=True)

    class Result:
        returncode, stdout, stderr = 1, "", "error: pathspec did not match"

    monkeypatch.setattr(launcher, "branch_exists", lambda home, ref: True)
    monkeypatch.setattr(launcher, "_foreign_checkout", lambda home, path, branch: False)
    monkeypatch.setattr(launcher, "git", lambda *args, **kw: Result())
    with pytest.raises(util.YggError) as error:
        launcher.detached_worktree(str(tmp_path), path, "ygg/p1/lane/tl1")
    assert "cannot check out" in error.value.message and "pathspec" in error.value.message


# 7: Codex defaults never override explicit user models settings.
def test_codex_defaults_keep_explicit_models(world):
    from yggdrasil import models
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nlauncher_cli: codex\nmodels:\n  effort: low\n")
    clear_caches()
    resolved = models.resolve("pm1")
    assert resolved["effort"] == "low" and resolved["model"] == "gpt-6.1-sol"
    (world / "config.yaml").write_text("launcher_cli: codex\nmodels:\n  model: gpt-5.9\n  pair_model: gpt-5.9\n")
    clear_caches()
    resolved = models.resolve("pm1")
    assert resolved["model"] == "gpt-5.9" and resolved["pair_model"] == "gpt-5.9" and resolved["effort"] == "high"


# 8: checkpoint batching counts a lane whose authorised package is ready for test as unfinished.
def test_checkpoint_batch_waits_for_ready_for_test_lane(world):
    from yggdrasil import commands, events, state
    commands.node_add("tl2", "tl", "pm1")
    commands.node_add("su2", "su", "pm1", "tl2")
    package("WP-1")
    commands.tracker_write("pm1", "p1", "work-package", "Build", stage="1", package="WP-2")
    commands.tracker_write("su2", "p1", "criteria", "Test", package="WP-2")
    commands.wp_authorise("p1", "WP-2", "tl2")
    commands.wp_complete("tl1", "WP-1")
    def batch_owed():
        return any(r.startswith("checkpoints") for r in (state.wakes().get("pm1") or {}).get("reasons") or [])

    events.note_checkpoint("pm1", "tl2")
    assert not batch_owed()
    events.note_checkpoint("pm1", "tl1")
    assert batch_owed()


# 9 envelope-end-review: a through-stage envelope ends once a later stage is current; replay restores the wake.
def test_through_stage_envelope_end_wakes_reviewer_and_replays(world, monkeypatch):
    from yggdrasil import commands, events, rehydrate, state, tracker
    commands.node_add("rv1", "reviewer", "alpha")
    base = tracker.project_dir("p1") / "envelopes"
    base.mkdir(parents=True, exist_ok=True)
    (base / "EN-0001.json").write_text(json.dumps({
        "id": "EN-0001", "scope": "p1", "until": None, "through_stage": 1, "state": "active",
        "granted_at": "2026-11-16T01:00:00Z", "actor": "user"}))
    events.envelope_expiries()
    assert "rv1" not in state.wakes()  # stage 1 is current: not ended
    milestones = tracker.milestones("p1")
    for stage in milestones["stages"]:
        stage["state"] = "done" if stage["number"] == 1 else ("executing" if stage["number"] == 2 else stage["state"])
    tracker.save_milestones("p1", milestones)
    events.envelope_expiries()
    assert "rv1" in state.wakes()
    rehydrate.replay_journal()
    assert "rv1" in state.wakes()


# 9 amendment-a3: the WIP limit comes through the layered config, not only the PID.
def test_wip_limit_through_layered_config(world):
    from yggdrasil import commands
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nwip_limit: 1\n")
    clear_caches()
    package("WP-1")
    commands.tracker_write("pm1", "p1", "work-package", "Build", stage="1", package="WP-2")
    commands.tracker_write("su1", "p1", "criteria", "Test", package="WP-2")
    with pytest.raises(util.YggError) as error:
        commands.wp_authorise("p1", "WP-2", "tl1")
    assert "wip-limit" in error.value.message


# 9 amendment-a3: a discovered tracker plugin with a files-format root is selectable and used.
def test_discovered_tracker_plugin_root_is_used(world, monkeypatch, tmp_path):
    from yggdrasil import tracker
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / "ygg_memtracker-0.1.dist-info").mkdir(parents=True)
    store = tmp_path / "memstore"
    (plugins_dir / "ygg_memtracker.py").write_text(
        "api_version = 1\nclass Tracker:\n    api_version = 1\n    @staticmethod\n"
        "    def root():\n        return " + repr(str(store)) + "\n")
    (plugins_dir / "ygg_memtracker-0.1.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: ygg_memtracker\nVersion: 0.1\n")
    (plugins_dir / "ygg_memtracker-0.1.dist-info" / "entry_points.txt").write_text(
        "[yggdrasil.trackers]\nmemory = ygg_memtracker:Tracker\n")
    monkeypatch.setenv("YGG_PLUGIN_PATH", str(plugins_dir))
    monkeypatch.setenv("YGG_TRACKER", "memory")
    monkeypatch.syspath_prepend(str(plugins_dir))
    clear_caches()
    assert tracker.root() == store


# 9 worktree-repository-identity: the approval base is found from a worktree path of the named repo.
def test_trusted_base_found_by_repository_identity(world, monkeypatch, tmp_path):
    import os
    from yggdrasil import commands, gitops
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    run("init", "-q", "-b", "main")
    run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "base")
    sha = run("rev-parse", "HEAD").stdout.strip()
    worktree = tmp_path / "wt"
    run("worktree", "add", "-q", "-b", "ygg/p1/lane/tl1", str(worktree))
    monkeypatch.setattr(commands, "approved_pid", lambda project: {"main_at_approval": {os.path.realpath(repo): sha}})
    assert gitops.trusted_base("p1", str(worktree)) == sha


# 9 initial-role-context: senior users and reviewers get the brief verbatim, fenced as data.
def test_brief_passed_verbatim_to_senior_user(world):
    from yggdrasil import launcher, state, tracker
    text = "# Brief\n\nGoal: ship it.\n\n## Grilling\n\nQ: scope? A: ```small```\n"
    tracker.write_document("p1", "brief", text)
    prompt = launcher.prompt_for(state.all_nodes()["su1"], "act-0001")
    assert text.rstrip("\n") in prompt and "````text" in prompt
    assert "Goal: ship it." not in launcher.prompt_for(state.all_nodes()["tl1"], "act-0001")
