"""Pilot readiness: fixes found by the sandbox dry run."""

import json
import os
import sys
import time

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


def _wait(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.05)
    return predicate()


def test_a_stopped_session_consumes_nothing_and_its_work_is_offered_again(world, monkeypatch):
    from yggdrasil import commands, conductor, launcher, lifecycle, ratatoskr, reducer, stop
    monkeypatch.setenv("YGG_UNIT_AGENT_SLEEP", "30")
    msg = ratatoskr.send("pm1", "tl1", "note", "work to do")
    result = launcher.launch("tl1")
    activation = result["activation"]
    assert msg not in [i["id"] for i in reducer.owed("tl1")]  # reserved while it runs
    assert stop.stop("alpha").startswith("stopped 1 session(s)")
    assert _wait(lambda: launcher.receipt(activation).get("exited_at") is not None)
    [end] = [e for e in lifecycle.facts("alpha", activation) if e["phase"] == "exited"]
    assert end.get("interrupted") is True
    commands.resume("alpha")
    assert msg in [i["id"] for i in reducer.owed("tl1")]  # the interrupted work is owed again
    assert "tl1" in conductor.candidates()


def test_a_clean_exit_is_ambiguous_unless_every_signal_attempt_is_proven_not_delivered(world):
    from yggdrasil import launcher, lifecycle
    first = lifecycle.mark_signalled("act-0042", "stop")  # intent: unresolved (in the gap, or the stopper died)
    assert launcher.exit_fields({"activation": "act-0042", "exit_code": 0}).get("ambiguous") is True
    lifecycle.mark_sent("act-0042", "stop", first)
    assert launcher.exit_fields({"activation": "act-0042", "exit_code": 0}).get("ambiguous") is True
    lifecycle.mark_missed("act-0042", "stop", first)  # a delivered signal is never downgraded
    assert lifecycle.signal_state("act-0042") == "sent"
    only = lifecycle.mark_signalled("act-0045", "stop")
    lifecycle.mark_missed("act-0045", "stop", only)  # the kill call failed: the exit was the agent's own
    fields = launcher.exit_fields({"activation": "act-0045", "exit_code": 0})
    assert "interrupted" not in fields and "ambiguous" not in fields  # a proven completion stays consumed
    lifecycle.mark_signalled("act-0045", "stop")  # a new attempt opens its own unresolved intent
    assert lifecycle.signal_state("act-0045") == "intent"
    assert launcher.exit_fields({"activation": "act-0043", "exit_code": -9}).get("interrupted") is True
    assert "interrupted" not in launcher.exit_fields({"activation": "act-0044", "exit_code": 0})


def test_overlapping_stops_where_one_misses_and_one_is_unresolved_end_held(world, monkeypatch):
    from yggdrasil import interlock, launcher, lifecycle, ratatoskr, reducer, status
    monkeypatch.setenv("YGG_UNIT_AGENT_SLEEP", "2")  # the agent finishes on its own, exit 0
    msg = ratatoskr.send("pm1", "tl1", "note", "work to do")
    activation = launcher.launch("tl1")["activation"]
    record = launcher.receipt(activation)
    lifecycle.mark_signalled(activation, "stop")  # stop 1 records its intent, then dies before its kill call
    monkeypatch.setattr(interlock, "_signal", lambda pid, number: False)
    interlock.signal_targets([("tl1", activation, record["pid"], record["process_started_at"])])  # stop 2 misses
    assert _wait(lambda: lifecycle.open_activation("tl1") is None)
    [end] = [e for e in lifecycle.facts("alpha", activation) if e["phase"] == "exited"]
    assert end.get("ambiguous") is True  # stop 1 may still have reached it: held, not consumed
    assert msg not in [i["id"] for i in reducer.owed("tl1")]
    assert activation in status.build_status()["uncertain_keys"]
    assert sorted(e["state"] for e in lifecycle._attempts(activation).values()) == ["intent", "missed"]


def test_a_signal_record_from_before_attempts_still_reads(world):
    from yggdrasil import lifecycle, util as u
    for value, expected in (("sent", "sent"), ("missed", "missed"), ("intent", "intent")):
        key = "act-00" + str(50 + len(value))
        lifecycle.signalled_path(key).parent.mkdir(parents=True, exist_ok=True)
        u.write_json(lifecycle.signalled_path(key), {"activation": key, "reason": "stop", "state": value})
        assert lifecycle.signal_state(key) == expected


