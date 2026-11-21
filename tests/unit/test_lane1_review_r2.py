"""Lane 1, Codex review round 2 (a11r2-lane1): one test per finding."""

import subprocess

import pytest

from yggdrasil import lanecheck


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo, name, text, message, *, remove=False):
    path = repo / name
    if remove:
        _git(repo, "rm", "-q", "--", name)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class _Issue:
    def __init__(self, identifier, at=None):
        self.id = identifier
        self.records = [{"kind": "acceptance", "verdict": "accept", "at": at}] if at else []

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
    _commit(home, "README.md", "x\n", "L0")
    return home


def test_blocker_1_the_package_accepted_first_owns_a_shared_path_across_lanes(repo, monkeypatch):
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl2")
    for number in range(3):  # more commits below WP-2's accepted commit: a commit count would pick WP-1
        _commit(repo, "notes-" + str(number) + ".md", "n\n", "n" + str(number))
    s2 = _commit(repo, "scripts/a.py", "two\n", "WP-2")
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl1", "main")
    s1 = _commit(repo, "scripts/a.py", "one\n", "WP-1")
    accepted = [("WP-1", "pm1tl1", s1, _Issue("WP-1", "2026-11-19T12:00:05Z")),
                ("WP-2", "pm1tl2", s2, _Issue("WP-2", "2026-11-19T12:00:00Z"))]
    monkeypatch.setattr(lanecheck, "accepted_packages", lambda project, repo_: list(accepted))
    assert lanecheck.frozen_files("alpha-p1", str(repo))["scripts/a.py"] == "WP-2"


def test_major_2_a_change_undone_before_acceptance_is_not_frozen(repo):
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl1")
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "docs/x.md", "draft\n", "add")
    _commit(repo, "docs/x.md", "", "undo", remove=True)
    _commit(repo, "README.md", "x\n" * 2, "readme")
    _commit(repo, "README.md", "x\n", "readme back")
    sha = _commit(repo, "scripts/a.py", "x = 2\n", "S")
    assert lanecheck.package_files("alpha-p1", str(repo), base, sha) == {"scripts/a.py"}


@pytest.mark.parametrize("path", ["config/.env.production.example", ".env.sample", "app/.env.template", ".env"])
def test_major_3_env_files_have_no_sample_exemption(path):
    assert lanecheck.touched_problem(path) == "credential file " + path + " is refused"


def test_other_sample_files_keep_their_exemption():
    assert lanecheck.touched_problem("config/settings.example") is None
