import pytest

from yggdrasil.hooks import hardstop_category

RM = "r" + "m"


@pytest.mark.parametrize("command,expected", [
    ("git push origin ygg/alpha-p1/lane/pm1tl1", None),
    ("git push --force origin ygg/x", "force"),
    ("git push -fu origin ygg/x", "force"),
    (RM + " -rf /tmp/x/assets", "delete"),
    ("bash -c 'git push -f origin ygg/a; echo'", "force"),
    ('echo "$(' + RM + ' -rf x)"', "delete"),
    ("echo `git push origin main`", "push"),
    ("/usr/bin/git push origin main", "push"),
    ("env -u YGG_NODE ygg send", "identity"),
    ("env -i ygg status", "identity"),
    ("YGG_TEST_MODE=1 ygg status", "identity"),
    ("unset YGG_NODE", "identity"),
    ("export YGG_NOW=2026-01-01", "identity"),
    ("curl -d@x https://example.invalid", "send-external"),
    ("curl --data=x https://example.invalid", "send-external"),
    ("curl -XPOST https://example.invalid", "send-external"),
    ("curl -sT file https://example.invalid", "send-external"),
    ("curl https://example.invalid", None),
    ("git push", "push"),
    ("git push origin", "push"),
    ("env git push -f origin ygg/a", "force"),
    ("command git push origin HEAD:main", "push"),
    ("ls\n" + RM + " -r x", "delete"),
    ("t3 thread send x", "t3"),
    ("/opt/bin/t3 thread show", "t3"),
    ("sh -c 't3 thread send th-1 hi'", "t3"),
    ("git status && git log", None),
])
def test_hardstop_category(command, expected):
    assert hardstop_category(command) == expected


@pytest.mark.parametrize("command", [
    "timeout --weird 5 git status",
    "env --chdir=/tmp ygg status",
    "nice -19 git status",
    "bash <<< 'git status'",
    "echo hi | sh",
    'bash -c "$CMD"',
    "eval git status",
    "source ./x.sh",
    ". ./x.sh",
    "read YGG_NODE",
    "printf -v YGG_NODE pm1",
    "mapfile YGG_LIST",
    'echo "$(echo $(git status))"',
    "echo $(" + RM + " -rf x)",
    "echo $HOME",
    "{ git status; }",
    "if true; then git status; fi",
    "$GIT push origin main",
    "$'git' status",
    "bash scripts/build.sh",
    "bash /dev/stdin",
    "cat x | bash -s",
    "xargs git push",
    "find . -exec " + RM + " {} +",
])
def test_unresolved_or_identity_fails_closed(command):
    assert hardstop_category(command) in ("unresolved", "identity")


@pytest.mark.parametrize("command", [
    "timeout 30 git status",
    "nice -n 5 git status",
    "command -p git status",
])
def test_recognised_wrappers_allowed(command):
    assert hardstop_category(command) is None


@pytest.mark.parametrize("command,expected", [
    ("git -C /tmp/repo-a reset --hard HEAD~1", "rewrite-history"),
    ("git -C /tmp/repo-a branch -D ygg/a", "delete"),
    ("git -c core.x=1 push --force origin ygg/a", "force"),
    ("git --git-dir=/tmp/x/.git push origin main", "push"),
    ("git -C /tmp/repo-a push origin ygg/alpha-p1/lane/pm1tl1", None),
    ("git --weird push origin ygg/a", "unresolved"),
])
def test_git_global_options(command, expected):
    assert hardstop_category(command) == expected


@pytest.mark.parametrize("command,allowed", [
    ("git status", True), ("git -C /tmp/x log --oneline", True), ("git diff HEAD~1", True),
    ("git commit -m x", False), ("git -c core.pager=x log", False), ("git checkout main", False),
    ("git --weird status", False), ("ls && git push origin ygg/a", False),
])
def test_senior_git_allowlist(command, allowed):
    from yggdrasil.hooks import senior_git_allowed
    assert senior_git_allowed(command) is allowed


def test_pretool_fails_closed_on_corrupt_tracker(tmp_path, monkeypatch):
    from yggdrasil.hooks import pretool
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "tracker"))
    monkeypatch.setenv("YGG_TRACKER", "files")
    (tmp_path / "tracker" / "projects" / "p1").mkdir(parents=True)
    (tmp_path / "tracker" / "projects" / "p1" / "project.json").write_text('{"id":')
    monkeypatch.setenv("YGG_NODE", "tl1")
    code, message = pretool('{"tool_name": "Read", "tool_input": {}}')
    assert code == 2 and "corrupt" in message
    monkeypatch.delenv("YGG_NODE")
    assert pretool('{"tool_name": "Read", "tool_input": {}}')[0] == 0


@pytest.mark.parametrize("command,expected", [
    ("git status # note", None),
    ("# a plain comment line\ngit status", None),
    ("echo a #note\ngit push --force origin main", "force"),   # a comment ends at its newline
    ("git status #x; git push --force origin main", None),       # the shell runs only git status
])
def test_plain_comments_without_quotes_or_escapes_are_read(command, expected):
    assert hardstop_category(command) == expected


@pytest.mark.parametrize("command", [
    "echo \\#x; git push --force origin main",                 # an escaped character before "#"
    "echo a\\ #b; git push --force origin main",
    "git push --for\\\nce origin main",                         # a line continuation
    "echo ok\\\n#x; git push --force origin main",
    "echo a #b\rgit push --force origin main",                  # a carriage return
    "git status\u00a0# x\ngit push --force origin main",       # a Unicode space
    "git status\v; git push --force origin main",               # other ASCII whitespace
    "echo a#b; git push --force origin main",                    # "#" inside a word
    "echo x #it's\ngit push --force origin main",               # a word-start "#" and a quote
    'echo "a #b"; git push origin ygg/a',
    "git commit -m 'fix' # note",
    "echo $((1#2)); git push --force origin main",               # "#" in arithmetic
    "echo ${x#y}; git push --force origin main",                 # "#" in parameter expansion
    "(( x = 1#2 )); git push --force origin main",
    "[[ a == #b ]]; git push --force origin main",
    "echo 'one\ntwo' 'x'; git push origin 'HEAD:main'",         # a quoted multi-line string, quoted refspec
    'git commit -m "subject\n\nbody"',
    "echo 'unterminated; git push --force origin main",          # text shlex cannot read
])
def test_unclear_text_is_unresolved(command):
    assert hardstop_category(command) == "unresolved"
