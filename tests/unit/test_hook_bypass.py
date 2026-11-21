import pytest

from yggdrasil.hooks import hardstop_category


@pytest.mark.parametrize("command,expected", [
    ("git push --no-verify origin ygg/alpha-p1/lane/pm1tl1", "hook-bypass"),
    ("git -c core.hooksPath=/dev/null push origin ygg/alpha-p1/lane/pm1tl1", "hook-bypass"),
    ("git -c core.hookspath=x status", "hook-bypass"),
    ("git -c alias.up='!git push' up origin ygg/alpha-p1/lane/pm1tl1", "hook-bypass"),
    ("git -c core.fsmonitor=./x.sh status", "hook-bypass"),
    ("git --config-env=core.hooksPath=HOOKS push origin ygg/a/lane/b", "hook-bypass"),
    ("git --config-env core.hooksPath=HOOKS push origin ygg/a/lane/b", "hook-bypass"),
    ("git config core.hooksPath /dev/null", "hook-bypass"),
    ("git config --worktree core.hooksPath /dev/null", "hook-bypass"),
    ("git config --unset core.hooksPath", "hook-bypass"),
    ("git config set alias.up '!git push'", "hook-bypass"),
    ("GIT_CONFIG_COUNT=1 git push origin ygg/a/lane/b", "hook-bypass"),
    ("export GIT_CONFIG_PARAMETERS=x", "hook-bypass"),
    ("git config core.hooksPath", None),
    ("git config --get core.hooksPath", None),
    ("git config --list", None),
    ("git config user.name Someone", None),
    ("git -c user.name=x commit -m y", None),
    ("git update-ref refs/heads/main HEAD", "ref-write"),
    ("git -C repo update-ref -d refs/heads/x", "ref-write"),
    ("git symbolic-ref HEAD refs/heads/main", "ref-write"),
    ("git symbolic-ref --delete HEAD", "ref-write"),
    ("git replace e5f6a7b b2c3d4e", "ref-write"),
    ("git replace -d e5f6a7b", "ref-write"),
    ("git symbolic-ref HEAD", None),
    ("git replace -l", None),
    ("git push origin ygg/alpha-p1/lane/pm1tl1", None),
])
def test_hook_bypass_and_ref_write(command, expected):
    assert hardstop_category(command) == expected


@pytest.mark.parametrize("command,expected", [
    ("git config --remove-section core", "hook-bypass"),
    ("git config --rename-section core saved", "hook-bypass"),
    ("git config --rename-section saved core", "hook-bypass"),
    ("git config remove-section alias", "hook-bypass"),
    ("git config core.hooksPath list", "hook-bypass"),
    ("git config core.hooksPath get", "hook-bypass"),
    ("git config --file .git/config core.hooksPath /dev/null", "hook-bypass"),
    ("git config include.path /tmp/other.cfg", "hook-bypass"),
    ("git config includeIf.gitdir:/x/.path /tmp/other.cfg", "hook-bypass"),
    ("git -c include.path=/tmp/other.cfg push origin ygg/a/lane/b", "hook-bypass"),
    ("git config --edit", "hook-bypass"),
    ("git config --remove-section user", None),
    ("git config get core.hooksPath", None),
    ("git config list", None),
    ("git config --get-regexp alias", None),
])
def test_config_actions_and_sections(command, expected):
    assert hardstop_category(command) == expected


@pytest.mark.parametrize("command,expected", [
    ("git config extensions.worktreeConfig false", "hook-bypass"),
    ("git config --unset extensions.worktreeConfig", "hook-bypass"),
    ("git config --remove-section extensions", "hook-bypass"),
    ("git config --rename-section extensions old", "hook-bypass"),
    ("git -c extensions.worktreeConfig=false push origin ygg/a/lane/b", "hook-bypass"),
    ("git config --get extensions.worktreeConfig", None),
])
def test_worktree_config_extension_is_guarded(command, expected):
    assert hardstop_category(command) == expected
