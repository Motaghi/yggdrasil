"""After a T3 session, a headless launch in the same (now marked) worktree still starts its agent."""

import os

import pytest

from driver import Driver


@pytest.mark.skipif(os.environ.get("ACCEPTANCE_TARGET", "real") != "real", reason="real target only")
def test_headless_launch_after_a_t3_activation_in_the_same_worktree(tmp_path):
    with Driver(tmp_path) as driver:
        driver.core_tree()
        driver.a5_t3(cli_version="0.1.9", server_version="0.1.9")
        driver.ready()
        assert driver.run("launch", "pm1tl1").returncode == 0
        assert driver.a5_t3_calls("thread start")
        driver.run("conductor", "tick")  # the fake thread reports completed: the T3 activation ends
        worktree = driver.repo_a / ".ygg-worktrees" / "alpha-p1" / "lane" / "pm1tl1"
        assert (worktree / ".ygg-managed").exists()
        driver.env.pop("YGG_T3_BIN")
        driver.clear_calls()
        result = driver.run("launch", "pm1tl1")
        assert result.returncode == 0, result.stderr
        driver.wait_exit("pm1tl1", timeout=30)
        launches = driver.launches("pm1tl1")
        assert len(launches) == 1 and launches[0]["binary"] == "claude"