def test_a_stop_whose_kill_call_fails_records_the_signal_as_missed(world, monkeypatch):
    from yggdrasil import interlock, lifecycle
    calls = iter([True, False])  # alive when targeted, gone by the grace check
    monkeypatch.setattr(interlock, "_same", lambda pid, started: next(calls, False))
    monkeypatch.setattr(interlock, "_signal", lambda pid, number: False)  # the agent exited first
    interlock.signal_targets([("tl1", "act-0042", 999999, "t0")])
    assert lifecycle.signal_state("act-0042") == "missed"


def test_a_clean_exit_before_sent_is_recorded_is_held_not_consumed(world, tmp_path, monkeypatch):
    from yggdrasil import commands, launcher, lifecycle, ratatoskr, reducer, status, stop
    agent = tmp_path / "clean-exit-agent"
    agent.write_text("#!" + sys.executable + "\nimport signal, sys, time\n"
                     "signal.signal(signal.SIGINT, lambda *a: sys.exit(0))\ntime.sleep(30)\n")
    agent.chmod(0o755)
    monkeypatch.setenv("YGG_CLAUDE_BIN", str(agent))
    msg = ratatoskr.send("pm1", "tl1", "note", "work to do")
    activation = launcher.launch("tl1")["activation"]
    sent = lifecycle.mark_sent

    def late_sent(key, why, attempt):  # the agent's exit is recorded before the stopper records delivery
        assert _wait(lambda: launcher.receipt(key).get("exited_at") is not None)
        sent(key, why, attempt)

    monkeypatch.setattr(lifecycle, "mark_sent", late_sent)
    stop.stop("alpha")
    [end] = [e for e in lifecycle.facts("alpha", activation) if e["phase"] == "exited"]
    assert end.get("ambiguous") is True
    commands.resume("alpha")
    assert msg not in [i["id"] for i in reducer.owed("tl1")]  # not replayed on its own ...
    assert activation in status.build_status()["uncertain_keys"]  # ... and not silently consumed


def test_a_held_stop_that_owns_work_refuses_every_launch_until_released(world):
    from yggdrasil import launcher, lifecycle, noderepair, ratatoskr, tracker
    from test_rebuild_t4 import SPAWNED, given
    msg = ratatoskr.send("pm1", "tl1", "note", "work")
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 0, "ambiguous": True}), messages=[msg])
    tracker.set_next("activation", 43)  # new activations are newer than the held one, as in production
    for kwargs in ({}, {"explicit": False}, {"reason": "relaunch", "expected_activation": "act-0042"}):
        with pytest.raises(util.YggError) as refused:
            launcher.launch("tl1", **kwargs)
        assert refused.value.code == util.EXIT_REFUSED == 3
        assert "message:" + msg in refused.value.message
        assert "run `ygg node release tl1 --as aborted|failed` first" in refused.value.message
    assert not lifecycle.facts("alpha", "act-0043")  # nothing was reserved
    noderepair.release("tl1", "aborted")
    second = launcher.launch("tl1")["activation"]
    [reserved] = [e for e in lifecycle.facts("alpha", second) if e["phase"] == "reserved"]
    assert msg in reserved["work"]["messages"]  # settled as cut short: offered again, once


def test_an_explicit_launch_goes_ahead_when_the_held_stop_owns_nothing(world):
    from yggdrasil import launcher, lifecycle, tracker
    from test_rebuild_t4 import SPAWNED, given
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 0, "ambiguous": True}))
    tracker.set_next("activation", 43)
    with pytest.raises(util.YggError):
        launcher.launch("tl1", explicit=False)  # the conductor still waits for the user
    assert launcher.launch("tl1")["activation"] == "act-0043"
    assert lifecycle.ambiguous_activations(["tl1"]) == ["act-0042"]  # the hold stays until it is settled


