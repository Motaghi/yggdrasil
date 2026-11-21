"""Lane 1, Codex review round 7 (a11r7-lane1), PM decision: M is the unique greatest main side (every other
in-range main side is an ancestor of it); with no unique greatest one, nothing is left out. Two incomparable
main sides A and B are fine when a later recorded sync of C = merge(A, B) holds them both."""

import itertools
import subprocess

import pytest

from yggdrasil import lanecheck

LANE = "ygg/alpha-p1/lane/pm1tl1"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo, name, text, message):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class _Issue:
    def __init__(self, identifier, base):
        self.id = identifier
        self.records = [{"kind": "review-opened", "base": base},
                        {"kind": "acceptance", "verdict": "accept", "at": "2026-11-19T12:00:00Z"}]

    def get(self, key, default=None):
        return default


class _NoConfig:
    @staticmethod
    def get(key, default=None):
        return default


@pytest.fixture
def three_syncs(tmp_path, monkeypatch):
    """Main forks into A (a.txt) and B (b.txt), incomparable; C = merge(A, B) also sets x.py to x1. The lane,
    from L0, records a sync of each of A, B and C; then the lane's own commit sets x.py to x2."""
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    monkeypatch.setattr(lanecheck, "config", _NoConfig)
    monkeypatch.setattr(lanecheck, "denylist", lambda: [])
    monkeypatch.setattr(lanecheck, "_repo_entry", lambda project, repo_: None)
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    repo = tmp_path / "home"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    base = _commit(repo, "scripts/x.py", "x0\n", "L0")
    a = _commit(repo, "a.txt", "a\n", "A")
    _git(repo, "checkout", "-q", "-b", "other", base)
    b = _commit(repo, "b.txt", "b\n", "B")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "other")
    (repo / "scripts/x.py").write_text("x1\n")
    _git(repo, "add", "--", "scripts/x.py")
    _git(repo, "commit", "-q", "-m", "C = merge(A, B), x1")
    c = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", LANE, base)
    syncs = []
    for main_side in (a, b, c):
        _git(repo, "merge", "-q", "--no-ff", "-m", "sync", main_side)
        syncs.append((_git(repo, "rev-parse", "HEAD"), {"lane": "pm1tl1", "main": main_side}))
    _commit(repo, "scripts/x.py", "x2\n", "lane sets x2")
    return repo, base, syncs, monkeypatch


def _wp2_problems(repo, base, sha, monkeypatch):
    accepted = [("WP-1", "pm1tl1", sha, _Issue("WP-1", base))]
    monkeypatch.setattr(lanecheck, "accepted_packages", lambda project, repo_: list(accepted))
    head = _commit(repo, "scripts/x.py", "wp-2\n", "WP-2 changes x.py")
    return " ".join(lanecheck.recheck_problems("alpha-p1", "pm1tl1", str(repo), ["WP-2"], head))


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_x_equal_to_c_is_left_out_in_every_record_order(three_syncs, order):
    repo, base, syncs, monkeypatch = three_syncs
    _commit(repo, "scripts/x.py", "x1\n", "lane takes x back to C's x1")
    sha = _commit(repo, "scripts/a.py", "a\n", "WP-1")
    ordered = dict(syncs[index] for index in order)
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: dict(ordered))
    assert lanecheck.package_files("alpha-p1", str(repo), base, sha) == {"scripts/a.py"}
    assert "scripts/x.py belongs to accepted" not in _wp2_problems(repo, base, sha, monkeypatch)


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_x_differing_from_c_is_frozen_in_every_record_order(three_syncs, order):
    repo, base, syncs, monkeypatch = three_syncs
    sha = _commit(repo, "scripts/a.py", "a\n", "WP-1 (x.py stays x2)")
    ordered = dict(syncs[index] for index in order)
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: dict(ordered))
    assert lanecheck.package_files("alpha-p1", str(repo), base, sha) == {"scripts/a.py", "scripts/x.py"}
    assert "scripts/x.py belongs to accepted WP-1" in _wp2_problems(repo, base, sha, monkeypatch)
