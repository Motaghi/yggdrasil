"""Lane 1, Codex review round 5 (a11r5-lane1): the file set compares full tree entries with M, the main state
the lane absorbed through its latest recorded sync. Five fixtures, each with the follow-on WP-2 re-check."""

import os
import subprocess

import pytest

from yggdrasil import lanecheck

LANE = "ygg/alpha-p1/lane/pm1tl1"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo, name, text, message, *, remove=False, mode=None):
    path = repo / name
    if remove:
        _git(repo, "rm", "-q", "--", name)
    else:
        if text is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        if mode is not None:
            os.chmod(path, mode)
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


class Lane:
    def __init__(self, repo, monkeypatch):
        self.repo, self.syncs, self.monkeypatch = repo, {}, monkeypatch
        monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: dict(self.syncs))

    def main(self, name, text, message):
        _git(self.repo, "checkout", "-q", "main")
        sha = _commit(self.repo, name, text, message)
        _git(self.repo, "checkout", "-q", LANE)
        return sha

    def sync(self):
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "sync", "main")
        merge = _git(self.repo, "rev-parse", "HEAD")
        self.syncs[merge] = {"lane": "pm1tl1", "main": _git(self.repo, "rev-parse", "main")}
        return merge

    def accept_wp1(self, base, sha):
        accepted = [("WP-1", "pm1tl1", sha, _Issue("WP-1", base))]
        self.monkeypatch.setattr(lanecheck, "accepted_packages", lambda project, repo_: list(accepted))

    def wp2_changes(self, name):
        head = _commit(self.repo, name, "wp-2\n", "WP-2 changes " + name)
        return " ".join(lanecheck.recheck_problems("alpha-p1", "pm1tl1", str(self.repo), ["WP-2"], head))


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    monkeypatch.setattr(lanecheck, "config", _NoConfig)
    monkeypatch.setattr(lanecheck, "denylist", lambda: [])
    monkeypatch.setattr(lanecheck, "_repo_entry", lambda project, repo_: None)
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    repo = tmp_path / "home"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "scripts/x.py", "x0\n", "L0")
    _git(repo, "checkout", "-q", "-b", LANE)
    return Lane(repo, monkeypatch)


def test_1_edit_revert_then_sync_leaves_x_out(lane):
    base = _git(lane.repo, "rev-parse", "HEAD")
    _commit(lane.repo, "scripts/x.py", "lane edit\n", "edit")
    _commit(lane.repo, "scripts/x.py", "x0\n", "revert")
    lane.main("scripts/x.py", "x1\n", "main x1")
    lane.sync()
    sha = _commit(lane.repo, "scripts/a.py", "a\n", "WP-1")
    assert lanecheck.package_files("alpha-p1", str(lane.repo), base, sha) == {"scripts/a.py"}
    lane.accept_wp1(base, sha)
    assert "scripts/x.py belongs to accepted" not in lane.wp2_changes("scripts/x.py")


def test_2_back_to_an_older_synced_version_is_frozen(lane):
    base = _git(lane.repo, "rev-parse", "HEAD")
    for version in ("x1", "x2"):
        lane.main("scripts/x.py", version + "\n", "main " + version)
        lane.sync()
    sha = _commit(lane.repo, "scripts/x.py", "x1\n", "WP-1 back to x1")
    assert lanecheck.package_files("alpha-p1", str(lane.repo), base, sha) == {"scripts/x.py"}
    lane.accept_wp1(base, sha)
    assert "scripts/x.py belongs to accepted WP-1" in lane.wp2_changes("scripts/x.py")


def test_3_a_chmod_survives_an_unrelated_later_sync(lane):
    base = _git(lane.repo, "rev-parse", "HEAD")
    lane.main("scripts/x.py", "x1\n", "main x1")
    lane.sync()
    _commit(lane.repo, "scripts/x.py", None, "chmod", mode=0o755)
    lane.main("docs/notes.md", "main\n", "main notes")
    sha = lane.sync()
    assert lanecheck.package_files("alpha-p1", str(lane.repo), base, sha) == {"scripts/x.py"}
    lane.accept_wp1(base, sha)
    assert "scripts/x.py belongs to accepted WP-1" in lane.wp2_changes("scripts/x.py")


def test_4_a_chmod_survives_a_content_sync_of_the_same_file(lane):
    base = _git(lane.repo, "rev-parse", "HEAD")
    _commit(lane.repo, "scripts/x.py", None, "chmod", mode=0o755)
    lane.main("scripts/x.py", "x1\n", "main x1")
    sha = lane.sync()
    assert lanecheck.package_files("alpha-p1", str(lane.repo), base, sha) == {"scripts/x.py"}
    lane.accept_wp1(base, sha)
    assert "scripts/x.py belongs to accepted WP-1" in lane.wp2_changes("scripts/x.py")


def test_5_a_lane_only_file_the_package_deletes_stays_frozen(lane):
    base = _commit(lane.repo, "scripts/lane-only.py", "lane\n", "earlier lane work")
    lane.main("docs/notes.md", "main\n", "main moves")
    lane.sync()
    sha = _commit(lane.repo, "scripts/lane-only.py", None, "delete", remove=True)
    assert lanecheck.package_files("alpha-p1", str(lane.repo), base, sha) == {"scripts/lane-only.py"}
    lane.accept_wp1(base, sha)
    assert "scripts/lane-only.py belongs to accepted WP-1" in lane.wp2_changes("scripts/lane-only.py")