def test_a_held_retry_survives_a_newer_attempt_that_was_not_given_it(world):
    from yggdrasil import conductor, noderepair, reducer
    from test_rebuild_t4 import SPAWNED, given
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 1}))  # failed: owes retry 1
    [retry] = [i for i in reducer.owed("tl1") if i["kind"] == "retry"]
    assert retry["id"] == "act-0042" and retry["retries_used"] == 1
    given("tl1", "act-0043", SPAWNED, ("exited", {"exit_code": 0, "ambiguous": True}), retries=["act-0042"])
    given("tl1", "act-0044", SPAWNED, ("exited", {"exit_code": 0}), messages=[], retries=[])  # not given it
    assert not [i for i in reducer.owed("tl1") if i["kind"] == "retry"]  # held, not owed
    noderepair.release("tl1", "aborted")
    [retry] = [i for i in reducer.owed("tl1") if i["kind"] == "retry"]
    assert retry["id"] == "act-0042" and retry["retries_used"] == 1  # restored, with its count
    assert "tl1" in conductor.candidates()


def test_a_later_attempt_given_the_retry_settles_it(world):
    from yggdrasil import reducer
    from test_rebuild_t4 import SPAWNED, given
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 1}))
    given("tl1", "act-0043", SPAWNED, ("exited", {"exit_code": 0}), retries=["act-0042"])
    assert not [i for i in reducer.owed("tl1") if i["kind"] == "retry"]
    given("tl1", "act-0044", SPAWNED, ("exited", {"exit_code": 1}), retries=[])
    [retry] = [i for i in reducer.owed("tl1") if i["kind"] == "retry"]
    assert retry["id"] == "act-0044"  # only the newest failure owes a retry


def test_concurrent_signal_attempts_never_lose_a_delivery(world, tmp_path):
    import subprocess
    from yggdrasil import lifecycle
    missing = lifecycle.mark_signalled("act-0042", "stop")
    delivering = lifecycle.mark_signalled("act-0042", "stop")
    ready = tmp_path / "missed-writer-has-read"
    script = ("import pathlib, time\nfrom yggdrasil import lifecycle\nread = lifecycle._attempts\n"
              "def slow(activation):\n    value = read(activation)\n"
              "    pathlib.Path(" + repr(str(ready)) + ").write_text('read')\n    time.sleep(1.0)\n"
              "    return value\nlifecycle._attempts = slow\n"
              "lifecycle.mark_missed('act-0042', 'stop', " + repr(missing) + ")\n")
    # One stop's kill call failed: it reads the record, then is slow to write "missed" ...
    child = subprocess.Popen([sys.executable, "-c", script], env=dict(os.environ))
    assert _wait(ready.exists, 10)
    lifecycle.mark_sent("act-0042", "stop", delivering)  # ... while the other stop delivers its signal
    assert child.wait(20) == 0
    attempts = lifecycle._attempts("act-0042")
    assert attempts[delivering]["state"] == "sent" and attempts[missing]["state"] == "missed"
    assert lifecycle.signal_state("act-0042") == "sent"


def test_a_stop_held_alert_lost_to_a_crash_is_raised_on_the_next_pass(world, monkeypatch):
    from yggdrasil import lifecycle, state
    from test_rebuild_t4 import SPAWNED, given
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 0, "ambiguous": True}))
    real = state.alert

    def crash(kind, message, **fields):
        raise SystemExit(97)  # the process dies before the alert line is written

    monkeypatch.setattr(state, "alert", crash)
    with pytest.raises(SystemExit):
        lifecycle.alert_stop_holds()
    monkeypatch.setattr(state, "alert", real)
    lifecycle.alert_stop_holds()
    lifecycle.alert_stop_holds()
    assert [a["activation"] for a in state.alerts() if a["kind"] == "ygg-stop-held"] == ["act-0042"]


def test_a_held_stop_shows_the_node_blocked_by_an_uncertain_activation(world):
    from yggdrasil import noderepair, status
    from test_rebuild_t4 import SPAWNED, given
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 0, "ambiguous": True}))
    [view] = [n for n in status.build_status()["nodes"] if n["id"] == "tl1"]
    assert view["state"] == "blocked" and "uncertain-activation" in view["blocked_by"]
    noderepair.release("tl1", "failed")
    [view] = [n for n in status.build_status()["nodes"] if n["id"] == "tl1"]
    assert view["state"] != "blocked" and "uncertain-activation" not in view["blocked_by"]


