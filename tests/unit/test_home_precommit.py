import subprocess

from yggdrasil import launcher


def _git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *map(str, args)],
                          cwd=cwd, capture_output=True, text=True)


def test_home_precommit_found_through_include_and_separate_git_dir(tmp_path):
    home, meta, lane, ours = tmp_path / "home", tmp_path / "meta", tmp_path / "lane", tmp_path / "ygghooks"
    _git("init", "-q", "--separate-git-dir", meta, home)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=home)
    hooks = home / "scripts" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\necho home pre-commit ran >&2\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    included = tmp_path / "included.cfg"
    included.write_text("[core]\n\thooksPath = scripts/hooks\n")
    _git("config", "include.path", included, cwd=home)
    _git("worktree", "add", "-q", lane, "-b", "lane", cwd=home)
    ours.mkdir()
    (ours / "pre-commit").write_text(launcher.HOME_PRE_COMMIT)
    (ours / "pre-commit").chmod(0o755)
    _git("config", "extensions.worktreeConfig", "true", cwd=lane)
    _git("config", "--worktree", "core.hooksPath", ours, cwd=lane)
    _git("config", "--worktree", "ygg.home", home, cwd=lane)
    result = _git("commit", "--allow-empty", "-m", "x", cwd=lane)
    assert result.returncode == 1
    assert "home pre-commit ran" in result.stderr


def test_no_home_precommit_lets_the_commit_through(tmp_path):
    home, lane, ours = tmp_path / "home", tmp_path / "lane", tmp_path / "ygghooks"
    _git("init", "-q", home)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=home)
    _git("worktree", "add", "-q", lane, "-b", "lane", cwd=home)
    ours.mkdir()
    (ours / "pre-commit").write_text(launcher.HOME_PRE_COMMIT)
    (ours / "pre-commit").chmod(0o755)
    _git("config", "extensions.worktreeConfig", "true", cwd=lane)
    _git("config", "--worktree", "core.hooksPath", ours, cwd=lane)
    assert _git("commit", "--allow-empty", "-m", "x", cwd=lane).returncode == 0
