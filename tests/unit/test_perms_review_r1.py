"""Codex review perms-r1 of fix/agent-permissions: the six majors, each reproduced first."""

import json
import os
import subprocess

import pytest
from test_agent_permissions import _config, _git  # noqa: F401
from test_agent_profiles import _commit, lane_repo  # noqa: F401  (shared fixture)
from test_guard_reads_and_hook_skips import _pretool, _push, _skip_hooks_commit, agent, pushed  # noqa: F401
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)

from yggdrasil import util

STANDARD = json.dumps({"profile": "standard", "rules_version": 1, "programmes": ["alpha"], "origin": "user"})


def _write_agreement(text):
    from yggdrasil import permissions
    path = permissions.agreement_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---------------------------------------------------------------- 1. the agreement is read fresh at each launch

def test_a_revocation_by_another_process_reaches_the_next_launch_without_a_cache_clear(world):
    from yggdrasil import launcher, permissions, state
    _config(world, "agent_claude_config_dir: " + str(world / "agent-claude") + "\n")
    permissions.allow_standard(yes=True)
    assert permissions.agreed_profile() == "standard"
    _write_agreement(json.dumps({"profile": "not-granted", "rules_version": 0, "programmes": [],
                                 "origin": "user"}))  # `ygg agents revoke`, elsewhere
    assert permissions.agreed_profile() is None
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1")
    assert error.value.code == util.EXIT_REFUSED
    assert state.runtime("tl1").get("activation") is None


def test_an_agreement_by_another_process_reaches_the_next_launch_without_a_cache_clear(world):
    from yggdrasil import config, permissions
    config.get("agent_permissions")  # the caches are warm
    _write_agreement(STANDARD)
    assert permissions.agreed_profile() == "standard"


# ---------------------------------------------------------------- 2. git reads stay in the lane's repository