def test_a_refused_launch_raises_one_alert_with_its_reason(world, monkeypatch):
    from yggdrasil import conductor, launcher, state
    monkeypatch.setattr(conductor, "admit", lambda: (True, "ok"))

    def refuse(node, **kw):
        raise util.YggError(util.EXIT_REFUSED, "launch_env may not set YGG_WORKER_GUIDANCE")

    monkeypatch.setattr(launcher, "launch", refuse)
    conductor.launch_queue([{"node": "tl1"}])
    conductor.launch_queue([{"node": "tl1"}])  # the same reason again: no second alert
    alerts = [a for a in state.alerts() if a["kind"] == "ygg-launch-refused"]
    assert len(alerts) == 1 and "YGG_WORKER_GUIDANCE" in alerts[0]["reason"] and alerts[0]["node"] == "tl1"
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: {"started": True})
    conductor.launch_queue([{"node": "tl1"}])
    monkeypatch.setattr(launcher, "launch", refuse)
    conductor.launch_queue([{"node": "tl1"}])  # refused again after a launch: reported again
    assert len([a for a in state.alerts() if a["kind"] == "ygg-launch-refused"]) == 2


def test_approving_the_final_stage_closes_the_project(world):
    from yggdrasil import commands, conductor, status, tracker
    data = tracker.milestones("p1")
    for stage in data["stages"]:
        stage["state"] = "approved"
    data["stages"][-1]["state"] = "awaiting-approval"
    tracker.save_milestones("p1", data)
    project = tracker.project("p1")
    project["state"] = "awaiting-approval"
    tracker.save_project(project)
    assert "final stage" in commands.stage_approve("p1")
    assert tracker.project("p1")["state"] == "closed"
    assert [p["state"] for p in status.build_status()["projects"] if p["id"] == "p1"] == ["closed"]
    conductor.recover_records()  # the recorded transition is already applied: nothing reopens it
    assert tracker.project("p1")["state"] == "closed"


def test_approving_a_middle_stage_keeps_the_project_executing(world):
    from yggdrasil import commands, tracker
    data = tracker.milestones("p1")
    data["stages"][0]["state"] = "awaiting-approval"
    tracker.save_milestones("p1", data)
    commands.stage_approve("p1")
    assert tracker.project("p1")["state"] == "executing"


def test_paused_nodes_show_as_paused_not_queued(world):
    from yggdrasil import commands, ratatoskr, reducer, status
    ratatoskr.send("pm1", "tl1", "note", "work")
    commands.pause("alpha")
    view = status.build_status()
    tl1 = next(n for n in view["nodes"] if n["id"] == "tl1")
    assert tl1["state"] == "blocked" and tl1["paused"] is True and "pause:alpha" in tl1["blocked_by"]
    assert ", paused" in status.human(view)
    assert reducer.shadow_diff(view) == {}
    commands.resume("alpha")
    tl1 = next(n for n in status.build_status()["nodes"] if n["id"] == "tl1")
    assert tl1["state"] == "queued" and tl1["paused"] is False


def test_an_unquoted_yaml_boolean_for_branch_protection_is_refused_with_the_fix(world, monkeypatch):
    from yggdrasil import commands, config
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nsettle_delay_seconds: 0\nbranch_protection: off\n")
    clear_caches()
    with pytest.raises(util.YggError) as error:
        config.validate()
    assert 'quote the mode: "refuse", "warn" or "off"' in error.value.message
    with pytest.raises(util.YggError) as error:
        commands.protection_mode({}, {})
    assert "YAML boolean false" in error.value.message
    (world / "config.yaml").write_text('launch_confirm_seconds: 0.5\nsettle_delay_seconds: 0\nbranch_protection: "off"\n')
    clear_caches()
    config.validate()
    assert commands.protection_mode({}, {}) == "off"


def test_agent_output_is_kept_per_activation_and_private(world):
    import stat
    from yggdrasil import launcher
    result = launcher.launch("tl1")
    activation = result["activation"]
    assert _wait(lambda: launcher.receipt(activation).get("exited_at") is not None)
    for stream in ("stdout", "stderr"):
        path = launcher.output_path(activation, stream)
        assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(launcher.output_path(activation, "stdout").parent.stat().st_mode) == 0o700


