"""Lane 1, Codex review round 4 (a11r4-lane1): recorded syncs are taken in order; equality with an older sync's
content never takes a lane change out of the package's file set."""

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
    def __init__(self, identifier, base, at):
        self.id = identifier
        self.records = [{"kind": "review-opened", "base": base},
                        {"kind": "acceptance", "verdict": "accept", "at": at}]

    def get(self, key, default=None):
        return default


class _NoConfig:
    @staticmethod
    def get(key, default=None):
        return default


@pytest.fixture
def two_syncs(tmp_path, monkeypatch):
    """Base B holds x.py = x0; two clean recorded syncs import x1, then x2; WP-1's lane change takes x.py back
    to x1 and is accepted at S1."""
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    monkeypatch.setattr(lanecheck, "config", _NoConfig)
    monkeypatch.setattr(lanecheck, "denylist", lambda: [])
    monkeypatch.setattr(lanecheck, "_repo_entry", lambda project, repo_: None)
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    repo = tmp_path / "home"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    base = _commit(repo, "scripts/x.py", "x0\n", "B")
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl1")
    syncs = {}
    for version in ("x1", "x2"):
        _git(repo, "checkout", "-q", "main")
        main = _commit(repo, "scripts/x.py", version + "\n", "main " + version)
        _git(repo, "checkout", "-q", "ygg/alpha-p1/lane/pm1tl1")
        _git(repo, "merge", "-q", "--no-ff", "-m", "sync " + version, "main")
        syncs[_git(repo, "rev-parse", "HEAD")] = {"lane": "pm1tl1", "main": main}
    s1 = _commit(repo, "scripts/x.py", "x1\n", "WP-1 takes x.py back to x1")
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: dict(syncs))
    return repo, base, s1


def test_a_lane_change_back_to_an_older_synced_version_stays_in_the_file_set(two_syncs):
    repo, base, s1 = two_syncs
    assert lanecheck.package_files("alpha-p1", str(repo), base, s1) == {"scripts/x.py"}


def test_another_package_changing_that_file_is_a_recheck_problem(two_syncs, monkeypatch):
    repo, base, s1 = two_syncs
    head = _commit(repo, "scripts/x.py", "x9\n", "WP-2 changes x.py")
    accepted = [("WP-1", "pm1tl1", s1, _Issue("WP-1", base, "2026-11-19T12:00:00Z"))]
    monkeypatch.setattr(lanecheck, "accepted_packages", lambda project, repo_: list(accepted))
    problems = lanecheck.recheck_problems("alpha-p1", "pm1tl1", str(repo), ["WP-2"], head)
    assert any("scripts/x.py belongs to accepted WP-1" in problem for problem in problems), problems


def test_accepted_content_equal_to_the_latest_sync_is_left_out(two_syncs, monkeypatch):
    repo, base, _ = two_syncs
    _git(repo, "reset", "-q", "--hard", "HEAD~1")  # back to the second sync: x.py = x2 from main
    sha = _commit(repo, "scripts/a.py", "a\n", "WP-1 changes only a.py")
    assert lanecheck.package_files("alpha-p1", str(repo), base, sha) == {"scripts/a.py"}
