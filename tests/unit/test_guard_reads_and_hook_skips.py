"""Security review of ruling 129: read-only git can't read outside the worktree, and lane hooks can't be skipped."""

import json
import os
import subprocess

import pytest
from test_agent_permissions import _config, _git  # noqa: F401
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)

from yggdrasil import hooks


# ---------------------------------------------------------------- skipping the lane hooks

@pytest.mark.parametrize("command", [
    "git commit --no-verify -m x", "git commit -n -m x", "git commit -nm x", "git commit -anm x",
    "git commit -m x -n", "git commit --no-verif -m x", "git -C /tmp/w commit -n -m x",
    "git commit -a --no-verify", "git commit -vn -m x"])
def test_a_commit_that_skips_its_hooks_is_refused(command):
    assert hooks.hardstop_category(command) == "hook-bypass"


@pytest.mark.parametrize("command", [
    "git commit -m x", "git commit -am x", "git commit -F msg.txt", "git commit -m n", "git commit -mn",
    "git commit --amend --no-edit", "git commit -m x -- notes.md", "git commit -C HEAD"])
def test_an_ordinary_commit_is_not_a_hook_bypass(command):
    assert hooks.hardstop_category(command) is None


# ---------------------------------------------------------------- --no-index and --output anywhere

@pytest.mark.parametrize("command", [
    "git diff --no-index /etc/hosts /dev/null", "git diff --stat --no-index a b", "git diff a b --no-index",
    "git -C /tmp/w diff --no-index a b", "git diff --no-inde a b", "git grep --no-index secret",
    "git log --output=/tmp/x", "git show HEAD --output /tmp/x", "git diff --outpu=/tmp/x",
    "git log -p --output=x HEAD"])
def test_no_index_and_output_are_refused_in_every_position(command):
    assert hooks.static_checks("Bash", {"command": command}) is not None


@pytest.mark.parametrize("command", ["git log --oneline", "git diff --stat", "ygg send --body 'try --output'"])
def test_commands_without_those_options_are_not_refused_statically(command):
    assert hooks.static_checks("Bash", {"command": command}) is None


# ---------------------------------------------------------------- paths outside the worktree

@pytest.fixture
def agent(world, monkeypatch, tmp_path):
    """tl1 in a managed session whose recorded working folder is `worktree`, with a file outside it."""
    from yggdrasil import launcher
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "a.py").write_text("x\n")
    (tmp_path / "outside.txt").write_text("secret\n")
    # A lane worktree as launches make them: linked to a home repository, its git metadata outside the lane.
    home = tmp_path / "home-repo"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    _git(home, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "root")
    (worktree / "src").rename(tmp_path / "src-kept")
    worktree.rmdir()
    _git(home, "worktree", "add", "-q", "-b", "ygg/p1/lane/tl1", str(worktree))
    (tmp_path / "src-kept").rename(worktree / "src")
    git_dir, common_dir = launcher.lane_git_dirs(worktree)
    launcher.save_receipt("act-0042", {"node": "tl1", "activation": "act-0042", "cwd": str(worktree),
                                       "git_dir": git_dir, "git_common_dir": common_dir})
    monkeypatch.setenv("YGG_NODE", "tl1")
    monkeypatch.setenv("YGG_ACTIVATION", "act-0042")
    return worktree