def test_the_worktree_folder_is_excluded_from_the_users_checkout(tmp_path):
    import subprocess
    from yggdrasil import launcher
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "initial"], check=True)
    launcher.ensure_worktree(str(repo), repo / ".ygg-worktrees" / "p1" / "_integration", "ygg/p1/_integration",
                             "main")
    launcher.exclude_worktrees(str(repo))  # idempotent
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count("/.ygg-worktrees/") == 1
    status = subprocess.run(["git", "-C", str(repo), "status", "--short"], capture_output=True, text=True).stdout
    assert ".ygg-worktrees" not in status


def test_an_unreadable_shell_command_is_refused_with_guidance_not_as_a_hard_stop():
    from yggdrasil import hooks
    code, message = hooks.static_checks("Bash", {"command": "git --weird push origin ygg/a"})
    assert code == 2 and message.startswith("Hard stop (unresolved) denied")
    assert "not a real hard stop" in message and "nobody's approval" in message and "Write or Edit tool" in message
    assert hooks.static_checks("Bash", {"command": "git push --force origin main"})[1].startswith("Hard stop (")


def test_role_prompts_carry_the_brief_the_formats_and_who_is_who(world):
    from yggdrasil import commands, launcher, state
    commands.tracker_write("pm1", "p1", "brief", "# Brief\nAnswers.")
    nodes = state.all_nodes()
    pm_prompt = launcher.prompt_for(nodes["pm1"], "act-0042")
    assert "brief.md" in pm_prompt and "schema: ygg.pid.v1" in pm_prompt and "--kind pid-ready" in pm_prompt
    assert "su1: senior user, paired with tl1." in pm_prompt and "l1: the main session" in pm_prompt
    assert "Destination, Frontier, Blockers, Decisions so far, Fog, Out of scope" in pm_prompt
    tl_prompt = launcher.prompt_for(nodes["tl1"], "act-0043")
    assert "--ack <message id>" in tl_prompt and "not a real hard stop" in tl_prompt and "WP-<n>" in tl_prompt
    assert "pm1: project manager." in tl_prompt


def test_agents_get_their_own_claude_config_folder_when_the_profile_names_one(world, tmp_path, monkeypatch):
    from yggdrasil import launcher
    folder = tmp_path / "agent-claude"
    profile = tmp_path / "profile.yaml"
    profile.write_text("schema: ygg.config.v1\nname: pilot\nagent_claude_config_dir: " + str(folder) + "\n")
    monkeypatch.setenv("YGG_PROFILE", str(profile))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/personal")
    clear_caches()
    data = {"node": "tl1", "activation": "act-0042", "cli": "claude-code", "programme": "alpha"}
    assert launcher.child_env(data)["CLAUDE_CONFIG_DIR"] == str(folder)
    assert "CLAUDE_CONFIG_DIR" not in launcher.child_env(dict(data, cli="codex"))
    profile.write_text("schema: ygg.config.v1\nname: pilot\n")
    clear_caches()
    assert launcher.child_env(data)["CLAUDE_CONFIG_DIR"] == "/somewhere/personal"  # unset: inherited as before


def test_max_concurrent_sessions_holds_launches_at_the_cap(world, monkeypatch):
    from yggdrasil import conductor, config, launcher
    monkeypatch.setattr(conductor, "admit", lambda: (True, "ok"))
    launched = []
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: launched.append(node) or {"started": True})
    monkeypatch.setattr(conductor, "running_sessions", lambda: 2)
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nsettle_delay_seconds: 0\n"
                                       "max_concurrent_sessions: 2\n")
    clear_caches()
    config.validate()
    result = conductor.launch_queue([{"node": "tl1"}])
    assert launched == [] and result["held"] == ["tl1"]
    monkeypatch.setattr(conductor, "running_sessions", lambda: 1)
    conductor.launch_queue([{"node": "tl1"}])
    assert launched == ["tl1"]
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nmax_concurrent_sessions: 0\n")
    clear_caches()
    with pytest.raises(util.YggError):
        config.validate()


