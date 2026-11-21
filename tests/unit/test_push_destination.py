import os
import subprocess

from yggdrasil.gitops import git_env, normal_url, remote_urls


def test_normal_url_resolves_local_paths_and_keeps_network_urls(tmp_path):
    target = tmp_path / "remote.git"
    target.mkdir()
    link = tmp_path / "link.git"
    link.symlink_to(target)
    assert normal_url(str(link)) == os.path.realpath(target)
    assert normal_url("file://" + str(target)) == os.path.realpath(target)
    assert normal_url("https://example.invalid/org/repo.git/") == "https://example.invalid/org/repo.git"
    assert normal_url("git@example.invalid:org/repo.git") == "git@example.invalid:org/repo.git"


def test_remote_urls_lists_effective_fetch_and_push_urls(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "/srv/a.git"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "--push", "origin", "/srv/b.git"], check=True)
    assert remote_urls(repo) == {"origin": [os.path.realpath("/srv/a.git"), os.path.realpath("/srv/b.git")]}


def test_remote_urls_apply_url_rewrites_and_resolve_relative_paths(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/org/a.git"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "url.git@example.invalid:.pushInsteadOf",
                    "https://example.invalid/"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "sibling", "../repo-a"], check=True)
    found = remote_urls(repo)
    assert found["origin"] == ["git@example.invalid:org/a.git", "https://example.invalid/org/a.git"]
    assert found["sibling"] == [os.path.realpath(tmp_path / "repo-a")]


def test_relative_destination_has_no_fixed_meaning():
    assert normal_url("../repo-a") is None


def test_git_env_drops_repository_selection(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    environment = git_env()
    assert "GIT_DIR" not in environment
    assert "GIT_CONFIG_KEY_0" not in environment
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
