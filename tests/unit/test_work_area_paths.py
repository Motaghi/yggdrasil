import os
import subprocess

import pytest

from yggdrasil import launcher
from yggdrasil.util import YggError


def _repo(path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "first"], check=True)


def test_existing_worktree_of_the_right_branch_is_reused(tmp_path):
    home = tmp_path / "home"
    _repo(home)
    path = home / ".ygg-worktrees" / "p" / "lane" / "a"
    assert launcher.ensure_worktree(str(home), path, "ygg/p/lane/a", "main") == path
    assert launcher.ensure_worktree(str(home), path, "ygg/p/lane/a", "main") == path


def test_foreign_checkout_or_wrong_branch_is_refused(tmp_path):
    home, other = tmp_path / "home", tmp_path / "other"
    _repo(home)
    _repo(other)
    foreign = home / ".ygg-worktrees" / "p" / "lane" / "a"
    foreign.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(other), str(foreign)], check=True)
    with pytest.raises(YggError) as caught:
        launcher.ensure_worktree(str(home), foreign, "ygg/p/lane/a", "main")
    assert "is not the worktree of ygg/p/lane/a" in caught.value.message
    wrong = home / ".ygg-worktrees" / "p" / "lane" / "b"
    launcher.ensure_worktree(str(home), wrong, "ygg/p/lane/b", "main")
    with pytest.raises(YggError):
        launcher.ensure_worktree(str(home), wrong, "ygg/p/lane/c", "main")
    assert os.path.isdir(wrong)