def test_running_sessions_counts_open_activations_with_live_evidence(world):
    from yggdrasil import conductor
    from test_rebuild_t3 import seed
    assert conductor.running_sessions() == 0
    identifier = seed("tl1", "--phase", "spawned", "--process", "live")
    from yggdrasil import launcher
    try:
        assert conductor.running_sessions() == 1
    finally:
        os.killpg(launcher.receipt(identifier)["pid"], 9)


def _fake_gh(tmp_path, answers: dict):
    """A gh stand-in answering `gh api <path>` from a table (path -> (exit code, body))."""
    script = tmp_path / "gh"
    table = tmp_path / "gh.json"
    table.write_text(json.dumps(answers))
    script.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                      "answers = json.load(open(" + repr(str(table)) + "))\n"
                      "code, body = answers.get(sys.argv[2], [1, 'unknown path'])\n"
                      "(sys.stdout if code == 0 else sys.stderr).write(body)\nsys.exit(code)\n")
    script.chmod(0o755)
    return str(script)


RULES = [{"type": "non_fast_forward", "ruleset_id": 7}, {"type": "deletion", "ruleset_id": 7},
         {"type": "pull_request", "ruleset_id": 7, "parameters": {"required_approving_review_count": 1}}]


@pytest.mark.parametrize("ruleset,expected", [
    ({"enforcement": "active", "bypass_actors": [], "current_user_can_bypass": "never"}, "protected"),
    ({"enforcement": "active", "bypass_actors": [{"actor_type": "RepositoryRole"}],
      "current_user_can_bypass": "never"}, "insufficient"),
    ({"enforcement": "active", "bypass_actors": [], "current_user_can_bypass": "always"}, "insufficient"),
    ({"enforcement": "evaluate", "bypass_actors": [], "current_user_can_bypass": "never"}, "insufficient"),
])
def test_a_ruleset_nobody_may_bypass_protects_main(tmp_path, monkeypatch, ruleset, expected):
    from yggdrasil import commands
    gh = _fake_gh(tmp_path, {
        "repos/me/site/branches/main/protection": [1, "gh: Branch not protected (HTTP 404)"],
        "repos/me/site/rules/branches/main": [0, json.dumps(RULES)],
        "repos/me/site/rulesets/7": [0, json.dumps(ruleset)],
    })
    monkeypatch.setenv("YGG_GH_BIN", gh)
    assert commands.protection_status({"github": "me/site", "path": None})[0] == expected


def test_a_ruleset_whose_bypass_list_the_token_cannot_see_is_unknown(tmp_path, monkeypatch):
    from yggdrasil import commands
    gh = _fake_gh(tmp_path, {
        "repos/me/site/branches/main/protection": [1, "gh: Branch not protected (HTTP 404)"],
        "repos/me/site/rules/branches/main": [0, json.dumps(RULES)],
        "repos/me/site/rulesets/7": [0, json.dumps({"enforcement": "active", "current_user_can_bypass": "never"})],
    })
    monkeypatch.setenv("YGG_GH_BIN", gh)
    status, detail = commands.protection_status({"github": "me/site", "path": None})
    assert status == "unknown" and "cannot see its bypass list" in detail


def test_no_protection_and_no_ruleset_is_unprotected(tmp_path, monkeypatch):
    from yggdrasil import commands
    gh = _fake_gh(tmp_path, {
        "repos/me/site/branches/main/protection": [1, "gh: Branch not protected (HTTP 404)"],
        "repos/me/site/rules/branches/main": [0, "[]"],
    })
    monkeypatch.setenv("YGG_GH_BIN", gh)
    assert commands.protection_status({"github": "me/site", "path": None})[0] == "unprotected"


