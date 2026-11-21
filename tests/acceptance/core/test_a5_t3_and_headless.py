import json

import pytest

from a1_observations import entry


def argument(call, name):
    argv = call["argv"]
    assert name in argv
    return argv[argv.index(name) + 1]


def nested_values(value):
    if isinstance(value, dict):
        return [item for child in value.values() for item in nested_values(child)]
    if isinstance(value, list):
        return [item for child in value for item in nested_values(child)]
    return [value]


def current_activation(driver):
    node = entry(driver.status(), "nodes", "pm1tl1")
    assert isinstance(node["activation"], dict)
    return node["activation"]


def assert_uncertain_send(driver):
    sends = driver.a5_t3_calls("thread send")
    assert len(sends) == 1
    assert "th-4" in sends[0]["argv"]
    activation = current_activation(driver)
    assert activation["outcome"] == "uncertain"
    assert any(activation["id"] in nested_values(line) and "uncertain" in nested_values(line)
               for line in driver.state_lines("sends.jsonl"))
    assert sends[0]["argv"][-1].splitlines()[0] == "[ygg " + activation["id"] + " m-0010]"
    return activation["id"]


@pytest.mark.ac("AC-18")
def test_unknown_t3_send_is_recorded_uncertain_once(quiet_tree):
    quiet_tree.a5_live_thread()
    quiet_tree.run("conductor", "tick")
    assert_uncertain_send(quiet_tree)


@pytest.mark.ac("AC-18")
def test_delivered_t3_marker_confirms_send_without_resend_or_headless(quiet_tree):
    quiet_tree.a5_live_thread()
    quiet_tree.run("conductor", "tick")
    activation = assert_uncertain_send(quiet_tree)
    quiet_tree.run("conductor", "tick")
    observed = current_activation(quiet_tree)
    assert observed["id"] == activation
    assert observed["outcome"] == "confirmed"
    assert len(quiet_tree.a5_t3_calls("thread send")) == 1
    assert quiet_tree.launches("pm1tl1") == []


@pytest.mark.ac("AC-18")
def test_ygg_send_never_calls_t3_with_unknown_send_control(quiet_tree):
    quiet_tree.a5_t3()
    quiet_tree.clear_calls()
    result = quiet_tree.run("send", "--from", "pm1", "--to", "pm1tl1",
                            "--kind", "work", "--body", "start WP-3")
    assert result.returncode == 0
    assert quiet_tree.calls("t3") == []
    # Row 1 supplies the nonempty control required by BRIEF.md for row 3.
    quiet_tree.a5_live_thread()
    quiet_tree.run("conductor", "tick")
    assert_uncertain_send(quiet_tree)


@pytest.mark.ac("AC-19")
def test_matching_supported_t3_versions_start_thread_without_claude(tree):
    tree.ready()
    tree.a5_version_lists(["0.1.9"], ["0.1.9"])
    tree.a5_t3(cli_version="0.1.9", server_version="0.1.9")
    tree.clear_calls()
    tree.run("launch", "pm1tl1")
    assert tree.a5_t3_calls("thread start")
    assert tree.calls("claude") == []


@pytest.mark.ac("AC-19")
def test_unsupported_t3_server_uses_headless_and_logs_fallback(tree):
    tree.ready()
    tree.a5_version_lists(["0.1.9"], ["0.1.9"])
    tree.a5_t3(cli_version="0.1.9", server_version="0.3.0")
    tree.clear_calls()
    result = tree.run("launch", "pm1tl1")
    assert len(tree.launches("pm1tl1")) == 1
    assert tree.launches("pm1tl1")[0]["binary"] == "claude"
    assert tree.a5_t3_calls("thread start") == []
    assert tree.a5_logged_fallbacks(result)


@pytest.mark.ac("AC-19")
def test_missing_t3_binary_uses_one_headless_claude_launch(tree):
    tree.ready()
    tree.clear_calls()
    tree.run("launch", "pm1tl1")
    launches = tree.launches("pm1tl1")
    assert len(launches) == 1
    assert launches[0]["binary"] == "claude"


