import subprocess

import pytest

from yggdrasil.hooks import hardstop_category


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global.gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    monkeypatch.chdir(path)
    return path


def _alias(repo, name, value, *scope):
    subprocess.run(["git", "-C", str(repo), "config", *scope, "alias." + name, value], check=True)


# Aliases are never resolved: whatever they expand to, the call is unresolved and refused with guidance.
@pytest.mark.parametrize("name,value", [
    ("p", "push --force origin HEAD:main"),   # a hard stop behind an alias
    ("x", "!git push --force"),               # a shell alias
    ("st", "status -sb"),                     # even a harmless one
])
def test_any_alias_is_unresolved(repo, name, value):
    _alias(repo, name, value)
    assert hardstop_category("git " + name) == "unresolved"
    assert hardstop_category("git -C " + str(repo) + " " + name) == "unresolved"


def test_alias_from_global_config_is_unresolved(repo):
    _alias(repo, "ship", "push origin HEAD:main", "--global")
    assert hardstop_category("git ship") == "unresolved"


@pytest.mark.parametrize("command", [
    "git frobnicate --all",                   # neither a built-in nor a classified script
    "git lfs push origin main",               # an external git-<name> program
    "git --exec-path=/tmp/x status",
    "git --exec-path /tmp/x status",
    "PATH=/tmp/x:$PATH git status",
    "PATH=/tmp/x git status",
    "GIT_EXEC_PATH=/tmp/x git status",
    "env PATH=/tmp/x git push origin ygg/a",
    "export PATH=/tmp/x; git status",
    "export GIT_EXEC_PATH=/tmp/x && git status",
    "readonly PATH=/tmp/x; git status",
    "readonly GIT_EXEC_PATH=/tmp/x; git status",
    "printf -v PATH %s /tmp/x; git status",
    "printf -v GIT_EXEC_PATH %s /tmp/x; git status",
    "declare -x PATH=/tmp/x; git status",
    "typeset GIT_EXEC_PATH=/tmp/x; git status",
    "local PATH=/tmp/x; git status",
    "read PATH < /tmp/x; git status",
    "declare -n p=PATH; p=/tmp/x; git status",
    "PATH+=:/tmp/x; git status",
    "bash -c 'readonly PATH=/tmp/x; git status'",
    "unset PATH; git status",
    "P\\ATH=/tmp/x git status",             # a backslash inside the name
    "export \"PA\"TH=/tmp/x; git status",     # quotes inside the name
    "export 'GIT_EXEC'_PATH=/tmp/x; git status",
    "readonly ${PATH}; git status",           # braces
    "git add docs/PATH.md",                   # the name anywhere refuses: fail closed
])
def test_git_calls_the_hook_cannot_resolve(repo, command):
    assert hardstop_category(command) == "unresolved"


@pytest.mark.parametrize("command,expected", [
    ("git status", None),
    ("git -C /tmp/repo-a log --oneline", None),
    ("git push --force origin ygg/a", "force"),
    ("git filter-branch --all", "rewrite-history"),
    ("FOO=1 git status", None),
    ("export PATH=/tmp/x", None),             # no git call in the command
])
def test_built_ins_keep_their_classification(repo, command, expected):
    assert hardstop_category(command) == expected
