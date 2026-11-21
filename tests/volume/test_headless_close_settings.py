"""A repository that tracks its own .claude/settings.json: a merged headless lane still loses its worktree, the
file going with it through git (follow-ups review round 1)."""

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


def test_a_merged_headless_lane_with_tracked_settings_loses_its_worktree(tmp_path):
    with DriverA10(tmp_path) as driver:
        driver.core_tree()
        settings = driver.repo_a / ".claude/settings.json"
        driver.write(settings, "{}\n")
        driver.git(driver.repo_a, "add", ".claude/settings.json")
        driver.git(driver.repo_a, "commit", "-qm", "project settings")
        assert driver.ready().returncode == 0
        driver.package(criteria=True, authorise=True)
        launch = driver.run("launch", "pm1tl1")
        assert launch.returncode == 0, launch.stderr
        driver.wait_exit("pm1tl1")
        worktree = driver.repo_a / ".ygg-worktrees/alpha-p1/lane/pm1tl1"
        assert (worktree / ".claude/settings.json").exists()
        issue = driver.tracker / "projects/alpha-p1/issues/WP-41.md"
        text = issue.read_text()
        assert "state: authorised" in text
        issue.write_text(text.replace("state: authorised", "state: merged"))
        # The project gives the lane no more work (ready to merge): only then does a merged lane lose its worktree.
        record = driver.tracker / "projects/alpha-p1/project.json"
        project = json.loads(record.read_text())
        project["state"] = "ready-to-merge"
        record.write_text(json.dumps(project))
        for _ in range(2):
            result = driver.run("conductor", "tick")
            assert result.returncode == 0, result.stderr
        assert not worktree.exists(), {
            "status": driver.git_result(worktree, "status", "--porcelain").stdout,
            "settings_exists": (worktree / ".claude/settings.json").exists()}
