"""Ruling 129: the standard permission profile, the versioned agreement, project settings, the lane hooks."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from test_agent_permissions import RULE, _abs, _config, _git, lane  # noqa: F401  (shared fixtures)
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)

from yggdrasil import util



def _standard():
    from yggdrasil import permissions
    output = permissions.allow_standard(yes=True)
    clear_caches()
    return output


def _read_only(cwd):
    from yggdrasil.permissions import STANDARD_READ_RULES
    assert STANDARD_READ_RULES == ["Bash(git status)", "Bash(git status *)", "Bash(git diff)", "Bash(git diff *)",
                                   "Bash(git log)", "Bash(git log *)", "Bash(git show *)", "Bash(git rev-parse *)",
                                   "Bash(git branch --list)", "Bash(git branch --list *)"]
    return list(STANDARD_READ_RULES)


DENY = ["Bash(git *--output*)", "Bash(git *--no-index*)"]
NAMED = ["Bash(pytest -q)", "Bash(pytest -q *)", "Bash(npm run build)", "Bash(npm run build *)"]


@pytest.fixture
def commands(world, monkeypatch):
    """The approved PID names its test and build commands (AC-74); the reading itself is tested below."""
    from yggdrasil import permissions
    monkeypatch.setattr(permissions, "commands_for",
                        lambda project: ["pytest -q", "npm run build"] if project == "q1" else [])


# ---------------------------------------------------------------- the standard rules, exactly

def test_a_team_lead_gets_read_only_git_the_programmes_commands_and_its_own_lane_push(world, lane, commands):
    from yggdrasil import permissions, tracker
    _standard()
    worktree = world / "beta-home" / ".ygg-worktrees" / "q1" / "lane" / "tl2"
    worktree.mkdir(parents=True)
    assert permissions.block(lane["tl2"], lane, worktree, is_git=True) == {"defaultMode": "dontAsk", "allow": [
        "Bash(ygg *)", "Read(" + _abs(tracker.project_dir("q1")) + "/**)", "Edit(" + _abs(worktree) + "/**)",
        "Bash(git add *)", "Bash(git commit *)", *_read_only(worktree),
        "Bash(git mv *)", "Bash(git rm *)", "Bash(git restore *)",
        "Bash(git push origin ygg/q1/lane/tl2)", *NAMED], "deny": DENY}


def test_a_worker_gets_the_commands_but_never_a_push(world, lane, commands):
    from yggdrasil import permissions
    _standard()
    worktree = world / "beta-home" / ".ygg-worktrees" / "q1" / "lane" / "tl2"
    worktree.mkdir(parents=True)
    allow = permissions.block(lane["w2"], lane, worktree, is_git=True)["allow"]
    assert set(NAMED) <= set(allow)
    assert not [rule for rule in allow if "push" in rule]


def test_a_senior_user_gets_read_only_git_and_the_commands_and_edits_nothing(world, lane, commands):
    from yggdrasil import permissions, tracker
    _standard()
    checkout = world / "beta-home" / ".ygg-worktrees" / "q1" / "_test-su2"
    checkout.mkdir(parents=True)
    assert permissions.block(lane["su2"], lane, checkout, is_git=True) == {"defaultMode": "dontAsk", "allow": [
        "Bash(ygg *)", "Read(" + _abs(tracker.project_dir("q1")) + "/**)", *_read_only(checkout), *NAMED],
        "deny": DENY}


def test_a_pm_gets_read_only_git_but_no_commands_or_push(world, lane, commands):
    from yggdrasil import permissions
    _standard()
    worktree = world / "beta-home" / ".ygg-worktrees" / "q1" / "_integration"
    worktree.mkdir(parents=True)
    allow = permissions.block(lane["pm2"], lane, worktree, is_git=True)["allow"]
    assert set(_read_only(worktree)) <= set(allow)
    assert "Bash(pytest -q *)" not in allow and not [rule for rule in allow if "push" in rule]
    assert "Bash(git commit *)" not in allow


def test_named_commands_come_from_the_approved_pid_header_only(world, monkeypatch):
    from yggdrasil import commands as verbs, permissions, tracker
    assert permissions.commands_for("p1") == []  # approved without agent_commands
    text = tracker.read_document("p1", "pid")
    assert "agent_commands" not in text
    header = text.split("---\n")[1]
    verbs.tracker_write("pm1", "p1", "pid", "---\n" + header + "agent_commands: [make test]\n---\n# PID\n")
    assert permissions.commands_for("p1") == []  # only the live header names it


def test_outside_git_there_is_no_git_rule_at_all(world, commands):
    from yggdrasil import permissions, state
    _standard()
    nodes = state.all_nodes()
    staging = world / "home" / ".ygg-staging" / "p1" / "tl1"
    staging.mkdir(parents=True)
    block = permissions.block(nodes["tl1"], nodes, staging, is_git=False)
    assert not [rule for rule in block["allow"] if rule.startswith("Bash(git")]


def test_every_standard_rule_is_one_claude_code_accepts(world, lane, commands):
    from yggdrasil import permissions
    _standard()
    worktree = world / "beta-home" / ".ygg-worktrees" / "q1" / "lane" / "tl2"
    worktree.mkdir(parents=True)
    block = permissions.block(lane["tl2"], lane, worktree, is_git=True)
    assert all(RULE.match(rule) for rule in block["allow"] + block["deny"])


def test_the_minimal_profile_is_unchanged_by_the_standard_one(world, lane, commands):
    from yggdrasil import permissions, tracker
    permissions.allow_minimal()
    clear_caches()
    worktree = world / "beta-home" / ".ygg-worktrees" / "q1" / "lane" / "tl2"
    worktree.mkdir(parents=True)
    assert permissions.block(lane["tl2"], lane, worktree, is_git=True)["allow"] == [
        "Bash(ygg *)", "Read(" + _abs(tracker.project_dir("q1")) + "/**)", "Edit(" + _abs(worktree) + "/**)",
        "Bash(git add *)", "Bash(git commit *)"]


# ---------------------------------------------------------------- the programme's commands are the user's

def test_agent_commands_count_only_from_the_users_own_config(world, lane, monkeypatch, tmp_path):
    from yggdrasil import config, tracker
    programme = tracker.programme("beta")
    programme["config"] = {"agent_commands": {"beta": ["rm -rf ."]}}
    tracker.save_programme(programme)
    clear_caches()
    with pytest.raises(util.YggError) as error:
        config.validate("q1")
    assert "agent_commands" in error.value.message
    assert config.get("agent_commands") is None and config.project_get("agent_commands", "q1") is None
    folder = tmp_path / "cwd-config"
    folder.mkdir()
    (folder / "yggdrasil.yaml").write_text("agent_commands:\n  beta: [make]\n")
    monkeypatch.delenv("YGG_CONFIG")
    monkeypatch.chdir(folder)  # a current-folder config may be one an agent wrote
    programme["config"] = {}
    tracker.save_programme(programme)
    clear_caches()
    assert config.get("agent_commands") is None


@pytest.mark.parametrize("bad", ["", " pytest", "pytest; rm -rf .", "make && x", "a | b", "run $(x)", "go (x)",
                                 "make *", "a\nb", "echo `x`", "x > y"])
def test_a_command_that_is_not_one_plain_command_is_refused(world, bad):
    from yggdrasil import config
    _config(world, "agent_commands:\n  beta: [" + json.dumps(bad) + "]\n")
    with pytest.raises(util.YggError) as error:
        config.validate()
    assert "agent_commands" in error.value.message


# ---------------------------------------------------------------- allow-standard shows before it records

def test_allow_standard_without_yes_shows_the_exact_rules_and_records_nothing(world, commands, capsys):
    from yggdrasil import cli, config
    assert cli.main(["agents", "allow-standard"]) == 0
    out = capsys.readouterr().out
    from yggdrasil import permissions
    for rule in ("Bash(ygg *)", "Bash(git status)", "Bash(git branch --list *)", "Bash(git *--output*)",
                 "Bash(git push origin ygg/<project>/lane/<team lead>)", "Bash(<named commands>)",
                 "Edit(//<worktree>/**)", "Edit(//<review dir>/**)"):
        assert rule in out, rule
    for heading in ("Project manager:", "Team lead:", "Worker:", "Senior user:", "Reviewer:"):
        assert heading in out
    assert "--yes" in out
    assert not permissions.agreement_path().exists()


def test_allow_standard_with_yes_records_the_profile_and_its_version(world):
    from yggdrasil import cli, config, permissions
    assert cli.main(["agents", "allow-standard", "--yes"]) == 0
    clear_caches()
    recorded = json.loads(permissions.agreement_path().read_text())
    assert recorded["profile"] == "standard" and recorded["origin"] == "user"
    assert recorded["rules_version"] == permissions.VERSIONS["standard"]
    assert permissions.agreed_profile() == "standard"


def test_allow_standard_is_user_only(world, monkeypatch, capsys):
    from yggdrasil import cli
    monkeypatch.setenv("YGG_NODE", "tl1")
    for argv in (["agents", "allow-standard"], ["agents", "allow-standard", "--yes"]):
        assert cli.main(argv) == util.EXIT_REFUSED
        assert "user-only" in capsys.readouterr().err
    from yggdrasil import permissions
    assert not permissions.agreement_path().exists()


# ---------------------------------------------------------------- the agreement is versioned

def test_a_wider_rule_set_refuses_the_launch_until_the_user_agrees_again(world, monkeypatch):
    from yggdrasil import launcher, permissions, state
    _config(world, "agent_claude_config_dir: " + str(world / "agent-claude") + "\n")
    _standard()
    monkeypatch.setitem(permissions.VERSIONS, "standard", permissions.VERSIONS["standard"] + 1)
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1")
    assert error.value.code == util.EXIT_REFUSED
    assert "ygg agents allow-standard" in error.value.message and "version" in error.value.message
    assert state.runtime("tl1").get("activation") is None
    _standard()
    launcher.launch("tl1")
    data = launcher.receipt(state.runtime("tl1")["activation"])
    assert json.loads(Path(data["settings"]).read_text())["permissions"]["deny"] == DENY  # the standard rules


def test_an_agreement_without_a_version_counts_for_nothing(world):
    from yggdrasil import permissions
    path = permissions.agreement_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"profile": "standard", "programmes": ["alpha"], "origin": "user"}))
    assert permissions.agreed_profile() is None


def test_the_refusal_names_the_standard_command_and_the_strict_option(world):
    from yggdrasil import launcher
    _config(world, "agent_claude_config_dir: " + str(world / "agent-claude") + "\n")
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1")
    assert "ygg agents allow-standard" in error.value.message and "ygg agents allow-minimal" in error.value.message


# ---------------------------------------------------------------- project settings

def test_by_default_an_agent_reads_the_projects_claude_md_but_not_its_settings(world):
    from yggdrasil import launcher
    data = {"cli": "claude-code", "cwd": str(world), "settings": str(world / "s.json"), "model": "m"}
    argv, environment = launcher.claude_command(data, {})
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert argv[argv.index("--add-dir") + 1] == str(world)
    assert environment["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] == "1"


def test_the_super_user_setting_includes_the_projects_settings(world):
    from yggdrasil import launcher
    _config(world, "agent_project_settings: include\n")
    data = {"cli": "claude-code", "cwd": str(world), "settings": str(world / "s.json"), "model": "m"}
    argv, environment = launcher.claude_command(data, {})
    assert argv[argv.index("--setting-sources") + 1] == "user,project,local" and "--add-dir" not in argv
    assert "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD" not in environment


def test_a_launch_records_the_project_settings_choice(world):
    from yggdrasil import launcher, state
    launcher.launch("tl1")
    assert launcher.receipt(state.runtime("tl1")["activation"])["project_settings"] == "exclude"


def test_agent_project_settings_is_set_only_in_user_layers(world, lane, monkeypatch, tmp_path):
    from yggdrasil import config, tracker
    _config(world, "agent_project_settings: sometimes\n")
    with pytest.raises(util.YggError):
        config.validate()
    _config(world, "")
    programme = tracker.programme("beta")
    programme["config"] = {"agent_project_settings": "include"}
    tracker.save_programme(programme)
    clear_caches()
    with pytest.raises(util.YggError):
        config.validate("q1")
    assert config.project_get("agent_project_settings", "q1") == "exclude"  # the core default (A11 AC-35)
    programme["config"] = {}
    tracker.save_programme(programme)
    folder = tmp_path / "cwd-config"
    folder.mkdir()
    (folder / "yggdrasil.yaml").write_text("agent_project_settings: include\n")
    monkeypatch.delenv("YGG_CONFIG")
    monkeypatch.chdir(folder)
    clear_caches()
    assert config.get("agent_project_settings") == "exclude"  # the core default (A11 AC-35)


# ---------------------------------------------------------------- Yggdrasil's own lane checks

@pytest.fixture
def lane_repo(world, monkeypatch):
    """A lane worktree using Yggdrasil's hooks folder, with a denylist and a home pre-commit hook."""
    from yggdrasil import launcher
    home = world / "lane-home"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    _git(home, "config", "user.name", "t")
    _git(home, "config", "user.email", "t@example.invalid")
    _git(home, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty", "-m", "root")
    denylist = world / "denylist.txt"
    denylist.write_text("zebra-orchid-7\n")
    ygg = world / "bin" / "ygg"
    ygg.parent.mkdir()
    ygg.write_text("#!/bin/sh\nexec " + __import__("sys").executable + " -m yggdrasil \"$@\"\n")
    ygg.chmod(0o755)
    _config(world, "leak_denylist: " + str(denylist) + "\n")  # the profile's denylist only (A11 AC-31)
    hooks = launcher.hooks_dir(str(ygg))
    # As on a lane worktree: Yggdrasil's hooks folder in the worktree's own config, the home's left as it is.
    _git(home, "config", "extensions.worktreeConfig", "true")
    _git(home, "config", "--worktree", "core.hooksPath", str(hooks))
    return home


def _commit(repo, name, text, message="add"):
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", "--", name], check=True, capture_output=True)
    return subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], capture_output=True, text=True,
                          env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src")))


