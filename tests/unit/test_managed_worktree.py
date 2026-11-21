"""A managed worktree fails closed (A5.1), and its guard settings and the tracker credentials are guarded."""

import os

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


def test_inside_a_managed_worktree_a_session_without_markers_is_managed(world, monkeypatch, tmp_path):
    from yggdrasil import state
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    (worktree / ".ygg-managed").write_text("tl1\n")
    monkeypatch.chdir(worktree / "src")
    assert state.managed_session()
    with pytest.raises(util.YggError) as error:
        state.check_user_only("pid work-ahead")
    assert "REFUSED: user-only" in error.value.message
    monkeypatch.chdir(tmp_path)
    assert not state.managed_session()


@pytest.mark.parametrize("tool,tool_input", [
    ("Write", {"file_path": ".claude/settings.json"}),
    ("Edit", {"file_path": "/x/wt/.CLAUDE/Settings.Local.json"}),
    ("Write", {"file_path": ".ygg-managed"}),
    ("Bash", {"command": "rm -rf .claude"}),
    ("Bash", {"command": "echo tl9 > .ygg-managed"}),
    ("Bash", {"command": "security find-generic-password -s yggdrasil-linear -w"}),
])
def test_the_guard_settings_and_the_keychain_are_denied(world, tool, tool_input):
    from yggdrasil import hooks
    assert hooks.static_checks(tool, tool_input)[0] == 2


def test_the_linear_token_store_is_denied(world, monkeypatch, tmp_path):
    from yggdrasil import hooks
    monkeypatch.setenv("YGG_LINEAR_FAKE", str(tmp_path / "linear"))
    store = os.path.join(str(tmp_path / "linear"), "token-store.json")
    assert hooks.static_checks("Bash", {"command": "cat " + store})[0] == 2


def test_ordinary_work_is_not_affected(world):
    from yggdrasil import hooks
    assert hooks.static_checks("Write", {"file_path": "src/app.py"}) is None
    assert hooks.static_checks("Bash", {"command": "pytest -q tests"}) is None


def test_the_token_store_and_its_folder_are_denied_by_real_path(world, monkeypatch, tmp_path):
    from yggdrasil import hooks
    linear = tmp_path / "linear"
    linear.mkdir()
    (linear / "token-store.json").write_text("{}")
    os.symlink(linear, tmp_path / "alias")
    monkeypatch.setenv("YGG_LINEAR_FAKE", str(linear))
    monkeypatch.chdir(tmp_path)
    assert hooks.static_checks("Read", {"file_path": "alias/token-store.json"})[0] == 2
    assert hooks.static_checks("Glob", {"pattern": "*", "path": str(linear)})[0] == 2
    assert hooks.static_checks("Bash", {"command": "cat 'alias/token-store.json'"})[0] == 2
    assert hooks.static_checks("Bash", {"command": "ls alias"})[0] == 2
    assert hooks.static_checks("Read", {"file_path": "notes.md"}) is None


@pytest.mark.parametrize("command", ["cat .c*/settings.json", "rm -f .ygg-*", "cp x './.CLAUDE/settings.json'",
                                     "mv a '.clau'de", "ls .cl[a]ude"])
def test_quoted_or_globbed_guard_paths_are_denied(world, command):
    from yggdrasil import hooks
    assert hooks.static_checks("Bash", {"command": command})[0] == 2


@pytest.mark.parametrize("command", ["security find-generic-password -s x",
                                     "python3 -c 'import subprocess; subprocess.run([\"security\"])'",
                                     "echo yggdrasil-linear"])
def test_the_keychain_is_denied(world, command):
    from yggdrasil import hooks
    assert hooks.static_checks("Bash", {"command": command})[0] == 2


def test_the_hook_fails_closed_for_a_managed_session_without_identity(world, monkeypatch, tmp_path):
    import json
    from yggdrasil import hooks
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".ygg-managed").write_text("tl1\n")
    monkeypatch.chdir(worktree)
    monkeypatch.delenv("YGG_NODE", raising=False)
    code, message = hooks.pretool(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
    assert code == 2 and "without its node identity" in message
    monkeypatch.chdir(tmp_path)
    assert hooks.pretool(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))[0] == 0


@pytest.mark.parametrize("command", ["cat x.claude/settings.json", "printf %s foo=.claude/x",
                                     "tar -cf a.tar ./sub/.ygg-managed", "echo $(printf .claude)",
                                     "ls \"$HOME\"/wt/.claude"])
