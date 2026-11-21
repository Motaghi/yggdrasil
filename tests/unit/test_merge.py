import subprocess

import pytest

from yggdrasil import gitops, util


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "t")
    git(path, "config", "user.email", "t@example.invalid")
    (path / "a.txt").write_text("a\n")
    git(path, "add", "a.txt")
    git(path, "-c", "core.hooksPath=/dev/null", "commit", "-m", "base")
    git(path, "checkout", "-b", "feature")
    (path / "b.txt").write_text("b\n")
    git(path, "add", "b.txt")
    git(path, "-c", "core.hooksPath=/dev/null", "commit", "-m", "feature")
    feature = git(path, "rev-parse", "HEAD")
    git(path, "checkout", "main")
    return path, feature


def test_checked_out_main_merges_in_place(repo):
    path, feature = repo
    new = gitops._merge_into(path, "main", feature, "merge", no_ff=True)
    assert git(path, "rev-parse", "HEAD") == new
    assert (path / "b.txt").exists()
    assert git(path, "status", "--porcelain") == ""


def test_dirty_checked_out_main_is_refused(repo):
    path, feature = repo
    (path / "a.txt").write_text("changed\n")
    before = git(path, "rev-parse", "main")
    with pytest.raises(util.YggError) as error:
        gitops._merge_into(path, "main", feature, "merge", no_ff=True)
    assert error.value.code == 3
    assert git(path, "rev-parse", "main") == before


def test_unchecked_out_branch_merges_in_temporary_worktree(repo):
    path, feature = repo
    git(path, "checkout", "--detach")
    new = gitops._merge_into(path, "main", feature, "merge", no_ff=True)
    assert git(path, "rev-parse", "main") == new
    assert len(git(path, "worktree", "list").splitlines()) == 1
