"""Codex review perms-r2 of fix/agent-permissions: the two remaining majors, from the reviewer's probes."""

import json
import shutil
import subprocess

import pytest
from test_agent_permissions import _git  # noqa: F401
from test_agent_profiles import _commit, lane_repo  # noqa: F401  (shared fixture)
from test_guard_reads_and_hook_skips import _pretool, _push, _skip_hooks_commit, agent, pushed  # noqa: F401
from test_round2 import world  # noqa: F401  (shared fixture)

from yggdrasil import hooks


def _foreign(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git(foreign, "init", "-q", "-b", "main")
    _git(foreign, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty",
         "-m", "FOREIGN-ONLY-COMMIT")
    return foreign


def _git_dir(path):
    return subprocess.run(["git", "-C", str(path), "rev-parse", "--absolute-git-dir"], capture_output=True,
                          text=True).stdout.strip()


# ---------------------------------------------------------------- 1. the lane's common git directory

def test_a_redirected_commondir_is_refused_though_the_git_dir_is_unchanged(agent, tmp_path):
    foreign = _foreign(tmp_path)
    git_dir = _git_dir(agent)
    assert _pretool("git log --oneline -n 1", agent) == (0, "")
    with open(git_dir + "/commondir", "w") as handle:
        handle.write(str(foreign / ".git") + "\n")
    with open(git_dir + "/HEAD", "w") as handle:
        handle.write("ref: refs/heads/main\n")
    assert _git_dir(agent) == git_dir  # the git directory alone looks the same
    code, message = _pretool("git log --oneline -n 1", agent)
    assert code == 2 and "repository" in message


def test_git_metadata_inside_the_lane_itself_is_refused(agent):
    """The probe's lane: its .git a folder inside the worktree, which the agent can write."""
    from yggdrasil import launcher
    git_dir = _git_dir(agent)
    _, original_common = launcher.lane_git_dirs(agent)
    (agent / ".git").unlink()
    shutil.copytree(git_dir, agent / ".git")
    # A linked worktree's commondir is relative (../..); copied, it would point elsewhere, so make it absolute.
    (agent / ".git" / "commondir").write_text(original_common + "\n")
    inside, common = launcher.lane_git_dirs(agent)
    launcher.save_receipt("act-0042", {"node": "tl1", "activation": "act-0042", "cwd": str(agent),
                                       "git_dir": inside, "git_common_dir": common})
    code, message = _pretool("git status", agent)
    assert code == 2 and "repository" in message


def test_a_read_without_a_recorded_common_directory_fails_closed(agent):
    from yggdrasil import launcher
    data = launcher.receipt("act-0042")
    data.pop("git_common_dir")
    launcher.save_receipt("act-0042", data)
    assert _pretool("git status", agent)[0] == 2


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"), ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
def test_editing_git_metadata_is_refused(agent, tool, key):
    for target in (agent / ".git", agent / "src" / ".git" / "config"):
        payload = {"tool_name": tool, "cwd": str(agent), "tool_input": {key: str(target)}}
        code, _ = hooks.pretool(json.dumps(payload))
        assert code == 2, (tool, target)


def test_editing_an_ordinary_file_or_gitignore_is_not_refused_by_that_rule(agent):
    for target in (agent / "src" / "a.py", agent / ".gitignore"):
        payload = {"tool_name": "Write", "cwd": str(agent), "tool_input": {"file_path": str(target)}}
        assert hooks.pretool(json.dumps(payload)) == (0, "")


# ---------------------------------------------------------------- 2. scoped npm and escaped Docker auth

CREDENTIALS = [
    (".npmrc", "//registry.npmjs.org/:_auth=dXNlcjpwYXNz\n"),
    (".npmrc", "//registry.example.invalid/:_authToken=abc\n"),
    (".npmrc", "//registry.example.invalid/:_password=cGFzcw==\n"),
    (".docker/config.json", '{"auths":{"registry.example.invalid":{"\\u0061uth":"dXNlcjpwYXNz"}}}\n'),
    (".docker/config.json", '{"auths":{"r":{"identityToken":"x"}}}\n'),
]


@pytest.mark.parametrize("name,text", CREDENTIALS)
def test_scoped_npm_and_escaped_docker_auth_are_refused_at_commit(lane_repo, name, text):
    result = _commit(lane_repo, name, text)
    assert result.returncode == 1 and "credential" in result.stderr, result.stderr


@pytest.mark.parametrize("name,text", CREDENTIALS)
def test_scoped_npm_and_escaped_docker_auth_are_refused_at_push(pushed, name, text):
    code, reason = _push(pushed, _skip_hooks_commit(pushed, name, text, "add"))
    assert code != 0 and "credential" in reason


# A .npmrc is no longer here: A11 AC-67's signed patterns refuse every .npmrc.
@pytest.mark.parametrize("name,text", [
    (".docker/config.json", '{"auths":{"r":{}},"credsStore":"desktop"}\n')])
def test_configurations_that_name_auth_without_carrying_it_are_allowed(lane_repo, name, text):
    assert _commit(lane_repo, name, text).returncode == 0