def test_a_nested_git_file_pointing_at_a_foreign_repository_is_refused(agent, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git(foreign, "init", "-q", "-b", "main")
    nested = agent / "nested"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: " + str(foreign / ".git") + "\n")
    code, message = _pretool("git diff --cached", nested)
    assert code == 2 and "repository" in message
    assert _pretool("git diff --cached", agent) == (0, "")


def test_a_rewritten_worktree_git_file_is_refused(agent, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git(foreign, "init", "-q", "-b", "main")
    (agent / ".git").unlink()
    (agent / ".git").write_text("gitdir: " + str(foreign / ".git") + "\n")  # the agent edited its own .git
    assert _pretool("git status", agent)[0] == 2


def test_a_git_read_without_a_recorded_repository_fails_closed(agent):
    from yggdrasil import launcher
    launcher.save_receipt("act-0042", {"node": "tl1", "activation": "act-0042", "cwd": str(agent)})
    assert _pretool("git status", agent)[0] == 2


def test_a_launch_records_the_lanes_git_directory(world):
    from yggdrasil import commands, launcher, state
    home = world / "beta-home"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    _git(home, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "root")
    commands.programme_create("beta", "beta", str(home), "git-repo")
    commands.project_create("q1", "beta", "pm2")
    launcher.launch("pm2")  # a PM works in its project's integration worktree
    data = launcher.receipt(state.runtime("pm2")["activation"])
    expected = subprocess.run(["git", "-C", data["cwd"], "rev-parse", "--absolute-git-dir"], capture_output=True,
                              text=True).stdout.strip()
    assert data["git_dir"] == os.path.realpath(expected)
    common = subprocess.run(["git", "-C", data["cwd"], "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    assert data["git_common_dir"] == os.path.realpath(common)


# ---------------------------------------------------------------- 3. no symlinked agreement

def test_a_symlinked_agreement_into_an_agent_folder_counts_for_nothing(world):
    from yggdrasil import permissions
    planted = world / "home" / ".ygg-staging" / "p1" / "tl1"
    planted.mkdir(parents=True)
    (planted / "agents-agreement.json").write_text(STANDARD)
    path = permissions.agreement_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(planted / "agents-agreement.json", path)
    assert permissions.agreed_profile() is None


def test_any_symlinked_agreement_counts_for_nothing_and_is_never_written_through(world, tmp_path):
    from yggdrasil import permissions
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(STANDARD)
    path = permissions.agreement_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(elsewhere, path)
    assert permissions.agreed_profile() is None
    with pytest.raises(util.YggError) as error:
        permissions.allow_minimal()
    assert error.value.code == util.EXIT_REFUSED and "symbolic link" in error.value.message
    assert elsewhere.read_text() == STANDARD


# ---------------------------------------------------------------- 4. secret shapes in file names

SECRET_NAME = "ghp_" + "A" * 30 + ".txt"


def test_a_file_whose_name_is_a_secret_is_refused_at_commit(lane_repo):
    result = _commit(lane_repo, SECRET_NAME, "clean\n")
    assert result.returncode == 1 and "secret" in result.stderr


def test_a_file_whose_name_is_a_secret_is_refused_at_push(pushed):
    code, reason = _push(pushed, _skip_hooks_commit(pushed, SECRET_NAME, "clean\n", "add"))
    assert code != 0 and "secret" in reason


def test_deleting_a_file_whose_name_is_a_secret_is_refused_too(lane_repo):
    (lane_repo / SECRET_NAME).write_text("clean\n")
    _git(lane_repo, "add", "--", SECRET_NAME)
    _git(lane_repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "planted")
    _git(lane_repo, "rm", "-q", "--", SECRET_NAME)
    result = subprocess.run(["git", "-C", str(lane_repo), "commit", "-q", "-m", "remove"], capture_output=True,
                            text=True)
    assert result.returncode == 1 and "secret" in result.stderr


# ---------------------------------------------------------------- 5. the standard credential stores

@pytest.mark.parametrize("name,text", [
    (".aws/credentials", "[default]\naws_access_key_id = x\naws_secret_access_key = y\n"),
    (".npmrc", "//registry.npmjs.org/:_authToken=npm_" + "b" * 20 + "\n"),
    (".npmrc", "_auth=dXNlcjpwYXNz\n"),
    (".docker/config.json", json.dumps({"auths": {"https://index.docker.io/v1/": {"auth": "dXNlcjpwYXNz"}}})),
    (".git-credentials", "https://user:token@example.invalid\n"),
    (".kube/config", "apiVersion: v1\nusers:\n- name: u\n  user:\n    token: abc\n"),
    ("certs/store.jks", "x\n"), (".yarnrc.yml", "npmAuthToken: abc\n")])
def test_a_standard_credential_store_is_refused(lane_repo, pushed, name, text):
    result = _commit(lane_repo, name, text)
    assert result.returncode == 1 and "credential" in result.stderr, result.stderr
    code, reason = _push(pushed, _skip_hooks_commit(pushed, name, text, "add"))
    assert code != 0 and "credential" in reason


# A clean .npmrc is no longer here: A11 AC-67's signed patterns refuse every .npmrc.
@pytest.mark.parametrize("name,text", [
    (".docker/config.json", json.dumps({"credsStore": "desktop"})), (".aws/config", "[default]\nregion = x\n")])
def test_a_configuration_without_credentials_is_allowed(lane_repo, name, text):
    assert _commit(lane_repo, name, text).returncode == 0


# ---------------------------------------------------------------- 6. a visible permissions hold

def test_owed_work_refused_for_permissions_shows_a_visible_hold_that_clears_on_agreement(world):
    from yggdrasil import permissions, ratatoskr, reducer, status
    _config(world, "agent_claude_config_dir: " + str(world / "agent-claude") + "\n")
    ratatoskr.send("pm1", "tl1", "note", "work")
    view = status.build_status()
    tl1 = next(n for n in view["nodes"] if n["id"] == "tl1")
    assert tl1["state"] == "blocked" and "agent-permissions" in tl1["blocked_by"]
    assert "ygg agents allow-standard" in tl1["held_reason"]
    assert reducer.shadow_diff(view) == {}
    permissions.allow_standard(yes=True)
    tl1 = next(n for n in status.build_status()["nodes"] if n["id"] == "tl1")
    assert tl1["state"] == "queued" and "agent-permissions" not in tl1["blocked_by"]


def test_a_stale_agreement_shows_its_own_hold(world, monkeypatch):
    from yggdrasil import permissions, ratatoskr, status
    _config(world, "agent_claude_config_dir: " + str(world / "agent-claude") + "\n")
    permissions.allow_standard(yes=True)
    monkeypatch.setitem(permissions.VERSIONS, "standard", permissions.VERSIONS["standard"] + 1)
    ratatoskr.send("pm1", "tl1", "note", "work")
    tl1 = next(n for n in status.build_status()["nodes"] if n["id"] == "tl1")
    assert tl1["state"] == "blocked" and "version" in tl1["held_reason"]


def test_without_a_separate_agent_folder_there_is_no_permissions_hold(world):
    from yggdrasil import ratatoskr, status
    ratatoskr.send("pm1", "tl1", "note", "work")
    tl1 = next(n for n in status.build_status()["nodes"] if n["id"] == "tl1")
    assert "agent-permissions" not in tl1["blocked_by"]