def test_an_exit_after_a_delivered_signal_is_held_until_the_user_settles_it(world):
    from yggdrasil import conductor, lifecycle, noderepair, ratatoskr, reducer, status
    from test_rebuild_t4 import SPAWNED, given
    msg = ratatoskr.send("pm1", "tl1", "note", "work")
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 0, "ambiguous": True}), messages=[msg])
    assert msg not in [i["id"] for i in reducer.owed("tl1")]  # never replayed on its own
    assert "act-0042" in status.build_status()["uncertain_keys"]  # a visible hold
    assert "tl1" not in conductor.candidates()
    noderepair.release("tl1", "aborted")  # the user: it was cut short
    assert msg in [i["id"] for i in reducer.owed("tl1")]
    assert "act-0042" not in status.build_status()["uncertain_keys"]
    assert [e["outcome"] for e in lifecycle.facts("alpha", "act-0042") if e["phase"] == "resolved"] == ["aborted"]


def test_settling_an_ambiguous_exit_as_failed_keeps_its_work_consumed(world):
    from yggdrasil import noderepair, ratatoskr, reducer
    from test_rebuild_t4 import SPAWNED, given
    msg = ratatoskr.send("pm1", "tl1", "note", "work")
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 0, "ambiguous": True}), messages=[msg])
    noderepair.release("tl1", "failed")
    assert msg not in [i["id"] for i in reducer.owed("tl1")]


def test_an_event_given_to_a_stopped_session_is_owed_again_after_confirmation(world):
    from yggdrasil import launcher, lifecycle, reducer, state
    from test_rebuild_t4 import SPAWNED, given
    from test_round3 import receipt_file
    state.wake("tl1", "timebox-checkpoint")
    [event] = [i["id"] for i in reducer.owed("tl1")]
    given("tl1", "act-0042", SPAWNED, events=[event])
    receipt_file("act-0042", outcome="confirmed", pid=999999, consumed={"events": [event]}, lifecycle=True)
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "pending"})
    with util.locked():
        launcher.apply_outcome("tl1", "act-0042", "confirmed")  # confirmation clears nothing
    lifecycle.commit("tl1", "act-0042", "exited", exit_code=-2, interrupted=True)
    assert event in [i["id"] for i in reducer.owed("tl1")]


def test_stopping_a_running_retry_keeps_the_retry_and_its_count(world):
    from yggdrasil import lifecycle, reducer
    from test_rebuild_t4 import SPAWNED
    lifecycle.commit("tl1", "act-0042", "reserved", launcher_pid=1, launcher_started_at="x", work={}, retries_used=1)
    lifecycle.commit("tl1", "act-0042", *SPAWNED[:1], **SPAWNED[1])
    lifecycle.commit("tl1", "act-0042", "exited", exit_code=1)
    lifecycle.commit("tl1", "act-0043", "reserved", launcher_pid=1, launcher_started_at="x",
                     work={"retries": ["act-0042"]}, retries_used=2)
    lifecycle.commit("tl1", "act-0043", *SPAWNED[:1], **SPAWNED[1])
    lifecycle.commit("tl1", "act-0043", "exited", exit_code=-2, interrupted=True)  # stopped mid-retry
    retries = [(i["id"], i["retries_used"]) for i in reducer.owed("tl1") if i["kind"] == "retry"]
    assert retries == [("act-0042", 2)]


def test_the_session_cap_shows_as_a_held_launch_with_its_reason(world, monkeypatch):
    from yggdrasil import conductor, ratatoskr, reducer, status
    ratatoskr.send("pm1", "tl1", "note", "work")
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nmax_concurrent_sessions: 1\n")
    clear_caches()
    monkeypatch.setattr(conductor, "running_sessions", lambda: 1)
    view = status.build_status()
    tl1 = next(n for n in view["nodes"] if n["id"] == "tl1")
    assert tl1["state"] == "held-load" and "tl1" in view["held_launches"]
    assert "limit of 1" in tl1["held_reason"]
    assert reducer.shadow_diff(view) == {}


