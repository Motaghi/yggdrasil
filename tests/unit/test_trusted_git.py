import subprocess

import pytest

from yggdrasil import util


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    util._GLOBAL_CONFIG.clear()


def _repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _set(repo, key, value, *extra):
    util.trusted_git("-C", repo, "config", *extra, key, value)


def test_allowed_baseline_is_accepted(tmp_path):
    repo = _repo(tmp_path / "r")
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"),
                       ("remote.origin.url", "/srv/a.git"), ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
                       ("branch.main.remote", "origin"), ("branch.main.merge", "refs/heads/main"),
                       ("core.hooksPath", "scripts/hooks"), ("core.fsmonitor", "/bin/true"),
                       ("ygg.publicIdentity", "t <t@example.invalid>"), ("filter.lfs.smudge", "git-lfs smudge -- %f"),
                       ("filter.lfs.process", "git-lfs filter-process"), ("filter.lfs.required", "true"),
                       ("pull.rebase", "true"), ("pull.ff", "only"), ("core.autocrlf", "input"), ("core.eol", "lf"),
                       ("core.safecrlf", "warn"), ("core.whitespace", "trailing-space"),
                       ("init.defaultBranch", "main"), ("fetch.prune", "true"), ("merge.ff", "false"),
                       ("push.default", "simple"), ("color.ui", "auto"), ("color.diff.meta", "blue"),
                       ("alias.st", "status -sb"), ("submodule.lib.url", "/srv/lib.git"),
                       ("submodule.lib.path", "lib"), ("submodule.lib.branch", "main"),
                       ("core.untrackedCache", "true"),
                       ("branch.t3/x.gh-merge-base", "main"), ("branch.t3/x.t3-merge-base", "main"),
                       ("branch.t3/x.t3-merge-base-ref", "refs/heads/main"),
                       ("branch.t3/x.vscode-merge-base", "origin/main")):
        _set(repo, key, value)
    assert util.repository_helper(repo) is None


@pytest.mark.parametrize("key,value", [
    ("merge.evil.driver", "touch x"),                  # merge driver
    ("filter.evil.smudge", "touch x"),                 # filter
    ("filter.lfs.smudge", "touch x"),                  # LFS key with another value
    ("diff.img.textconv", "exiftool"),                 # diff program
    ("diff.external", "touch x"),                      # external diff
    ("gpg.program", "touch x"),                        # signing program
    ("core.sshCommand", "touch x"),                    # transport program
    ("protocol.ext.allow", "always"),                  # transport permission
    ("remote.origin.uploadpack", "touch x"),           # remote helper program
    ("remote.origin.vcs", "evil"),                     # remote helper
    ("extensions.partialClone", "origin"),             # lazy fetch
    ("core.gitProxy", "touch x"),                      # proxy program
    ("alias.st", "!touch x"),                          # shell alias
    ("alias.up", "  !touch x"),                        # shell alias after spaces
    ("submodule.lib.update", "!touch x"),              # submodule update command
    ("submodule.lib.fetchRecurseSubmodules", "yes"),   # any other submodule key
    ("core.attributesFile", "/tmp/attrs"),             # a core key off the list
    ("branch.main.evil-merge-base", "main"),           # look-alike of the tool merge-base keys
    ("branch.main.t3-merge-base.x", "main"),           # merge-base key with a trailing part
    ("include.path", "/tmp/other.cfg"),                # include
    ("includeIf.onbranch:main.path", "/tmp/other.cfg"),  # conditional include
])
def test_each_class_of_disallowed_key_is_refused(tmp_path, key, value):
    repo = _repo(tmp_path / "r")
    _set(repo, key, value)
    assert util.repository_helper(repo) == key.lower()


