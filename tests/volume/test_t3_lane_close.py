"""Merging a T3 lane through the real CLI removes its T3 project, then its worktree (A5.1)."""

import json
import os
import sys
from pathlib import Path

import pytest

_base = Path(__file__).resolve().parents[1]
for _folder in ("acceptance-a9", "acceptance-a10"):
    sys.path.insert(0, str(_base / _folder))

from driver_a10 import DriverA10


@pytest.mark.skipif(os.environ.get("ACCEPTANCE_TARGET", "real") != "real", reason="real target only")
def test_merging_a_t3_lane_removes_its_project_and_then_its_worktree(tmp_path):
    with DriverA10(tmp_path) as driver:
        driver.core_tree()
        driver.accepted_integration()
        driver.a5_t3(cli_version="0.1.9", server_version="0.1.9")
        assert driver.run("launch", "pm1tl1").returncode == 0
        driver.run("conductor", "tick")  # the fake thread reports completed: the T3 activation ends
        worktree = driver.repo_a / ".ygg-worktrees" / "alpha-p1" / "lane" / "pm1tl1"
        assert (worktree / ".ygg-managed").exists()
        known = json.loads((driver.state / "t3-projects.json").read_text())
        project = known[os.path.realpath(worktree)]["id"]
        result = driver.merge_lane()
        assert result.returncode == 0, result.stderr
        # A merged lane keeps its worktree while the project can still give it work; ready to merge ends that.
        record = driver.tracker / "projects/alpha-p1/project.json"
        state = json.loads(record.read_text())
        state["state"] = "ready-to-merge"
        record.write_text(json.dumps(state))
        tick = driver.run("conductor", "tick")
        assert tick.returncode == 0, tick.stderr
        removes = driver.a5_t3_calls("project remove")
        assert [call["argv"] for call in removes] == [["project", "remove", "--force", project]]
        assert not worktree.exists()
        listed = driver.git_result(driver.repo_a, "worktree", "list")
        assert str(worktree) not in listed.stdout
        fake = json.loads((Path(driver.fake_config).parent / "t3-state.json").read_text())
        assert project not in fake["projects"]