def test_guard_names_are_denied_by_raw_text_and_by_split_words(world, command):
    from yggdrasil import hooks
    assert hooks.static_checks("Bash", {"command": command})[0] == 2


def test_the_token_store_is_denied_by_name_glob_and_serialised_input(world, monkeypatch, tmp_path):
    from yggdrasil import hooks
    linear = tmp_path / "linear"
    linear.mkdir()
    (linear / "token-store.json").write_text("{}")
    monkeypatch.setenv("YGG_LINEAR_FAKE", str(linear))
    for command in ("cd linear && cat token-store.json", "cat linear/token-*.json", "cat linear/tok?n-store.json"):
        assert hooks.static_checks("Bash", {"command": command})[0] == 2, command
    assert hooks.static_checks("WebFetch", {"url": "file://" + str(linear / "token-store.json")})[0] == 2
    assert hooks.static_checks("Bash", {"command": "cat README.md"}) is None


@pytest.mark.parametrize("command", ["cat .cla{u,x}de/settings.json", "rm .ygg-{managed,x}", "echo {a..c}",
                                     "cat linear/token-st{o,x}re.json"])
def test_brace_expansion_is_refused(world, command):
    from yggdrasil import hooks
    code, message = hooks.static_checks("Bash", {"command": command})
    assert code == 2 and "Brace expansion" in message


def test_the_token_store_check_ignores_case(world, monkeypatch, tmp_path):
    from yggdrasil import hooks
    linear = tmp_path / "linear"
    linear.mkdir()
    (linear / "token-store.json").write_text("{}")
    monkeypatch.setenv("YGG_LINEAR_FAKE", str(linear))
    upper = str(linear).upper() + "/TOKEN-STORE.JSON"
    assert hooks.static_checks("Read", {"file_path": upper})[0] == 2
    assert hooks.static_checks("Bash", {"command": "cat TOKEN-STORE.JSON"})[0] == 2
    assert hooks.static_checks("Bash", {"command": "cat linear/TOKEN-*.JSON"})[0] == 2
    assert hooks.static_checks("WebFetch", {"url": "file://" + upper})[0] == 2


@pytest.mark.parametrize("command,denied", [("git clean -fdx", True), ("git clean -fdX", True),
                                            ("git -C wt clean -f", True), ("git clean", True),
                                            ("git clean -n -fdx", False), ("git clean --dry-run -d", False)])
def test_git_clean_is_denied_unless_a_dry_run(world, command, denied):
    from yggdrasil import hooks
    result = hooks.static_checks("Bash", {"command": command})
    assert (result is not None and result[0] == 2) == denied


def test_a_case_variant_of_the_token_store_is_the_same_file(world, monkeypatch, tmp_path):
    from yggdrasil import hooks
    creds = tmp_path / "credentials"
    creds.mkdir()
    (creds / "token-store.json").write_text("{}")
    monkeypatch.setenv("YGG_LINEAR_FAKE", str(creds))
    variant = str(tmp_path / "CREDENTIALS" / "TOKEN-STORE.JSON")
    if os.path.exists(variant):  # a case-insensitive volume: the variant names the same file
        assert hooks.static_checks("Read", {"file_path": variant})[0] == 2
        assert hooks.static_checks("Bash", {"command": "cat " + variant})[0] == 2


@pytest.mark.parametrize("command,denied", [
    ("git --git-dir .git clean -fdx", True), ("git --work-tree . clean -fdx", True),
    ("git clean -fdx -enode_modules/", True), ("git clean -fdx -e -n", True), ("git clean -fd -- -n", True),
    ("git stash --all", True), ("git stash -a", True), ("git stash push --all", True),
    ("git clean -fdx -n", False), ("git clean -fdn", False), ("git stash", False)])
def test_git_cleanups_that_can_remove_the_guard_files_are_denied(world, command, denied):
    from yggdrasil import hooks
    result = hooks.static_checks("Bash", {"command": command})
    assert (result is not None and result[0] == 2) == denied, (command, result)


@pytest.mark.parametrize("command,denied", [
    ("git clean -fdxe -n", True), ("git clean -fdxe --dry-run", True), ("git clean -n --no-dry-run -fdx", True),
    ("git clean --dry-run --no-dry-run -fdx", True), ("git clean -n -- docs", True),
    ("git clean -ndx", False), ("git clean --dry-run -d -q", False)])
def test_git_clean_passes_only_a_provable_dry_run(world, command, denied):
    from yggdrasil import hooks
    result = hooks.static_checks("Bash", {"command": command})
    assert (result is not None and result[0] == 2) == denied, (command, result)
