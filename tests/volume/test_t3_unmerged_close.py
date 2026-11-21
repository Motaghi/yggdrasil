"""A close that keeps an unmerged worktree removes nothing in T3 either: its project and thread stay (landing
review round 2)."""

import json
import os
import sys
from pathlib import Path

import pytest

_base = Path(__file__).resolve().parents[1]
for _folder in ("acceptance-a9", "acceptance-a10"):
    sys.path.insert(0, str(_base / _folder))

from driver_a10 import DriverA10

pytestmark = pytest.mark.skipif(os.environ.get("ACCEPTANCE_TARGET", "real") != "real", reason="real target only")


@pytest.mark.parametrize("close", ["lane", "project"])
def test_an_unmerged_close_keeps_the_t3_project_and_thread(tmp_path, close):
    with DriverA10(tmp_path) as driver:
        driver.core_tree()
        driver.ready()
        driver.a5_t3(cli_version="0.1.9", server_version="0.1.9")
        launched = driver.run("launch", "pm1tl1")
        assert launched.returncode == 0, launched.stderr
        assert driver.run("conductor", "tick").returncode == 0
        receipt = driver.a5_receipts("pm1tl1")[0]
        assert receipt.get("exited_at") and receipt.get("exit_code") == 0
        worktree = driver.repo_a / ".ygg-worktrees/alpha-p1/lane/pm1tl1"
        driver.write(worktree / "unmerged.txt", "unmerged work\n")
        driver.git(worktree, "add", "unmerged.txt")
        driver.git(worktree, "-c", "user.name=review", "-c", "user.email=review@example.invalid",
                   "commit", "-qm", "unmerged work")
        tip = driver.git(worktree, "rev-parse", "HEAD").stdout.strip()
        assert driver.git_result(driver.repo_a, "merge-base", "--is-ancestor", tip, "main").returncode == 1
        assert driver.git(worktree, "status", "--porcelain").stdout == ""
        remote_path = tmp_path / "t3-state.json"
        before = json.loads(remote_path.read_text())
        thread = receipt["t3_thread"]
        assert thread in before["threads"]
        project = next(key for key, value in before["projects"].items() if value["path"] == str(worktree))
        if close == "project":
            result = driver.run("project", "close", "alpha-p1", "--premature", "--note", "stopped")
            assert result.returncode == 0, result.stderr
        else:
            driver.package(criteria=True, authorise=True)
            issue = driver.tracker / "projects/alpha-p1/issues/WP-41.md"
            text = issue.read_text()
            assert "state: authorised" in text
            issue.write_text(text.replace("state: authorised", "state: merged"))
            assert driver.run("conductor", "tick").returncode == 0
        after = json.loads(remote_path.read_text())
        assert worktree.exists() and (worktree / "unmerged.txt").read_text() == "unmerged work\n"
        observed = {"worktree_preserved": worktree.exists(), "project_preserved": project in after["projects"],
                    "thread_preserved": thread in after["threads"],
                    "removes": [call["argv"] for call in driver.a5_t3_calls("project remove")]}
        assert project in after["projects"] and thread in after["threads"], observed
