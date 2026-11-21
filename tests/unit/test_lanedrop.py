"""A11.1 lane 1: the lane records (creation, syncs, drops) and the history guard (AC-12)."""

import subprocess

import pytest

from yggdrasil import lanedrop


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    _git(home, "commit", "-q", "--allow-empty", "-m", "L0")
    return home


def test_creation_is_recorded_once(repo):
    first = _git(repo, "rev-parse", "HEAD")
    assert lanedrop.creation("alpha-p1", "pm1tl1") is None
    lanedrop.record_creation("alpha-p1", "pm1tl1", first)
    lanedrop.record_creation("alpha-p1", "pm1tl1", "0" * 40)
    assert lanedrop.creation("alpha-p1", "pm1tl1") == first


def test_records_survive_an_emptied_state_root(repo, tmp_path):
    import shutil
    lanedrop.record_creation("alpha-p1", "pm1tl1", _git(repo, "rev-parse", "HEAD"))
    shutil.rmtree(tmp_path / "state", ignore_errors=True)
    assert lanedrop.creation("alpha-p1", "pm1tl1")


def test_the_creation_commit_is_the_drop_point_without_packages(repo, monkeypatch):
    from yggdrasil import lanecheck
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    l0 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl1")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "C1")
    lanedrop.record_creation("alpha-p1", "pm1tl1", l0)
    assert lanedrop.drop_point("alpha-p1", "pm1tl1", str(repo)) == l0


def test_a_dropped_commit_and_its_descendants_are_refused(repo):
    l0 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "C1")
    c1 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "C2")
    c2 = _git(repo, "rev-parse", "HEAD")
    plan = {"packages": ["WP-1"], "lane": "pm1tl1", "head": c2, "drop_point": l0, "dropped": [c2, c1]}
    identifier = lanedrop.record("alpha-p1", plan)
    assert lanedrop.interrupted("alpha-p1", "pm1tl1")["id"] == identifier
    assert lanedrop.history_problem("alpha-p1", str(repo), c1) == "the lane can't go back to the work of cancelled WP-1"
    assert lanedrop.history_problem("alpha-p1", str(repo), c2)
    assert lanedrop.history_problem("alpha-p1", str(repo), l0) is None
    lanedrop.complete("alpha-p1", identifier)
    assert lanedrop.interrupted("alpha-p1", "pm1tl1") is None
    assert lanedrop.summary(plan).startswith("WP-1 cancelled; lane pm1tl1 reset to " + l0[:7] + "; 2 commits dropped: ")