def test_helper_in_a_worktree_config_is_found(tmp_path):
    repo = _repo(tmp_path / "r")
    util.trusted_git("-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                     "commit", "-q", "--allow-empty", "-m", "x")
    lane = tmp_path / "lane"
    util.trusted_git("-C", repo, "worktree", "add", "-q", "-b", "lane", lane)
    _set(lane, "extensions.worktreeConfig", "true")
    _set(lane, "filter.evil.smudge", "touch x", "--worktree")
    assert util.repository_helper(repo) is None
    assert util.repository_helper(lane) == "filter.evil.smudge"


def test_refusal_names_the_repository_and_key(tmp_path):
    repo = _repo(tmp_path / "r")
    _set(repo, "merge.evil.driver", "touch x")
    with pytest.raises(util.YggError) as caught:
        util.helper_refusal(repo, "gate")
    assert caught.value.message == ("REFUSED: gate: " + str(repo) + " defines merge.evil.driver; "
                                    "Yggdrasil does not run repository-defined git helpers")


def test_environment_is_pinned(monkeypatch):
    for name in ("GIT_EXTERNAL_DIFF", "GIT_EXEC_PATH", "GIT_TEMPLATE_DIR", "GIT_DIR"):
        monkeypatch.setenv(name, "/tmp/x")
    environment = util.git_env()
    for name in ("GIT_EXTERNAL_DIFF", "GIT_EXEC_PATH", "GIT_TEMPLATE_DIR", "GIT_DIR"):
        assert name not in environment
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_ALLOW_PROTOCOL"] == ""
    assert environment["GIT_PROTOCOL_FROM_USER"] == "0"
    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    global_config = open(environment["GIT_CONFIG_GLOBAL"]).read()
    assert all(line.startswith(("[user]", "\tname", "\temail")) for line in global_config.splitlines())


def test_forced_settings_do_not_run(tmp_path):
    repo = _repo(tmp_path / "r")
    marker = tmp_path / "ran"
    script = tmp_path / "touch.sh"
    script.write_text("#!/bin/sh\ntouch " + str(marker) + "\nexit 1\n")
    script.chmod(0o755)
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\ntouch " + str(marker) + "\n")
    hook.chmod(0o755)
    for key in ("gpg.program", "core.editor", "core.pager", "core.sshCommand"):
        _set(repo, key, script)
    _set(repo, "commit.gpgSign", "true")
    result = util.trusted_git("-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                              "commit", "-q", "--allow-empty", "-m", "x")
    assert result.returncode == 0
    util.trusted_git("-C", repo, "checkout", "-q", "-b", "other")
    util.trusted_git("-C", repo, "log", "-1")
    assert not marker.exists()


def test_unreadable_config_fails_closed(tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / ".git" / "config").write_text("[core\n\tbroken")
    key = util.repository_helper(repo)
    assert key is not None and key.startswith("config (unreadable")
    with pytest.raises(util.YggError):
        util.helper_refusal(repo, "gate")


def test_old_git_is_refused(monkeypatch):
    monkeypatch.setitem(util._GIT_VERSION, "ok", False)
    monkeypatch.setitem(util._GIT_VERSION, "text", "git version 2.25.1")
    with pytest.raises(util.YggError) as caught:
        util.require_git_scopes()
    assert caught.value.message == "REFUSED: launch precondition: git 2.26 or later is required (found git version 2.25.1)"


def test_inherited_config_selectors_cannot_hide_repository_settings(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r")
    _set(repo, "merge.evil.driver", "touch x")
    empty = tmp_path / "empty.cfg"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    assert util.repository_helper(repo) == "merge.evil.driver"


def _home_with_file(tmp_path):
    home = _repo(tmp_path / "home")
    (home / "tracked.txt").write_text("tracked\n")
    util.trusted_git("-C", home, "add", "tracked.txt")
    util.trusted_git("-C", home, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                     "commit", "-q", "-m", "x")
    util.trusted_git("-C", home, "branch", "-M", "main")
    return home


def test_interrupted_worktree_creation_is_finished_on_reuse(tmp_path):
    from yggdrasil import launcher
    home = _home_with_file(tmp_path)
    lane = tmp_path / "lane"
    util.trusted_git("-C", home, "worktree", "add", "-q", "--no-checkout", "-b", "lane", lane, "main")
    assert not (lane / "tracked.txt").exists()
    launcher.ensure_worktree(str(home), lane, "lane", "main")
    assert (lane / "tracked.txt").read_text() == "tracked\n"
    assert util.trusted_git("-C", lane, "status", "--porcelain").stdout == ""


def test_interrupted_worktree_creation_never_overwrites_a_file(tmp_path):
    from yggdrasil import launcher
    home = _home_with_file(tmp_path)
    lane = tmp_path / "lane"
    util.trusted_git("-C", home, "worktree", "add", "-q", "--no-checkout", "-b", "lane", lane, "main")
    (lane / "tracked.txt").write_text("someone's work\n")
    with pytest.raises(util.YggError) as caught:
        launcher.ensure_worktree(str(home), lane, "lane", "main")
    assert "cannot finish checking out worktree" in caught.value.message
    assert (lane / "tracked.txt").read_text() == "someone's work\n"