def test_a_held_stop_raises_one_alert_naming_the_node_and_the_release_command(world, monkeypatch):
    from yggdrasil import conductor, lifecycle, noderepair, state
    from test_rebuild_t4 import SPAWNED, given

    def held():
        return [a for a in state.alerts() if a["kind"] == "ygg-stop-held"]

    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 0, "ambiguous": True}))
    lifecycle.alert_stop_holds()
    lifecycle.alert_stop_holds()  # the same hold again: no second alert
    monkeypatch.setattr(conductor, "launch_queue", lambda queue, started=None: {})
    conductor.tick()  # the conductor's pass finds the same hold: still one alert
    alerts = held()
    assert len(alerts) == 1
    assert alerts[0]["node"] == "tl1" and alerts[0]["activation"] == "act-0042"
    assert "ygg node release tl1 --as aborted|failed" in alerts[0]["message"]
    noderepair.release("tl1", "aborted")
    lifecycle.alert_stop_holds()
    assert len(held()) == 1  # settled: nothing new
    given("tl1", "act-0043", SPAWNED, ("exited", {"exit_code": 0, "ambiguous": True}))
    conductor.tick()  # a new hold, found by the conductor alone
    assert [a["activation"] for a in held()] == ["act-0042", "act-0043"]


def test_a_real_stop_of_an_agent_that_exits_cleanly_alerts_from_the_supervisor(world, tmp_path, monkeypatch):
    from yggdrasil import launcher, ratatoskr, state, stop
    agent = tmp_path / "clean-exit-agent"
    agent.write_text("#!" + sys.executable + "\nimport signal, sys, time\n"
                     "signal.signal(signal.SIGINT, lambda *a: sys.exit(0))\ntime.sleep(30)\n")
    agent.chmod(0o755)
    monkeypatch.setenv("YGG_CLAUDE_BIN", str(agent))  # like Claude Code: exits 0 on the stop signal
    ratatoskr.send("pm1", "tl1", "note", "work to do")
    activation = launcher.launch("tl1")["activation"]
    assert stop.stop("alpha").startswith("stopped 1 session(s)")
    assert _wait(lambda: [a for a in state.alerts() if a["kind"] == "ygg-stop-held"])
    [alert] = [a for a in state.alerts() if a["kind"] == "ygg-stop-held"]
    assert alert["activation"] == activation and alert["node"] == "tl1"


def test_a_stop_then_a_full_wipe_then_a_clean_exit_is_held_through_recovery(world, tmp_path, monkeypatch):
    import shutil
    import subprocess
    from yggdrasil import lifecycle, reducer
    from test_a7 import YGG, ygg
    from test_round2 import clear_caches
    agent = tmp_path / "slow-clean-exit-agent"
    agent.write_text("#!" + sys.executable + "\nimport signal, sys, time\n"
                     "signal.signal(signal.SIGINT, lambda *a: (time.sleep(3), sys.exit(0)))\ntime.sleep(30)\n")
    agent.chmod(0o755)
    monkeypatch.setenv("YGG_CLAUDE_BIN", str(agent))  # exits 0 a few seconds after the stop signal
    sent = ygg("send", "--from", "pm1", "--to", "tl1", "--kind", "note", "--body", "work to do")
    assert sent.returncode == 0, sent.stderr
    launched = ygg("launch", "tl1")
    assert launched.returncode == 0, launched.stderr
    activation = json.loads(launched.stdout)["activation"]
    stopping = subprocess.Popen([YGG, "stop", "alpha"],
                                env=dict(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    assert _wait(lambda: "sent" in {e["state"] for e in lifecycle._attempts(activation).values()}, 15)
    shutil.rmtree(world / "state")  # the whole state root goes while the signalled agent is still running
    stopping.wait(30)
    assert _wait(lambda: any(e["phase"] == "exited" for e in lifecycle.facts("alpha", activation)), 20)
    [end] = [e for e in lifecycle.facts("alpha", activation) if e["phase"] == "exited"]
    assert end.get("exit_code") == 0 and end.get("ambiguous") is True  # classified from the safety folder
    for step in (("rehydrate",), ("resume", "alpha")):
        result = ygg(*step)
        assert result.returncode == 0, (step, result.stderr)
    view = json.loads(ygg("status", "--json").stdout)
    assert activation in view["uncertain_keys"]  # the hold survives wipe recovery ...
    [node] = [n for n in view["nodes"] if n["id"] == "tl1"]
    assert node["state"] == "blocked" and "uncertain-activation" in node["blocked_by"]
    clear_caches()
    assert not [i for i in reducer.owed("tl1") if i["kind"] == "message"]  # ... and its work is not replayed
    refused = ygg("launch", "tl1")
    assert refused.returncode == 3 and "ygg node release tl1" in refused.stderr
