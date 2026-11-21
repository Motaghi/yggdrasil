import pytest


@pytest.mark.ac("AC-06")
def test_managed_raw_t3_tool_denied(tree):
    result = tree.hook({"tool_name": "mcp__t3-code__t3_thread_send", "tool_input": {}}, node="pm1tl1")
    assert result.returncode == 2
    assert "ygg send" in result.stderr


@pytest.mark.ac("AC-06")
def test_managed_bash_t3_denied(tree):
    result = tree.hook({"tool_name": "Bash", "tool_input": {"command": 't3 thread send th-9 "hi"'}}, node="pm1")
    assert result.returncode == 2


@pytest.mark.ac("AC-06")
def test_unmanaged_raw_t3_tool_allowed(tree):
    result = tree.hook({"tool_name": "mcp__t3-code__t3_thread_send", "tool_input": {}})
    assert result.returncode == 0


@pytest.mark.ac("AC-07")
def test_first_cockpit_attach(tree):
    result = tree.run("attach", "--session", "s-alpha", env={"YGG_NOW": "2026-11-16T09:00:00+01:00"})
    assert result.returncode == 0
    assert "s-alpha" in tree.state_text("l1.lock")


@pytest.mark.ac("AC-07")
def test_live_cockpit_lock_refused(tree):
    tree.run("attach", "--session", "s-alpha", env={"YGG_NOW": "2026-11-16T09:00:00+01:00"})
    result = tree.run("attach", "--session", "s-beta", env={"YGG_NOW": "2026-11-16T09:05:00+01:00"})
    assert result.returncode == 5
    assert "s-alpha" in result.stderr


@pytest.mark.ac("AC-07")
def test_expired_cockpit_lock_replaced(tree):
    tree.run("attach", "--session", "s-alpha", env={"YGG_NOW": "2026-11-16T09:00:00+01:00"})
    result = tree.run("attach", "--session", "s-beta", env={"YGG_NOW": "2026-11-16T09:45:00+01:00"})
    assert result.returncode == 0
    assert "s-beta" in tree.state_text("l1.lock")
    assert "s-alpha" in tree.state_text("alerts.jsonl")


@pytest.mark.ac("AC-43")
def test_managed_clock_override_ignored(tree):
    status = tree.status(env={"YGG_NODE": "pm1", "YGG_NOW": "2026-11-16T02:00:00+01:00", "YGG_TEST_MODE": None})
    assert isinstance(status, dict)
    assert status["clock_source"] == "system"


@pytest.mark.ac("AC-43")
def test_user_clock_override_honoured(tree):
    status = tree.status(env={"YGG_NODE": None, "YGG_NOW": "2026-11-16T02:00:00+01:00"})
    assert isinstance(status, dict)
    assert status["clock_source"] == "override"
    assert status["generated_at"] == "2026-11-16T01:00:00Z"


@pytest.mark.ac("AC-44")
@pytest.mark.parametrize("command,category", [
    ("git push --force origin ygg/alpha-p1/lane/pm1tl1", "force"),
    ("rm -rf {repo}/assets", "delete"),
], ids=["force", "delete"])
def test_hardstop_bash_denied(tree, command, category):
    cwd = tree.make_cwd("repo-a/.ygg-worktrees/alpha-p1/lane/pm1tl1")
    result = tree.hook({"tool_name": "Bash", "tool_input": {"command": command.format(repo=tree.repo_a)}},
                       node="pm1tl1", cwd=cwd)
    assert result.returncode == 2
    assert category in result.stderr


@pytest.mark.ac("AC-44")
def test_lane_push_bash_allowed(tree):
    cwd = tree.make_cwd("repo-a/.ygg-worktrees/alpha-p1/lane/pm1tl1")
    result = tree.hook({"tool_name": "Bash", "tool_input": {"command": "git push origin ygg/alpha-p1/lane/pm1tl1"}},
                       node="pm1tl1", cwd=cwd)
    assert result.returncode == 0
