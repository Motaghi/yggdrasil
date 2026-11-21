import json

import pytest

from a1_observations import alerts, entry


LANE = "ygg/alpha-p1/lane/pm1tl1"
INTEGRATION = "ygg/alpha-p1/_integration"


@pytest.mark.ac("AC-20")
def test_headless_launches_use_domain_worktrees_and_leave_home_head_unchanged(tree):
    tree.ready()
    tree.ready("beta-p1")
    before = tree.ref("HEAD")
    for node in ("pm1tl1", "pm1tl2", "pm1", "pm2tl1"):
        tree.run("launch", node)
    for node in ("pm1tl1", "pm1tl2"):
        calls = tree.launches(node)
        assert len(calls) == 1
        expected = tree.repo_a / ".ygg-worktrees" / "alpha-p1" / "lane" / node
        assert tree.same_path(calls[0]["cwd"], expected)
        assert tree.git(calls[0]["cwd"], "branch", "--show-current").stdout.strip() == "ygg/alpha-p1/lane/" + node
        assert tree.git(calls[0]["cwd"], "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    calls = tree.launches("pm1")
    assert len(calls) == 1
    assert tree.same_path(calls[0]["cwd"], tree.repo_a / ".ygg-worktrees/alpha-p1/_integration")
    assert tree.git(calls[0]["cwd"], "branch", "--show-current").stdout.strip() == INTEGRATION
    calls = tree.launches("pm2tl1")
    assert len(calls) == 1
    assert tree.beneath(calls[0]["cwd"], tree.repo_b / ".ygg-worktrees")
    assert tree.ref("HEAD") == before


@pytest.mark.ac("AC-22")
def test_example_profile_is_shown_and_supplies_headless_hub_url(tree):
    tree.ready()
    tree.example_profile()
    shown = tree.run("profile", "show").stdout
    assert "example" in shown
    assert "http://127.0.0.1:9999" in shown
    tree.run("launch", "pm1tl1")
    calls = tree.launches("pm1tl1")
    assert len(calls) == 1
    assert calls[0]["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999"
    assert tree.run("profile", "show", env={"YGG_PROFILE": None}).stdout.strip() == "none"


@pytest.mark.ac("AC-23")
def test_model_resolution_uses_default_then_programme_then_package_override(tree):
    resolved = tree.command_json("resolve-model", "pm1tl1")
    assert isinstance(resolved, dict)
    assert resolved["model"] == "claude-opus-5-5"
    assert resolved["effort"] == "high"
    assert resolved["pair_model"] == "gpt-6.1-sol"
    assert resolved["pair_effort"] == "xhigh"
    tree.change_json(tree.tracker / "initiatives/alpha/initiative.json", models={"model": "claude-sonnet-5-5"})
    assert tree.command_json("resolve-model", "pm1tl1")["model"] == "claude-sonnet-5-5"
    tree.pid(models={"model": "claude-opus-5-5"})
    tree.package()
    tree.set_header(tree.tracker / "projects/alpha-p1/issues/WP-41.md", models={"model": "claude-haiku-5-5"})
    assert tree.command_json("resolve-model", "pm1tl1w2", "--package", "WP-41")["model"] == "claude-haiku-5-5"


@pytest.mark.ac("AC-25")
def test_push_guard_allows_fast_forward_lane_but_denies_main_and_deletion(tree):
    tree.ready()
    tree.git_labels()
    tree.branch(LANE, "b2c3d4e")
    ref = "refs/heads/" + LANE
    assert tree.push_guard(ref, "b2c3d4e", ref, "a1b2c3d").returncode == 0
    result = tree.push_guard(ref, "b2c3d4e", "refs/heads/main", "a1b2c3d")
    assert result.returncode == 4
    assert "refs/heads/main" in result.stderr
    assert tree.push_guard("(delete)", "0000000", ref, "a1b2c3d").returncode == 4


def accepted_integration(tree, **pid_overrides):
    tree.ready(**pid_overrides)
    tree.git_labels()
    tree.branch(LANE, "b2c3d4e")
    tree.branch(INTEGRATION, "b2c3d4e")
    tree.package(criteria=True, authorise=True)
    tree.run("wp", "complete", "--from", "pm1tl1", "--package", "WP-41")
    tree.complete_package_review()
    tree.run("review", "record", "--from", "pm1tl1", "--package", "WP-41", "--round", "1", "--verdict", "pass")
    tree.run("acceptance", "record", "--from", "pm1su1", "--package", "WP-41", "--sha", tree.sha("b2c3d4e"), "--verdict", "accept")

    tree.run("merge", "lane", "alpha-p1", "--lane", "pm1tl1", "--repo", tree.repo_a)
    tree.run("merge", "check", "alpha-p1", "--repo", tree.repo_a)

@pytest.mark.ac("AC-26")
def test_default_main_merge_prepares_command_and_preserves_main(tree):
    accepted_integration(tree)
    result = tree.run("merge", "main", "alpha-p1", "--repo", tree.repo_a, "--print")
    assert result.returncode == 0
    assert INTEGRATION in result.stdout
    assert "merge" in result.stdout.lower() or "pr" in result.stdout.lower()
    assert tree.symbolic(tree.ref("main")) == "a1b2c3d"


@pytest.mark.ac("AC-26")
def test_local_main_merge_contains_accepted_integration_commit(tree):
    accepted_integration(tree, merge_mode="local")
    result = tree.run("merge", "main", "alpha-p1", "--repo", tree.repo_a)
    assert result.returncode == 0
    branches = tree.git(tree.repo_a, "branch", "--contains", tree.sha("b2c3d4e"), "--format=%(refname:short)").stdout.splitlines()
    assert "main" in branches


@pytest.mark.ac("AC-26")
def test_lane_merge_refuses_acceptance_for_a_different_sha(tree):
    tree.ready()
    tree.git_labels()
    tree.branch(LANE, "b2c3d4e")
    tree.package(criteria=True, authorise=True)
    tree.run("wp", "complete", "--from", "pm1tl1", "--package", "WP-41")
    tree.run("acceptance", "record", "--from", "pm1su1", "--package", "WP-41", "--sha", tree.sha("9f8e7d6"), "--verdict", "accept")
    before = tree.ref(INTEGRATION)
    result = tree.run("merge", "lane", "alpha-p1", "--lane", "pm1tl1", "--repo", tree.repo_a)
    assert result.returncode == 3
    assert "REFUSED: no acceptance for b2c3d4e" in tree.symbolic(result.stderr)
    assert tree.ref(INTEGRATION) == before


@pytest.mark.ac("AC-32")
def test_pause_switches_block_global_and_programme_launches(tree):
    tree.ready()
    tree.ready("beta-p1")
    tree.write(tree.platform / "pause", "")
    assert tree.run("launch", "pm1tl1").returncode == 64
    assert not tree.launches()
    tree.remove(tree.platform / "pause")
    tree.run("pause", "alpha")
    assert tree.run("launch", "pm1tl1").returncode == 64
    assert not tree.launches("pm1tl1")
    assert tree.run("launch", "pm2tl1").returncode == 0
    assert len(tree.launches("pm2tl1")) == 1
    tree.wait_exit("pm2tl1")
    tree.clear_calls()
    assert tree.run("launch", "pm2tl1", env={"CLAUDE_PLATFORM_ENABLED": "0"}).returncode == 64
    assert not tree.launches()


@pytest.mark.ac("AC-45")
@pytest.mark.parametrize("local,local_label,remote,remote_label,reason", [
    ("refs/heads/" + LANE, "c3d4e5f", "refs/heads/" + LANE, "b2c3d4e", "non-fast-forward"),
    ("refs/heads/ygg/beta-p1/lane/pm2tl1", "d4e5f6a", "refs/heads/ygg/beta-p1/lane/pm2tl1", "0000000", "beta-p1"),
    ("refs/heads/" + LANE, "e5f6a7b", "refs/heads/" + LANE, "b2c3d4e", "workflow-change"),
], ids=["non-fast-forward", "other-project", "workflow-change"])
def test_push_guard_denies_disallowed_ranges(tree, local, local_label, remote, remote_label, reason):
    tree.ready()
    tree.git_labels()
    tree.branch(local.removeprefix("refs/heads/"), local_label)
    result = tree.push_guard(local, local_label, remote, remote_label)
    assert result.returncode == 4
    assert reason in result.stderr


@pytest.mark.ac("AC-46")
def test_headless_launch_injects_pretool_settings_and_worktree_hooks(tree):
    tree.ready()
    tree.run("launch", "pm1tl1")
    calls = tree.launches("pm1tl1")
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert "--settings" in argv
    settings = tree.json_file(argv[argv.index("--settings") + 1])
    assert isinstance(settings, dict)
    assert "ygg hook pretool" in json.dumps(settings["hooks"]["PreToolUse"])
    hooks_path = tree.git(calls[0]["cwd"], "config", "--worktree", "--get", "core.hooksPath").stdout.strip()
    assert tree.same_path(hooks_path, tree.root / "state.safety" / "hooks")


@pytest.mark.ac("AC-46")
def test_hook_injection_write_failure_prevents_launch(tree):
    tree.ready()
    tree.readonly(tree.state / "launch")
    result = tree.run("launch", "pm1tl1")
    assert result.returncode == 3
    assert "hook" in result.stderr.lower()
    assert "inject" in result.stderr.lower()
    assert not tree.launches()


@pytest.mark.ac("AC-46")
def test_managed_session_cannot_create_raw_threads(tree):
    result = tree.hook({"tool_name": "mcp__t3-code__create_threads", "tool_input": {}}, node="pm1")
    assert result.returncode == 2


@pytest.mark.ac("AC-48")
def test_envelope_snapshots_and_morning_go_preserve_symbolic_commit_range(tree):
    tree.ready()
    tree.git_labels()
    tree.branch(LANE, "b2c3d4e")
    tree.active_envelope()
    envelope = entry(tree.status(), "projects", "alpha-p1")["envelope"]
    assert envelope["until"] == "2026-11-16T07:00:00Z"
    snapshot = {item["branch"]: tree.symbolic(item["sha"]) for item in envelope["ref_snapshot"]}
    assert snapshot[LANE] == "b2c3d4e"
    assert snapshot[INTEGRATION] == "a1b2c3d"
    tree.advance_label("c3d4e5f", "b2c3d4e")
    tree.branch(LANE, "c3d4e5f")
    tree.report(1)
    tree.run("stage", "end", "alpha-p1", "--within-tolerance", "yes", "--report", "end-stage-1.md")
    tree.run("node", "add", "rv1", "--role", "reviewer", "--parent", "alpha")
    tree.at("2026-11-16T09:00:00+01:00")
    tree.run("morning-go", "build", "--programme", "alpha", "--reviewer-node", "rv1")
    items = [item for item in tree.json_records("initiatives/alpha/morning-go") if item["kind"] == "stage-end"]
    assert any(tree.symbolic(commit["range"]) == "b2c3d4e..c3d4e5f" for item in items for commit in item["commit_ranges"])


@pytest.mark.ac("AC-48")
def test_envelope_without_local_merge_grant_refuses_main_merge(tree):
    accepted_integration(tree, merge_mode="local")
    tree.active_envelope()
    before = tree.ref("main")
    result = tree.run("merge", "main", "alpha-p1", "--repo", tree.repo_a)
    assert result.returncode == 0
    assert tree.ref("main") != before
    assert tree.git(tree.repo_a, "rev-parse", "main^2").stdout.strip() == tree.ref(INTEGRATION)


@pytest.mark.ac("AC-57")
def test_unprotected_main_refuses_pid_and_warn_mode_allows_with_alert(tree):
    tree.fake_behavior(gh={"protected": {}})
    result = tree.run("pid", "approve", "alpha-p1")
    assert result.returncode == 3
    assert str(tree.repo_a) in result.stderr
    assert "main" in result.stderr
    pid = tree.tracker_text("projects/alpha-p1/documents/pid.md")
    from a1_observations import records
    assert not any(record["kind"] == "pid-approval" for record in records(pid))
    tree.pid(branch_protection="warn")
    before = len(alerts(tree, "ygg-protection-missing"))
    assert tree.run("pid", "approve", "alpha-p1").returncode == 0
    assert len(alerts(tree, "ygg-protection-missing")) - before == 1


@pytest.mark.ac("AC-57")
def test_protected_main_allows_pid_without_protection_alert(tree):
    result = tree.run("pid", "approve", "alpha-p1")
    assert result.returncode == 0
    assert not alerts(tree, "ygg-protection-missing")
