"""The permissions a Claude Code agent's launch settings grant, and the user's agreement they need first."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)

from yggdrasil import util

# The permission-rule pattern of the Claude Code settings schema (json.schemastore.org/claude-code-settings.json,
# `$defs.permissionRule`, fetched 2026-11-18 for Claude Code 2.1.322), so every generated rule is one it accepts.
RULE = re.compile(r"^((Agent|Artifact|Bash|Cd|Edit|EnterWorktree|ExitPlanMode|Glob|Grep|KillShell|LSP|Monitor|"
                  r"MultiEdit|NotebookEdit|PowerShell|Read|ShareOnboardingGuide|Skill|TaskCreate|TaskGet|TaskList|"
                  r"TaskOutput|TaskStop|TaskUpdate|TodoWrite|ToolSearch|WebFetch|WebSearch|Workflow|Write)"
                  r"(\([^)]+\))?|mcp__.*)$")
COMMAND = "ygg agents allow-minimal"


def _config(world, text):
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nsettle_delay_seconds: 0\n" + text)
    clear_caches()


def _agree():
    from yggdrasil import permissions
    output = permissions.allow_minimal()
    clear_caches()
    return output


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def lane(world):
    """A git-repo programme beta with project q1: PM pm2, team lead tl2, its worker w2, senior user su2."""
    from yggdrasil import commands, state
    home = world / "beta-home"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    _git(home, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "root")
    commands.programme_create("beta", "beta", str(home), "git-repo")
    commands.project_create("q1", "beta", "pm2")
    commands.node_add("tl2", "tl", "pm2")
    commands.node_add("w2", "worker", "tl2")
    commands.node_add("su2", "su", "pm2", "tl2")
    return state.all_nodes()


def _abs(path):
    return "/" + os.path.realpath(str(path))


# ---------------------------------------------------------------- the rules, exactly

def test_an_editing_node_gets_exactly_its_ygg_tracker_worktree_and_commit_rules(world, lane):
    from yggdrasil import permissions, tracker
    _agree()
    worktree = world / "beta-home" / ".ygg-worktrees" / "q1" / "lane" / "tl2"
    worktree.mkdir(parents=True)
    project = _abs(tracker.project_dir("q1"))
    builder = {"defaultMode": "dontAsk", "allow": [
        "Bash(ygg *)", "Read(" + project + "/**)", "Edit(" + _abs(worktree) + "/**)",
        "Bash(git add *)", "Bash(git commit *)"]}
    for node in ("tl2", "w2"):
        assert permissions.block(lane[node], lane, worktree, is_git=True) == builder, node
    # A project manager edits its worktree and commits nothing (rule sets, version 1).
    assert permissions.block(lane["pm2"], lane, worktree, is_git=True) == {"defaultMode": "dontAsk", "allow": [
        "Bash(ygg *)", "Read(" + project + "/**)", "Edit(" + _abs(worktree) + "/**)"]}
    assert all(RULE.match(rule) for rule in builder["allow"])


def test_a_senior_user_reads_and_runs_ygg_but_edits_nothing(world, lane):
    from yggdrasil import permissions, tracker
    _agree()
    checkout = world / "beta-home" / ".ygg-worktrees" / "q1" / "_test-su2"
    checkout.mkdir(parents=True)
    assert permissions.block(lane["su2"], lane, checkout, is_git=True) == {"defaultMode": "dontAsk", "allow": [
        "Bash(ygg *)", "Read(" + _abs(tracker.project_dir("q1")) + "/**)"]}


def test_a_node_outside_git_gets_no_commit_rules(world):
    from yggdrasil import permissions, state, tracker
    _agree()
    nodes = state.all_nodes()
    staging = world / "home" / ".ygg-staging" / "p1" / "tl1"
    staging.mkdir(parents=True)
    assert permissions.block(nodes["tl1"], nodes, staging, is_git=False) == {"defaultMode": "dontAsk", "allow": [
        "Bash(ygg *)", "Read(" + _abs(tracker.project_dir("p1")) + "/**)", "Edit(" + _abs(staging) + "/**)"]}


def test_a_programme_reviewer_reads_every_project_of_its_programme(world, lane):
    from yggdrasil import commands, permissions, state, tracker
    _agree()
    commands.project_create("q2", "beta", "pm3")
    commands.node_add("rv2", "reviewer", "beta")
    nodes = state.all_nodes()
    block = permissions.block(nodes["rv2"], nodes, world, is_git=False)
    assert block["allow"] == ["Bash(ygg *)", "Read(" + _abs(tracker.project_dir("q1")) + "/**)",
                              "Read(" + _abs(tracker.project_dir("q2")) + "/**)", "Bash(git diff *)",
                              "Bash(git show *)"]


def test_a_reviewer_writes_only_its_review_folder(world, lane):
    from yggdrasil import commands, permissions, state
    _agree()
    commands.node_add("rv2", "reviewer", "beta")
    nodes = state.all_nodes()
    block = permissions.block(nodes["rv2"], nodes, world, is_git=True, activation="act-0042")
    folder = state.root() / "scratch" / "act-0042"
    assert folder.is_dir()
    assert [r for r in block["allow"] if r.startswith("Edit(")] == ["Edit(" + _abs(folder) + "/**)"]


def test_a_path_that_reads_as_a_pattern_is_refused(world, lane):
    from yggdrasil import permissions
    _agree()
    odd = world / "work[1]"
    odd.mkdir()
    with pytest.raises(util.YggError) as error:
        permissions.block(lane["tl2"], lane, odd, is_git=True)
    assert "permission rule" in error.value.message


# ---------------------------------------------------------------- the agreement

def test_without_the_agreement_nothing_is_granted(world, lane):
    from yggdrasil import permissions
    assert permissions.block(lane["tl2"], lane, world, is_git=True) is None
    _agree()
    assert permissions.block(lane["tl2"], lane, world, is_git=True) is not None
    permissions.revoke_minimal()
    clear_caches()
    assert permissions.block(lane["tl2"], lane, world, is_git=True) is None


def test_the_verb_records_the_agreement_in_the_files_root(world, lane):
    from yggdrasil import permissions, tracker
    output = _agree()
    path = Path(tracker.root()) / "_ygg" / "agents-agreement.json"
    assert permissions.agreement_path() == path
    record = json.loads(path.read_text())
    assert {k: record[k] for k in ("profile", "rules_version", "origin")} == \
        {"profile": "minimal", "rules_version": 1, "origin": "user"}
    assert record["programmes"] == sorted(p["id"] for p in tracker.programmes())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "read" in output and "ygg" in output and "worktree" in output and "ygg agents revoke" in output


def test_consent_covers_only_the_programmes_it_names(world, lane):
    from yggdrasil import commands, permissions, state
    _config(world, "agent_claude_config_dir: " + str(world / "agent-claude") + "\n")
    permissions.allow_standard(yes=True)
    home = world / "gamma-home"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    commands.programme_create("gamma", "gamma", str(home), "git-repo")
    commands.project_create("g1", "gamma", "pm9")
    nodes = state.all_nodes()
    assert permissions.granted("beta") and not permissions.granted("gamma")
    assert permissions.block(nodes["pm9"], nodes, world, is_git=True) is None
    with pytest.raises(util.YggError) as error:
        permissions.check("claude-code", nodes["pm9"])
    assert "--programme gamma" in error.value.message
    permissions.allow_standard(yes=True, programme="gamma")
    assert permissions.granted("beta") and permissions.granted("gamma")


def test_an_older_rule_set_version_asks_to_agree_again(world, lane):
    from yggdrasil import permissions
    _config(world, "agent_claude_config_dir: " + str(world / "agent-claude") + "\n")
    permissions.allow_standard(yes=True)
    path = permissions.agreement_path()
    record = json.loads(path.read_text())
    path.write_text(json.dumps(dict(record, rules_version=0)))
    with pytest.raises(util.YggError) as error:
        permissions.check("claude-code", lane["tl2"])
    assert "agree again" in error.value.message


def test_an_agreement_through_a_symbolic_link_never_counts(world, lane, tmp_path):
    from yggdrasil import permissions
    _agree()
    path = permissions.agreement_path()
    elsewhere = tmp_path / "planted.json"
    elsewhere.write_text(path.read_text())
    path.unlink()
    path.symlink_to(elsewhere)
    assert not permissions.granted()
    with pytest.raises(util.YggError):
        _agree()


@pytest.mark.parametrize("marker", ["YGG_NODE", "YGG_ACTIVATION"])
def test_the_verb_is_refused_in_a_managed_session(world, monkeypatch, marker, capsys):
    from yggdrasil import cli, permissions
    monkeypatch.setenv(marker, "tl1")
    for verb in ("allow-minimal", "revoke-minimal"):
        assert cli.main(["agents", verb, "--yes"] if verb == "allow-minimal" else ["agents", verb]) == \
            util.EXIT_REFUSED
        assert "user-only" in capsys.readouterr().err
    monkeypatch.delenv(marker)
    clear_caches()
    assert not permissions.agreement_path().exists()


@pytest.mark.parametrize("folder", [".ygg-worktrees/q1/lane/tl2/src", ".ygg-staging/p1/tl1"])
def test_the_verb_is_refused_inside_a_managed_worktree(world, monkeypatch, folder, capsys):
    from yggdrasil import cli, permissions
    inside = world / "beta-home" / folder
    inside.mkdir(parents=True)
    monkeypatch.chdir(inside)
    assert cli.main(["agents", "allow-minimal"]) == util.EXIT_REFUSED
    assert "user-only" in capsys.readouterr().err
    assert not permissions.agreement_path().exists()


def test_the_verb_is_refused_inside_a_scratch_folder(world, monkeypatch, capsys):
    from yggdrasil import cli, state
    scratch = state.root() / "scratch" / "act-0001"
    scratch.mkdir(parents=True)
    monkeypatch.chdir(scratch)
    assert cli.main(["agents", "allow-minimal"]) == util.EXIT_REFUSED
    assert "user-only" in capsys.readouterr().err


# ---------------------------------------------------------------- launches

def test_a_launch_with_its_own_claude_folder_is_refused_until_the_user_agrees(world):
    from yggdrasil import launcher, state
    _config(world, "agent_claude_config_dir: " + str(world / "agent-claude") + "\n")
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1")
    assert error.value.code == util.EXIT_REFUSED
    assert COMMAND in error.value.message
    assert state.runtime("tl1").get("activation") is None  # refused before anything was reserved
    assert not (state.root() / "activations").exists() or not list((state.root() / "activations").iterdir())


def test_an_agreed_launch_writes_the_rules_into_its_settings(world):
    from yggdrasil import launcher, state
    _config(world, "agent_claude_config_dir: " + str(world / "agent-claude") + "\n")
    _agree()
    launcher.launch("tl1")
    data = launcher.receipt(state.runtime("tl1")["activation"])
    settings = json.loads(Path(data["settings"]).read_text())
    assert settings["permissions"]["defaultMode"] == "dontAsk"
    assert "Bash(ygg *)" in settings["permissions"]["allow"]
    assert "ygg" in json.dumps(settings["hooks"]["PreToolUse"])  # the guard hook stays


def test_a_launch_using_the_users_own_claude_setup_is_unchanged(world):
    from yggdrasil import launcher, state
    _config(world, "agent_claude_config_dir: ''\n")  # the default is Yggdrasil's own folder (AC-75)
    launcher.launch("tl1")
    data = launcher.receipt(state.runtime("tl1")["activation"])
    assert "permissions" not in json.loads(Path(data["settings"]).read_text())


def test_init_names_the_command_and_asks_nothing_about_permissions(world, tmp_path):
    from yggdrasil import init
    assert "agent_permissions" not in init.QUESTIONS
    out = tmp_path / "yggdrasil.yaml"
    message = init.run("starter", True, None, str(out), False)
    assert COMMAND in message
    assert "agent_permissions" not in out.read_text()
