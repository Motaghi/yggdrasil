"""A launch whose agent runs with a clean Claude settings folder: through the real CLI and the frozen fake claude.

The agent needs no trust of the repository: what it may do comes only from Yggdrasil's launch settings, and only
after the user agreed. (Claude Code's own matching of the rules is checked against the real binary in
test_agent_permissions_real_claude.py.)
"""

import json
import os

import pytest
from driver import Driver

pytestmark = pytest.mark.skipif(os.environ.get("ACCEPTANCE_TARGET", "real") != "real", reason="real target only")


def _profile(driver, folder, agreed):
    path = driver.root / "profile.yaml"
    driver.write(path, "schema: ygg.config.v1\nname: clean\nagent_claude_config_dir: " + str(folder) + "\n")
    driver.env["YGG_PROFILE"] = str(path)
    # The frozen core tree records the minimal agreement (A11 amendment 3); these rows start without one.
    driver.remove(driver.tracker / "_ygg" / "agents-agreement.json")
    if agreed:  # the user agrees through the user-only verb, from their own shell
        result = driver.run("agents", "allow-minimal")
        assert result.returncode == 0, result.stderr


def test_a_clean_agent_folder_launch_is_refused_until_the_user_agrees(tmp_path):
    with Driver(tmp_path) as driver:
        driver.core_tree()
        driver.ready()
        folder = driver.root / "agent-claude"
        folder.mkdir()
        _profile(driver, folder, agreed=False)
        result = driver.run("launch", "pm1tl1")
        assert result.returncode == 3
        assert "ygg agents allow-minimal" in result.stderr
        assert driver.launches("pm1tl1") == []
        # An agent can't agree for the user: not with its markers set, and not from inside its worktree.
        managed = driver.run("agents", "allow-minimal", env={"YGG_NODE": "pm1tl1"})
        assert managed.returncode == 3 and "user-only" in managed.stderr
        assert driver.run("launch", "pm1tl1").returncode == 3


def test_an_agreed_launch_runs_its_own_ygg_with_only_yggdrasil_s_rules(tmp_path):
    with Driver(tmp_path) as driver:
        driver.core_tree()
        driver.ready()
        folder = driver.root / "agent-claude"
        folder.mkdir()
        _profile(driver, folder, agreed=True)
        driver.fake_behavior(claude={"agents": {"pm1tl1": {"mode": "script", "steps": [
            {"id": "inbox", "run": ["ygg", "inbox", "{node}"]}]}}})
        result = driver.run("launch", "pm1tl1")
        assert result.returncode == 0, result.stderr
        driver.wait_exit("pm1tl1", timeout=30)
        launch = driver.launches("pm1tl1")[0]
        assert launch["env"]["CLAUDE_CONFIG_DIR"] == str(folder)
        assert sorted(os.listdir(folder)) == []  # nothing trusts the repository or grants anything there
        argv = launch["argv"]
        settings = json.loads(driver.read(argv[argv.index("--settings") + 1]))
        worktree = os.path.realpath(launch["cwd"])
        project = os.path.realpath(driver.tracker / "projects" / "alpha-p1")
        assert settings["permissions"] == {"defaultMode": "dontAsk", "allow": [
            "Bash(ygg *)", "Read(/" + project + "/**)", "Edit(/" + worktree + "/**)",
            "Bash(git add *)", "Bash(git commit *)"]}
        steps = [call for call in driver.calls("claude") if call.get("event") == "step"]
        assert [(s["id"], s["exit_code"]) for s in steps] == [("inbox", 0)]


def test_a_standard_launch_shows_first_then_leaves_out_the_projects_settings(tmp_path):
    with Driver(tmp_path) as driver:
        driver.core_tree()
        driver.ready()
        folder = driver.root / "agent-claude"
        folder.mkdir()
        _profile(driver, folder, agreed=False)
        shown = driver.run("agents", "allow-standard")
        assert shown.returncode == 0 and "Bash(git status)" in shown.stdout and "--yes" in shown.stdout
        assert driver.run("launch", "pm1tl1").returncode == 3  # showing the rules agrees to nothing
        assert driver.run("agents", "allow-standard", "--yes").returncode == 0
        driver.fake_behavior(claude={"agents": {"pm1tl1": {"mode": "script", "steps": [
            {"id": "inbox", "run": ["ygg", "inbox", "{node}"]}]}}})
        result = driver.run("launch", "pm1tl1")
        assert result.returncode == 0, result.stderr
        driver.wait_exit("pm1tl1", timeout=30)
        launch = driver.launches("pm1tl1")[0]
        argv = launch["argv"]
        assert argv[argv.index("--setting-sources") + 1] == "user"
        assert os.path.realpath(argv[argv.index("--add-dir") + 1]) == os.path.realpath(launch["cwd"])
        assert launch["env"]["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] == "1"
        settings = json.loads(driver.read(argv[argv.index("--settings") + 1]))
        assert "Bash(git status)" in settings["permissions"]["allow"]
        assert "Bash(git push origin ygg/alpha-p1/lane/pm1tl1)" in settings["permissions"]["allow"]
        steps = [call for call in driver.calls("claude") if call.get("event") == "step"]
        assert [(s["id"], s["exit_code"]) for s in steps] == [("inbox", 0)]
