"""Lane 1, Codex review round 1 (a11r1-lane1): one test per confirmed finding."""


import subprocess

import pytest

from yggdrasil import hooks, lanecheck


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _write(repo, name, text):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class _NoConfig:
    @staticmethod
    def get(key, default=None):
        return default


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    monkeypatch.setattr(lanecheck, "config", _NoConfig)
    monkeypatch.setattr(lanecheck, "denylist", lambda: ["zebra-orchid-7"])
    monkeypatch.setattr(lanecheck, "_repo_entry", lambda project, repo_: None)
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    home = tmp_path / "home"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    _write(home, "README.md", "x = 1\n")
    _git(home, "add", ".")
    _git(home, "commit", "-q", "-m", "L0")
    return home


def test_blocker_1_the_recheck_reads_what_a_merge_commit_adds(repo):
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl1")
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, "x.txt", "clean\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "side")
    _git(repo, "checkout", "-q", "ygg/alpha-p1/lane/pm1tl1")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    _write(repo, ".github/workflows/x.yml", "name: x\n")
    _write(repo, "notes.txt", "token gho_" + "A" * 36 + "\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "merge")
    problems = " ".join(lanecheck.recheck_problems("alpha-p1", "pm1tl1", str(repo), ["WP-1"]))
    assert "CI workflow files are refused on lanes" in problems
    assert "secret-shaped token" in problems
    assert "gho_" not in problems


def test_blocker_2_a_quoted_name_keeps_its_exact_path_in_the_freeze(repo, monkeypatch):
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: {})
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "scripts/æ.py", "a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "S")
    assert lanecheck.package_files("alpha-p1", str(repo), base, _git(repo, "rev-parse", "HEAD")) == {"scripts/æ.py"}


def test_major_3_an_empty_inbox_file_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    folder = tmp_path / "state" / "inboxes"
    folder.mkdir(parents=True)
    (folder / "pm1tl1.json").write_text("")
    counts = hooks.unread_counts("pm1tl1", None)
    assert counts is None
    assert hooks.unread_refusal("pm1tl1", counts).startswith("inbox unavailable")


@pytest.mark.parametrize("path", ["credentials.json", "config/credentials.json", ".npmrc", "web/.npmrc"])
def test_major_4_the_signed_credential_names_are_refused(path):
    assert lanecheck.touched_problem(path) == "credential file " + path + " is refused"


def test_major_5_a_sync_before_acceptance_adds_no_main_files(repo, monkeypatch):
    _git(repo, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl1")
    _write(repo, "scripts/p.py", "p\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "P")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "docs/m.md", "main\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "m")
    main = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "ygg/alpha-p1/lane/pm1tl1")
    _git(repo, "merge", "-q", "--no-ff", "-m", "sync", "main")
    sync = _git(repo, "rev-parse", "HEAD")
    _write(repo, "scripts/a.py", "x = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "S")
    sha = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: {sync: {"lane": "pm1tl1", "main": main}})
    assert lanecheck.package_files("alpha-p1", str(repo), base, sha) == {"scripts/a.py"}


@pytest.mark.parametrize("command,allowed", [
    ("git log --max-count=5", True), ("git log --oneline --max-count=5 HEAD", True),
    ("git rev-parse --abbrev-ref HEAD", True), ("git rev-parse --show-toplevel", True),
    ("git rev-parse --short HEAD", False), ("git rev-parse --is-inside-work-tree", False),
    ("git rev-parse --abbrev-ref main", False), ("git log --format=%H", False), ("git --git-dir=/x log", False)])
def test_minor_6_the_read_shapes_follow_ac21(repo, command, allowed):
    assert (hooks.read_git_refusal(command, str(repo), str(repo)) is None) is allowed, command


def test_a_root_commit_lists_its_own_files(repo):
    first = _git(repo, "rev-parse", "HEAD")
    assert lanecheck._raw_names(lanecheck._commit_entries(str(repo), first)) == ["README.md"]