def _pretool(command, cwd):
    return hooks.pretool(json.dumps({"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}))


@pytest.mark.parametrize("command", [
    # paths, bare or after --, and -C, --git-dir or --work-tree
    "git diff -- /etc/hosts", "git log -- ../outside.txt", "git show /etc/hosts", "git diff HEAD ../outside.txt",
    "git -C /tmp log", "git -C .. diff", "git blame ../outside.txt", "git log --follow -- ../../x",
    "git --git-dir=/tmp/other/.git log", "git --work-tree=/tmp diff", "git diff -- src/a.py", "git log -3 -- src",
    "git show HEAD:README.md",
    # an earlier cd or pushd in the same command
    "cd .. && git log", "cd /tmp; git diff", "pushd /tmp && git status", "true && git status",
    # git location variables, inline, through env or exported
    "GIT_DIR=/tmp/other/.git git log", "env GIT_DIR=/tmp/other/.git git log", "GIT_WORK_TREE=/tmp git diff",
    "GIT_INDEX_FILE=/tmp/i git diff --cached", "GIT_CEILING_DIRECTORIES=/ git status",
    # file-taking options with attached values
    "git blame --contents=/etc/hosts a.py", "git blame -S/etc/hosts a.py", "git blame --ignore-revs-file=/etc/x a",
    "git grep -f/etc/hosts", "git diff -O/etc/hosts", "git log --orderfile=/etc/hosts", "git diff --relative=..",
    # anything else that reads with git
    "git log -p", "git status -uall", "git branch", "git ls-files", "git cat-file -p HEAD", "git log | head",
    "git log $(cat x)", "git log > out.txt", "git diff src"])
def test_read_only_git_outside_the_allowed_shapes_is_refused(agent, command):
    code, message = _pretool(command, agent)
    assert code == 2, (command, message)


@pytest.mark.parametrize("command", [
    "GIT_DIR=/tmp/other/.git", "export GIT_DIR=/tmp/other/.git", "export GIT_WORK_TREE=/tmp",
    "env GIT_OBJECT_DIRECTORY=/tmp ls"])
def test_setting_a_git_location_variable_is_refused_on_its_own(command):
    assert hooks.static_checks("Bash", {"command": command}) is not None


@pytest.mark.parametrize("command", [
    "git status", "git status --short", "git status --porcelain", "git diff", "git diff --cached",
    "git diff --stat HEAD", "git diff --name-only main..HEAD", "git diff HEAD~2", "git log", "git log --oneline",
    "git log --oneline -n 3", "git log -n 5 main", "git log --oneline -n 3 ygg/p1/lane/tl1", "git show HEAD",
    "git show --stat HEAD~1", "git rev-parse HEAD", "git rev-parse --abbrev-ref HEAD", "git rev-parse --show-toplevel",
    "git branch --list", "git branch --list ygg/*"])
def test_the_allowed_read_only_shapes_pass(agent, command):
    assert _pretool(command, agent) == (0, ""), command


def test_from_a_subfolder_of_the_worktree_a_shape_is_allowed(agent):
    assert _pretool("git status --short", agent / "src") == (0, "")


def test_a_shape_run_from_outside_the_worktree_is_refused(agent, tmp_path):
    assert _pretool("git status", tmp_path)[0] == 2


def test_a_revision_that_names_an_existing_file_is_refused(agent):
    assert _pretool("git diff src", agent)[0] == 2
    assert _pretool("git log main", agent) == (0, "")


def test_without_a_readable_receipt_a_git_read_fails_closed(agent, monkeypatch):
    monkeypatch.setenv("YGG_ACTIVATION", "act-0099")
    code, _ = _pretool("git status", agent)
    assert code == 2


def test_writes_are_not_held_to_the_read_shapes(agent):
    assert _pretool("git add -- src/a.py", agent) == (0, "")
    assert _pretool("git commit -m x", agent) == (0, "")


# ---------------------------------------------------------------- the push guard runs the lane check

@pytest.fixture
def pushed(world, monkeypatch, tmp_path):
    """A repository with main and a lane branch, the push guard's PID checks stubbed to pass."""
    from yggdrasil import gitops
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid")):
        _git(repo, "config", key, value)
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty", "-m", "root")
    main = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    _git(repo, "checkout", "-q", "-b", "ygg/p1/lane/tl1")
    monkeypatch.setattr(gitops, "_repo_entry", lambda project, repo_: {"path": str(repo)})
    monkeypatch.setattr(gitops, "check_url", lambda *a, **k: None)
    monkeypatch.setattr(gitops, "check_caller", lambda *a, **k: None)
    monkeypatch.setattr(gitops, "trusted_base", lambda project, repo_: main)
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("zebra-orchid-7\n")
    return repo


def _skip_hooks_commit(repo, name, text, message):
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text)
    _git(repo, "add", "--", name)
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", message)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def _push(repo, sha):
    from yggdrasil import gitops
    line = "refs/heads/ygg/p1/lane/tl1 " + sha + " refs/heads/ygg/p1/lane/tl1 " + gitops.ZERO + "\n"
    return gitops.push_guard(str(repo), "origin", line)


def test_the_push_guard_passes_a_clean_lane(pushed):
    assert _push(pushed, _skip_hooks_commit(pushed, "notes.md", "hello\n", "add notes")) == (0, "")


@pytest.mark.parametrize("name,text,message,why", [
    ("notes.md", "zebra-orchid-7\n", "add", "denylist"),
    (".github/workflows/ci.yml", "x\n", "ci", "workflow"),
    ("notes.md", "hello\n", "add\n\nCo-Authored-By: Model <noreply@example.invalid>\n", "trailers are not allowed")])
def test_the_push_guard_refuses_a_commit_whose_hooks_were_skipped(world, pushed, tmp_path, name, text, message, why):
    # The denylist comes from the profile only (A11 AC-31).
    _config(world, "commit_identity:\n  trailers: none\nleak_denylist: " + str(tmp_path / "denylist.txt") + "\n")
    first = _skip_hooks_commit(pushed, "ok.md", "fine\n", "fine")
    assert first
    sha = _skip_hooks_commit(pushed, name, text, message)
    code, reason = _push(pushed, sha)
    assert code != 0 and why in reason, reason