def test_the_lane_check_lets_a_clean_commit_through(lane_repo):
    assert _commit(lane_repo, "notes.md", "hello\n").returncode == 0


@pytest.mark.parametrize("name,text", [("notes.md", "a zebra-orchid-7 here\n"),
                                       ("key.txt", "token ghp_" + "a" * 30 + "\n")])
def test_the_lane_check_refuses_a_leak_in_staged_content(lane_repo, name, text):
    result = _commit(lane_repo, name, text)
    assert result.returncode == 1 and "lane check" in result.stderr


@pytest.mark.parametrize("name", [".github/workflows/ci.yml", ".env", "deploy/id_rsa", "certs/server.pem"])
def test_the_lane_check_refuses_ci_workflows_and_credential_files(lane_repo, name):
    result = _commit(lane_repo, name, "x\n")
    assert result.returncode == 1 and name in result.stderr


def test_a_co_authored_by_trailer_is_refused_when_the_identity_says_no_trailers(world, lane_repo):
    _config(world, "commit_identity:\n  name: T\n  trailers: none\n")
    message = "add\n\nCo-Authored-By: Some Model <noreply@example.invalid>\n"
    result = _commit(lane_repo, "notes.md", "hello\n", message)
    assert result.returncode == 1 and "trailers are not allowed by the profile" in result.stderr
    _config(world, "commit_identity:\n  name: T\n")
    assert _commit(lane_repo, "notes.md", "hello again\n", message).returncode == 0


