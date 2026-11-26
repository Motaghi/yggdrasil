"""git reads GIT_CEILING_DIRECTORIES as a list split on os.pathsep, so a box whose parent path holds that
separator (a programme home such as /x/repo:a) would get a ceiling that does not stop discovery, and git from
the box root would select the home repository. Such a builder launch is refused before its command line is
built."""
import os
import subprocess
from pathlib import Path

import pytest

from yggdrasil import launcher


def _data(box: Path, kind="lane", role="tl"):
    return {"node": "pm1tl1", "activation": "act-0007", "role": role, "cli": "codex", "box": str(box),
            "cwd": str(box / "checkout"), "box_kind": kind}


@pytest.mark.parametrize("kind", ["lane", "scratch"])
def test_a_builder_box_under_a_path_with_the_separator_is_refused(tmp_path, kind):
    box = tmp_path / ("repo" + os.pathsep + "a") / ".ygg-worktrees" / "alpha-p1" / "lane" / "pm1tl1.g000001"
    refusal = launcher.clone_refusal(_data(box, kind=kind))
    assert refusal and "GIT_CEILING_DIRECTORIES" in refusal and str(box.parent) in refusal


def test_a_builder_box_under_an_ordinary_path_is_not_refused(tmp_path):
    box = tmp_path / "repo" / ".ygg-worktrees" / "alpha-p1" / "lane" / "pm1tl1.g000001"
    assert launcher.clone_refusal(_data(box)) is None


def test_a_claude_launch_is_not_affected(tmp_path):
    box = tmp_path / ("repo" + os.pathsep + "a") / "box"
    data = _data(box)
    data["cli"] = "claude"
    assert launcher.clone_refusal(data) is None


def test_the_separator_really_defeats_the_ceiling(tmp_path):
    """Why the refusal exists: with the separator in the parent, git from the box root still finds the home."""
    home = tmp_path / ("repo" + os.pathsep + "a")
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    box = home / ".ygg-worktrees" / "alpha-p1" / "lane" / "pm1tl1.g000001"
    (box / ".git").mkdir(parents=True)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment["GIT_CEILING_DIRECTORIES"] = str(box.parent)
    found = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=box, env=environment, capture_output=True,
                           text=True)
    assert found.returncode == 0 and Path(found.stdout.strip()).resolve() == home.resolve()