@pytest.mark.ac("AC-21")
def test_headless_without_profile_records_default_model_effort_and_no_hub(tree):
    tree.ready()
    tree.env.pop("YGG_PROFILE", None)
    tree.env.pop("ANTHROPIC_BASE_URL", None)
    tree.clear_calls()
    tree.run("launch", "pm1tl1")
    launches = tree.launches("pm1tl1")
    assert len(launches) == 1
    launch = launches[0]
    assert launch["binary"] == "claude"
    assert argument(launch, "--model") == "claude-opus-5-5"
    assert argument(launch, "--effort") == "high"
    assert "ANTHROPIC_BASE_URL" not in launch["env"]
    assert not any("pool" in key.casefold()
                   or any(name in value for name in ("pool-live-a", "pool-bg-a", "pool-bg-b"))
                   for key, value in launch["env"].items())


@pytest.mark.ac("AC-21")
def test_model_resolution_without_profile_returns_json_without_pool(tree):
    tree.env.pop("YGG_PROFILE", None)
    resolved = tree.command_json("resolve-model", "pm1tl1")
    assert isinstance(resolved, dict)
    assert resolved.get("pool") is None


@pytest.mark.ac("AC-61")
def test_headless_activation_receipt_names_node_launcher_and_activation(tree):
    tree.ready()
    tree.run("launch", "pm1tl1")
    receipts = tree.a5_receipts("pm1tl1")
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["node"] == "pm1tl1"
    assert receipt["launcher"] == "headless"
    assert receipt["activation"]
    assert tree.exists(tree.state / "activations" / (receipt["activation"] + ".json"))


@pytest.mark.ac("AC-61")
def test_unknown_t3_start_stays_uncertain_without_headless_fallback(quiet_tree):
    quiet_tree.a5_uncertain_start()
    activation = current_activation(quiet_tree)
    assert activation["outcome"] == "uncertain"
    quiet_tree.clear_calls()
    quiet_tree.run("conductor", "tick")
    assert quiet_tree.launches("pm1tl1") == []


@pytest.mark.ac("AC-61")
def test_absent_t3_thread_restarts_once_with_same_activation(quiet_tree):
    quiet_tree.a5_uncertain_start()
    activation = current_activation(quiet_tree)
    assert activation["outcome"] == "uncertain"
    quiet_tree.a5_t3_behavior(thread_start={"outcome": "ok"})
    quiet_tree.clear_calls()
    quiet_tree.run("conductor", "tick")
    starts = quiet_tree.a5_t3_calls("thread start", "pm1tl1")
    assert len(starts) == 1
    assert argument(starts[0], "--title") == "pm1tl1 " + activation["id"]


@pytest.mark.ac("AC-88")
def test_supported_but_mismatched_t3_versions_use_headless_and_log_both(tree):
    tree.ready()
    tree.a5_version_lists(["0.1.5", "0.1.9"], ["0.1.5", "0.1.9"])
    tree.a5_t3(cli_version="0.1.5", server_version="0.1.9")
    tree.clear_calls()
    result = tree.run("launch", "pm1tl1")
    launches = tree.launches("pm1tl1")
    assert len(launches) == 1
    assert launches[0]["binary"] == "claude"
    assert tree.a5_t3_calls("thread start") == []
    assert any("0.1.5" in line and "0.1.9" in line for line in tree.a5_logged_fallbacks(result))


@pytest.mark.ac("AC-89")
def test_t3_uses_registered_lane_worktree_with_project_pretool_hook(tree):
    tree.ready()
    tree.a5_t3(cli_version="0.1.9", server_version="0.1.9")
    tree.clear_calls()
    tree.run("launch", "pm1tl1")
    worktree = tree.repo_a / ".ygg-worktrees" / "alpha-p1" / "lane" / "pm1tl1"
    projects = tree.a5_t3_calls("project add")
    assert any(str(worktree) in call["argv"] for call in projects)
    starts = tree.a5_t3_calls("thread start")
    assert any(str(worktree) in call["argv"] and "--worktree" not in call["argv"] for call in starts)
    settings = json.loads(tree.read(worktree / ".claude" / "settings.json"))
    hooks = settings.get("hooks", {}).get("PreToolUse", [])
    assert any("ygg hook pretool" in hook.get("command", "")
               for matcher in hooks for hook in matcher.get("hooks", []))