def test_the_home_pre_commit_never_runs_on_a_lane_commit(lane_repo):
    """A11 AC-16 (amends A10 AC-03): lane commits run Yggdrasil's light check only."""
    hooks = lane_repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\necho 'home pre-commit ran' >&2\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    result = _commit(lane_repo, "notes.md", "hello\n")
    assert result.returncode == 0 and "home pre-commit ran" not in result.stderr


def test_commit_identity_trailers_takes_none_or_allow(world):
    from yggdrasil import config
    _config(world, "commit_identity:\n  trailers: maybe\n")
    with pytest.raises(util.YggError) as error:
        config.validate()
    assert "trailers" in error.value.message


def test_the_profiles_denylist_is_the_lane_checks_denylist(world, lane_repo, tmp_path):
    """A11 AC-31: the denylist comes only from the profile's leak_denylist, never git config."""
    public = tmp_path / "public-denylist.txt"
    public.write_text("founder-name-3\n")
    _config(world, "leak_denylist: " + str(public) + "\n")
    _git(lane_repo, "config", "ygg.leakDenylist", str(tmp_path / "unused.txt"))
    result = _commit(lane_repo, "notes.md", "founder-name-3 wrote this\n")
    assert result.returncode == 1 and "founder-name-3" in result.stderr


