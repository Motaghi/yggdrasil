"""Review perms-merge-r1: agent permissions on the T3 route. Consent is checked before routing, on every Claude
route, and the agreed rules are written into the settings file T3 loads. From the reviewer's probes."""

import json
import os
from pathlib import Path

import pytest
from driver import Driver

pytestmark = pytest.mark.skipif(os.environ.get("ACCEPTANCE_TARGET", "real") != "real", reason="real target only")


def _fixture(tmp_path, cli="0.1.9", server="0.1.9"):
    driver = Driver(tmp_path)
    driver.core_tree()
    assert driver.ready().returncode == 0
    folder = driver.root / "agent-claude"
    folder.mkdir()
    driver.example_profile(agent_claude_config_dir=str(folder))
    driver.a5_t3(cli_version=cli, server_version=server)
    # The frozen core tree records the minimal agreement (A11 amendment 3); these rows start without one.
    driver.remove(driver.tracker / "_ygg" / "agents-agreement.json")
    return driver


@pytest.mark.parametrize("agreement", ["absent", "revoked"])
def test_a_t3_launch_without_a_valid_agreement_is_refused_before_anything_starts(tmp_path, agreement):
    with _fixture(tmp_path) as driver:
        if agreement == "revoked":
            assert driver.run("agents", "allow-standard", "--yes").returncode == 0
            assert driver.run("agents", "revoke").returncode == 0
        result = driver.run("launch", "pm1tl1")
        assert result.returncode == 3 and "ygg agents allow-standard" in result.stderr
        assert driver.a5_t3_calls("thread start", "pm1tl1") == []


@pytest.mark.parametrize("profile", ["minimal", "standard"])
def test_a_t3_session_gets_the_agreed_rules_in_the_settings_file_t3_loads(tmp_path, profile):
    with _fixture(tmp_path) as driver:
        command = ["agents", "allow-" + profile] + (["--yes"] if profile == "standard" else [])
        assert driver.run(*command).returncode == 0
        result = driver.run("launch", "pm1tl1")
        assert result.returncode == 0, result.stderr
        assert len(driver.a5_t3_calls("thread start", "pm1tl1")) == 1
        receipt = driver.a5_receipts("pm1tl1")[-1]
        t3_settings = json.loads(driver.read(Path(receipt["cwd"]) / ".claude" / "settings.json"))
        rules = t3_settings["permissions"]
        assert rules["defaultMode"] == "dontAsk" and "Bash(ygg *)" in rules["allow"]
        assert ("deny" in rules) == (profile == "standard")
        assert "Bash(git status)" in rules["allow"] if profile == "standard" else \
            "Bash(git status)" not in rules["allow"]
        assert "ygg" in json.dumps(t3_settings["hooks"]["PreToolUse"])  # the guard stays
        # T3 tells the agent to read its private prompt file first: exactly that file is readable.
        prompt = os.path.realpath(str(driver.state / "launch" / receipt["activation"] / "prompt.md"))
        assert "Read(/" + prompt + ")" in rules["allow"]
        assert not [r for r in rules["allow"] if r.startswith("Read(") and "/launch/" in r and r.endswith("**)")]


@pytest.mark.parametrize("cli,server", [("0.1.9", "0.1.9"), ("9.9.9", "9.9.9")])
def test_owed_work_without_an_agreement_shows_the_permissions_hold_on_either_route(tmp_path, cli, server):
    with _fixture(tmp_path, cli, server) as driver:
        assert driver.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "w").returncode == 0
        node = next(n for n in driver.status()["nodes"] if n["id"] == "pm1tl1")
        assert node["state"] == "blocked" and "agent-permissions" in node["blocked_by"], node
        assert "ygg agents allow-standard" in node["held_reason"]


def test_a_rule_that_cannot_be_written_shows_a_hold_until_the_path_is_plain(tmp_path):
    """Review perms-merge-r2: a state root holding a pattern character can't form the T3 prompt-file rule."""
    with Driver(tmp_path) as driver:
        driver.state = tmp_path / "state[review]"
        driver.state.mkdir()
        driver.env["YGG_STATE_ROOT"] = str(driver.state)
        driver.core_tree()
        assert driver.ready().returncode == 0
        folder = driver.root / "agent-claude"
        folder.mkdir()
        driver.example_profile(agent_claude_config_dir=str(folder))
        driver.a5_t3(cli_version="0.1.9", server_version="0.1.9")
        assert driver.run("agents", "allow-standard", "--yes").returncode == 0
        assert driver.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "w").returncode == 0
        launched = driver.run("launch", "pm1tl1")
        assert launched.returncode == 3 and "cannot be written as a permission rule" in launched.stderr
        assert driver.run("conductor", "tick").returncode == 0
        assert driver.a5_t3_calls("thread start", "pm1tl1") == []
        node = next(n for n in driver.status()["nodes"] if n["id"] == "pm1tl1")
        assert node["state"] == "blocked" and "agent-permissions" in node["blocked_by"], node
        assert "cannot be written as a permission rule" in node["held_reason"]
