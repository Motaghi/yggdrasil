"""A11 AC-85 row 3 and AC-93 row 3: the freeze at the trusted re-check doesn't depend on the order the tracker
lists accepted packages in, or on which packages the caller names (lane 4's trace, ed00db2)."""

import subprocess

import pytest

from yggdrasil import lanecheck


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo, name, text, message):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class _Issue:
    def __init__(self, identifier):
        self.id, self.records = identifier, []

    def get(self, key, default=None):
        return default


class _NoConfig:
    @staticmethod
    def get(key, default=None):
        return default


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """WP-2 accepted at S2 (adds scripts/a.py); then WP-1 accepted at S, a commit that changes scripts/a.py.
    The tracker lists WP-1 first."""
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    monkeypatch.setattr(lanecheck, "config", _NoConfig)
    monkeypatch.setattr(lanecheck, "denylist", lambda: [])
    monkeypatch.setattr(lanecheck, "_repo_entry", lambda project, repo_: None)
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: {})
    repo = tmp_path / "home"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "README.md", "x\n", "L0")
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl1")
    s2 = _commit(repo, "scripts/a.py", "a\n", "WP-2")
    s1 = _commit(repo, "scripts/a.py", "b\n", "WP-1")
    accepted = [("WP-1", "pm1tl1", s1, _Issue("WP-1")), ("WP-2", "pm1tl1", s2, _Issue("WP-2"))]
    monkeypatch.setattr(lanecheck, "accepted_packages", lambda project, repo_: list(accepted))
    return repo, s1, s2


def test_a_shared_path_belongs_to_the_package_accepted_first(lane):
    repo, _, _ = lane
    assert lanecheck.frozen_files("alpha-p1", str(repo)) == {"scripts/a.py": "WP-2"}


def test_merge_lane_rechecking_the_later_package_names_the_owner(lane):
    repo, s1, _ = lane
    problems = lanecheck.recheck_problems("alpha-p1", "pm1tl1", str(repo), ["WP-1"], s1)
    assert any("scripts/a.py belongs to accepted WP-2" in problem for problem in problems), problems


def test_the_owner_s_own_commits_are_not_a_freeze_fault(lane):
    repo, _, s2 = lane
    assert lanecheck.recheck_problems("alpha-p1", "pm1tl1", str(repo), ["WP-2"], s2) == []


def test_naming_both_packages_still_finds_the_change(lane):
    repo, s1, _ = lane
    problems = lanecheck.recheck_problems("alpha-p1", "pm1tl1", str(repo), ["WP-1", "WP-2"], s1)
    assert any("scripts/a.py belongs to accepted WP-2" in problem for problem in problems), problems