def test_a_claude_code_senior_user_is_told_the_shape_its_guard_allows(world, monkeypatch):
    """Its working folder is the testing checkout and its guard refuses `git -C`, so the prompt names neither."""
    import sys
    from test_round3 import receipt_file
    from yggdrasil import launcher, state, tracker
    seen_file = world / "seen.json"
    fake = world / "claude"
    fake.write_text("#!" + sys.executable + "\nimport json, sys\njson.dump({'stdin': sys.stdin.read()}, open(" +
                    repr(str(seen_file)) + ", 'w'))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("YGG_CLAUDE_BIN", str(fake))
    activation = tracker.allocate("activation")
    receipt_file(activation, node="su1", outcome="pending", phase="starting", pid=None, cwd=str(world),
                 model="claude-opus-5-5", effort="high", cli="claude-code", programme="alpha", project="p1", role="su",
                 settings=str(world / "settings.json"),
                 pinned={"YGG_STATE_ROOT": str(world / "state"), "YGG_FILES_ROOT": str(world / "tracker")},
                 testing={"package": "WP-1", "lane": "tl1", "branch": "ygg/p1/lane/tl1", "checkout": str(world)})
    state.save_runtime("su1", {"state": "running", "activation": activation, "outcome": "pending"})
    launcher.supervise(activation)
    stdin = json.loads(seen_file.read_text())["stdin"]
    assert "`git rev-parse HEAD` from your working folder" in stdin and "git -C" not in stdin
