"""Lane 1, Codex review round 3 (a11r3-lane1): one test per finding, plus the lane-only deletion edge."""

import itertools
import subprocess

import pytest

from yggdrasil import lanecheck


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo, name, text, message, *, remove=False):
    if remove:
        _git(repo, "rm", "-q", "--", name)
    else:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class _Issue:
    def __init__(self, identifier, at="2026-11-19T12:00:00Z"):
        self.id = identifier
        self.records = [{"kind": "acceptance", "verdict": "accept", "at": at}]

    def get(self, key, default=None):
        return default


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: {})
    home = tmp_path / "home"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    _commit(home, "scripts/a.py", "x = 1\n", "L0")
    return home


def test_blocker_1_equal_times_give_one_owner_in_every_list_order(repo, monkeypatch):
    """A is an ancestor of B, C is incomparable with both, all accepted in the same second: the pairwise
    ancestry comparator could cycle. The owner is now one sort key: A (2 commits) and C (2 commits) tie on
    depth and the lower package number, WP-2, wins, in every list order."""
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl1")
    a = _commit(repo, "scripts/a.py", "x = 2\n", "A")
    b = _commit(repo, "scripts/a.py", "x = 3\n", "B")
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl2", "main")
    c = _commit(repo, "scripts/a.py", "x = 4\n", "C")
    entries = [("WP-3", "pm1tl1", a, _Issue("WP-3")), ("WP-1", "pm1tl1", b, _Issue("WP-1")),
               ("WP-2", "pm1tl2", c, _Issue("WP-2"))]
    owners = set()
    for permutation in itertools.permutations(entries):
        monkeypatch.setattr(lanecheck, "accepted_packages", lambda project, repo_, p=permutation: list(p))
        owners.add(lanecheck.frozen_files("alpha-p1", str(repo))["scripts/a.py"])
    assert owners == {"WP-2"}


def test_a_same_second_earlier_commit_on_one_lane_owns_the_path(repo, monkeypatch):
    """A11 AC-85 row 3 under the suite's fixed clock: WP-2 accepted at S2, WP-1 seeded at a later S, same
    second, WP-1 listed first: WP-2 owns, so WP-1's change to the file is a freeze fault."""
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl1")
    s2 = _commit(repo, "scripts/a.py", "x = 2\n", "WP-2")
    s1 = _commit(repo, "scripts/a.py", "x = 3\n", "WP-1")
    entries = [("WP-1", "pm1tl1", s1, _Issue("WP-1")), ("WP-2", "pm1tl1", s2, _Issue("WP-2"))]
    monkeypatch.setattr(lanecheck, "accepted_packages", lambda project, repo_: list(entries))
    assert lanecheck.frozen_files("alpha-p1", str(repo))["scripts/a.py"] == "WP-2"


def test_an_earlier_acceptance_time_still_wins(repo, monkeypatch):
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl1")
    a = _commit(repo, "scripts/a.py", "x = 2\n", "A")
    entries = [("WP-1", "pm1tl1", a, _Issue("WP-1", "2026-11-19T12:00:09Z")),
               ("WP-7", "pm1tl1", a, _Issue("WP-7", "2026-11-19T12:00:01Z"))]
    monkeypatch.setattr(lanecheck, "accepted_packages", lambda project, repo_: list(entries))
    assert lanecheck.frozen_files("alpha-p1", str(repo))["scripts/a.py"] == "WP-7"


def test_major_2_an_edited_and_restored_path_synced_from_main_is_not_frozen(repo, monkeypatch):
    _commit(repo, "scripts/x.py", "main 1\n", "x on main")
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl1")
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "scripts/x.py", "lane edit\n", "lane edit")
    _commit(repo, "scripts/x.py", "main 1\n", "lane revert")
    _git(repo, "checkout", "-q", "main")
    main = _commit(repo, "scripts/x.py", "main 2\n", "main changes x")
    _git(repo, "checkout", "-q", "ygg/alpha-p1/lane/pm1tl1")
    _git(repo, "merge", "-q", "--no-ff", "-m", "sync", "main")
    sync = _git(repo, "rev-parse", "HEAD")
    sha = _commit(repo, "scripts/a.py", "x = 2\n", "S")
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: {sync: {"lane": "pm1tl1", "main": main}})
    assert lanecheck.package_files("alpha-p1", str(repo), base, sha) == {"scripts/a.py"}


def test_a_lane_only_file_the_package_deletes_stays_frozen_across_a_sync(repo, monkeypatch):
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl1")
    base = _commit(repo, "scripts/lane-only.py", "lane\n", "earlier lane work")
    _git(repo, "checkout", "-q", "main")
    main = _commit(repo, "docs/notes.md", "main\n", "main moves")
    _git(repo, "checkout", "-q", "ygg/alpha-p1/lane/pm1tl1")
    _git(repo, "merge", "-q", "--no-ff", "-m", "sync", "main")
    sync = _git(repo, "rev-parse", "HEAD")
    sha = _commit(repo, "scripts/lane-only.py", "", "delete", remove=True)
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: {sync: {"lane": "pm1tl1", "main": main}})
    assert lanecheck.package_files("alpha-p1", str(repo), base, sha) == {"scripts/lane-only.py"}
