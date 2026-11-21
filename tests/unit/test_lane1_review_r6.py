"""Lane 1, Codex review round 6 (a11r6-lane1): M is chosen by the syncs' main-side commits, never by record
order or by the sync commits (which a non-sync merge can make incomparable)."""

import subprocess

import pytest

from yggdrasil import lanecheck

LANE = "ygg/alpha-p1/lane/pm1tl1"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _write(repo, name, text):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _commit(repo, name, text, message):
    _write(repo, name, text)
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
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    monkeypatch.setattr(lanecheck, "config", _NoConfig)
    monkeypatch.setattr(lanecheck, "denylist", lambda: [])
    monkeypatch.setattr(lanecheck, "_repo_entry", lambda project, repo_: None)
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    home = tmp_path / "home"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    _commit(home, "scripts/x.py", "x0\n", "L0")
    return home


def _history(repo, first_main, second_main):
    """B; on a side line a recorded sync S1 of `first_main`; on the lane a recorded sync S2 of `second_main`;
    then a non-sync merge (first parent S2) of the side line that sets x.py back to x1. Returns (B, N, S1, S2)."""
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side", base)
    _git(repo, "merge", "-q", "--no-ff", "-m", "sync 1", first_main)
    s1 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", LANE, base)
    _git(repo, "merge", "-q", "--no-ff", "-m", "sync 2", second_main)
    s2 = _git(repo, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "core.hooksPath=/dev/null", "merge", "-q", "--no-ff", "--no-commit", "side"],
                   capture_output=True, text=True)
    _write(repo, "scripts/x.py", "x1\n")
    _git(repo, "add", "--", "scripts/x.py")
    _git(repo, "commit", "-q", "-m", "a non-sync merge restores x1")
    return base, _git(repo, "rev-parse", "HEAD"), s1, s2


def _wp2_problems(repo, base, accepted_sha, monkeypatch):
    accepted = [("WP-1", "pm1tl1", accepted_sha, _Issue("WP-1", base))]
    monkeypatch.setattr(lanecheck, "accepted_packages", lambda project, repo_: list(accepted))
    head = _commit(repo, "scripts/x.py", "wp-2\n", "WP-2 changes x.py")
    return " ".join(lanecheck.recheck_problems("alpha-p1", "pm1tl1", str(repo), ["WP-2"], head))


@pytest.mark.parametrize("order", ["s1-first", "s2-first"])
def test_incomparable_syncs_choose_m_by_the_newest_main_side(repo, monkeypatch, order):
    m1 = _commit(repo, "scripts/x.py", "x1\n", "M1")
    m2 = _commit(repo, "scripts/x.py", "x2\n", "M2")  # a descendant of M1
    base_commit = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    _git(repo, "checkout", "-q", base_commit)
    base, sha, s1, s2 = _history(repo, m1, m2)
    pairs = [(s1, {"lane": "pm1tl1", "main": m1}), (s2, {"lane": "pm1tl1", "main": m2})]
    syncs = dict(pairs if order == "s1-first" else reversed(pairs))
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: dict(syncs))
    assert lanecheck.package_files("alpha-p1", str(repo), base, sha) == {"scripts/x.py"}
    assert "scripts/x.py belongs to accepted WP-1" in _wp2_problems(repo, base, sha, monkeypatch)


@pytest.mark.parametrize("order", ["s1-first", "s2-first"])
def test_incomparable_main_sides_leave_nothing_out(repo, monkeypatch, order):
    base_commit = _git(repo, "rev-parse", "HEAD")
    a = _commit(repo, "scripts/x.py", "x1\n", "main line A")
    _git(repo, "checkout", "-q", "-b", "other-main", base_commit)
    b = _commit(repo, "scripts/x.py", "x3\n", "main line B")  # not related to A
    _git(repo, "checkout", "-q", base_commit)
    base, sha, s1, s2 = _history(repo, a, b)
    pairs = [(s1, {"lane": "pm1tl1", "main": a}), (s2, {"lane": "pm1tl1", "main": b})]
    syncs = dict(pairs if order == "s1-first" else reversed(pairs))
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: dict(syncs))
    # x.py ends as x1, equal to main line A: only the fail-safe keeps it frozen.
    assert lanecheck.package_files("alpha-p1", str(repo), base, sha) == {"scripts/x.py"}
    assert "scripts/x.py belongs to accepted WP-1" in _wp2_problems(repo, base, sha, monkeypatch)
