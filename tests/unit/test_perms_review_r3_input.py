"""Security review input for round 3: yarn's space-separated auth, duplicate Docker keys, and .git edits by
case, relative path or symlink."""

import json
import os
import subprocess

import pytest
from test_agent_permissions import _git  # noqa: F401
from test_agent_profiles import _commit, lane_repo  # noqa: F401  (shared fixture)
from test_guard_reads_and_hook_skips import _push, _skip_hooks_commit, agent, pushed  # noqa: F401
from test_round2 import world  # noqa: F401  (shared fixture)

from yggdrasil import hooks

CREDENTIALS = [
    (".yarnrc", "_authToken abc123\n"),
    (".yarnrc", '"//registry.example.invalid/:_authToken" "abc123"\n'),
    (".yarnrc", "'_auth' dXNlcjpwYXNz\n"),
    (".npmrc", '"_authToken"="abc123"\n'),
    (".docker/config.json", '{"auths":{"x":{"auth":"dXNlcjpwYXNz","auth":""}}}\n'),
    (".docker/config.json", '{"auths":{"x":{"auth":""}},"auths":{"y":{"auth":"dXNlcjpwYXNz"}}}\n'),
]


@pytest.mark.parametrize("name,text", CREDENTIALS)
def test_hidden_auth_forms_are_refused_at_commit(lane_repo, name, text):
    result = _commit(lane_repo, name, text)
    assert result.returncode == 1 and "credential" in result.stderr, result.stderr


@pytest.mark.parametrize("name,text", CREDENTIALS)
def test_hidden_auth_forms_are_refused_at_push(pushed, name, text):
    code, reason = _push(pushed, _skip_hooks_commit(pushed, name, text, "add"))
    assert code != 0 and "credential" in reason


# A .npmrc is no longer here: A11 AC-67's signed patterns refuse every .npmrc.
@pytest.mark.parametrize("name,text", [
    (".yarnrc", 'registry "https://registry.npmjs.org/"\nalways-auth true\n'),
    (".docker/config.json", '{"auths":{"x":{"auth":""}},"credsStore":"desktop"}\n')])
def test_auth_named_without_a_value_is_allowed(lane_repo, name, text):
    assert _commit(lane_repo, name, text).returncode == 0


def _edit(agent, target, cwd=None):
    payload = {"tool_name": "Write", "cwd": str(cwd or agent), "tool_input": {"file_path": str(target)}}
    return hooks.pretool(json.dumps(payload))[0]


def test_a_differently_cased_git_folder_is_refused(agent):
    assert _edit(agent, agent / ".GIT" / "HEAD") == 2
    assert _edit(agent, agent / "src" / ".Git" / "config") == 2


def test_a_relative_path_into_git_metadata_is_refused(agent):
    git_dir = subprocess.run(["git", "-C", str(agent), "rev-parse", "--absolute-git-dir"], capture_output=True,
                             text=True).stdout.strip()
    relative = os.path.relpath(os.path.join(git_dir, "HEAD"), str(agent / "src"))
    assert relative.startswith("..")
    assert _edit(agent, relative, cwd=agent / "src") == 2


def test_a_symlink_in_the_worktree_pointing_into_the_git_directory_is_refused(agent):
    git_dir = subprocess.run(["git", "-C", str(agent), "rev-parse", "--absolute-git-dir"], capture_output=True,
                             text=True).stdout.strip()
    os.symlink(git_dir, agent / "meta")
    assert _edit(agent, agent / "meta" / "commondir") == 2


def test_a_symlink_into_a_git_directory_with_no_git_in_its_name_is_refused(agent, tmp_path):
    from yggdrasil import launcher
    data = launcher.receipt("act-0042")
    common = data["git_common_dir"]
    bare = tmp_path / "plain-name"
    os.symlink(common, bare)
    assert _edit(agent, bare / "config") == 2


def test_an_ordinary_relative_path_in_the_worktree_is_allowed(agent):
    assert _edit(agent, "a.py", cwd=agent / "src") == 0


# ---------------------------------------------------------------- colon separators and Unicode-folded keys

FOLDED = [
    (".yarnrc", "_authToken: abc123\n"),
    (".npmrc", "_password:cGFzcw==\n"),
    (".docker/config.json", '{"auths":{"x":{"pa\\u017f\\u017fword":"hunter2"}}}\n'),       # long s folds to s
    (".docker/config.json", '{"auths":{"x":{"identityto\\u212aen":"abc"}}}\n'),             # Kelvin sign folds to k
    (".docker/config.json", '{"auths":{"x":{"\\u0101uth":"dXNlcjpwYXNz"}}}\n'),             # non-ASCII: fail closed
]


@pytest.mark.parametrize("name,text", FOLDED)
def test_colon_separated_and_unicode_folded_auth_is_refused_at_commit(lane_repo, name, text):
    result = _commit(lane_repo, name, text)
    assert result.returncode == 1 and "credential" in result.stderr, result.stderr


@pytest.mark.parametrize("name,text", FOLDED)
def test_colon_separated_and_unicode_folded_auth_is_refused_at_push(pushed, name, text):
    code, reason = _push(pushed, _skip_hooks_commit(pushed, name, text, "add"))
    assert code != 0 and "credential" in reason


# ---------------------------------------------------------------- identity, not names; untrimmed auth values

@pytest.fixture
def separate(agent, tmp_path):
    """The lane's recorded common git directory in a folder with no .git in its path (a separate git dir)."""
    from yggdrasil import launcher
    store = tmp_path / "STORE"
    store.mkdir()
    (store / "HEAD").write_text("ref: refs/heads/main\n")
    data = launcher.receipt("act-0042")
    data["git_common_dir"] = str(store)
    launcher.save_receipt("act-0042", data)
    return store


def test_an_edit_through_a_symlink_into_a_separate_git_dir_is_refused(agent, separate):
    os.symlink(separate, agent / "alias")
    assert _edit(agent, agent / "alias" / "HEAD") == 2
    assert _edit(agent, agent / "alias" / "objects" / "info" / "alternates") == 2  # not there yet


def test_an_edit_through_a_differently_cased_alias_is_refused(agent, separate, tmp_path):
    if not os.path.exists(tmp_path / "store"):
        pytest.skip("this file system is case-sensitive")
    os.symlink(tmp_path / "store", agent / "alias")  # names the same folder in another case
    assert _edit(agent, agent / "alias" / "HEAD") == 2
    assert _edit(agent, str(tmp_path / "Store" / "alternates")) == 2


def test_an_edit_beside_the_separate_git_dir_is_not_refused_by_that_rule(agent, separate):
    assert _edit(agent, agent / "src" / "notes.md") == 0


@pytest.mark.parametrize("text", ['{"auths":{"x":{"password":" "}}}\n', '{"auths":{"x":{"auth":"\\t"}}}\n'])
def test_a_whitespace_docker_auth_value_still_counts(lane_repo, pushed, text):
    result = _commit(lane_repo, ".docker/config.json", text)
    assert result.returncode == 1 and "credential" in result.stderr, result.stderr
    code, reason = _push(pushed, _skip_hooks_commit(pushed, ".docker/config.json", text, "add"))
    assert code != 0 and "credential" in reason


# ---------------------------------------------------------------- perms-r4: a senior user lists branches

@pytest.fixture
def senior(agent, monkeypatch):
    from yggdrasil import launcher
    data = launcher.receipt("act-0042")
    data["node"] = "su1"
    launcher.save_receipt("act-0042", data)
    monkeypatch.setenv("YGG_NODE", "su1")
    return agent


def _bash(cwd, command):
    return hooks.pretool(json.dumps({"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}))


@pytest.mark.parametrize("command", ["git branch --list", "git branch --list ygg/*", "git status", "git log --oneline"])
def test_a_senior_user_runs_every_standard_read_shape(senior, command):
    assert _bash(senior, command) == (0, ""), command


@pytest.mark.parametrize("command", ["git branch x", "git branch -d ygg/p1/lane/tl1", "git branch -m a b", "git branch"])
def test_a_senior_user_still_never_writes_or_lists_branches_outside_the_shape(senior, command):
    assert _bash(senior, command)[0] == 2, command


# ---------------------------------------------------------------- merge with main: the guard-settings check

@pytest.mark.parametrize("command", ["git branch --list ygg/*", "git branch --list ygg/p1/*"])
def test_the_branch_listing_shape_passes_the_guard_settings_check(command):
    assert not hooks.guard_path_touched("Bash", {"command": command})


@pytest.mark.parametrize("command", ["ls .cl*", "cat ygg/* > .claude/settings.json", "git branch --list ygg/* x",
                                     "rm -r .*", "git branch ygg/*"])
def test_other_glob_words_still_trip_the_guard_settings_check(command):
    assert hooks.static_checks("Bash", {"command": command}) is not None
