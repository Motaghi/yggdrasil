"""v1.1.1 lane clones: the Codex command line for a clone runs no trusted git, a launch waits for an assembly
that is still running, a feed completes only with the head it confirmed, and a truncated index is never
reported as a clean box. Each test fails before its fix."""

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from yggdrasil import clones, launcher, util


def _git(*args, cwd=None):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=E",
                           "-c", "user.email=e@example.invalid", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "files"))
    for name in ("YGG_TEST_CODEX_VERSION", "YGG_TEST_PLATFORM", "YGG_FAULT_AT", "YGG_TEST_ASSEMBLY_HOLD"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "home"
    _git("init", "-q", "--initial-branch=main", str(path))
    (path / "a.txt").write_text("a")
    _git("add", "a.txt", cwd=path)
    _git("commit", "-q", "-m", "a", cwd=path)
    _git("branch", "ygg/p/lane/tl1", cwd=path)
    monkeypatch.setattr(clones, "clone_config", lambda *a: "[core]\n\trepositoryformatversion = 0\n")
    monkeypatch.setattr(clones, "lane_alive", lambda *a, **k: False)
    return str(path)


# An ordinary clone launch never runs trusted git in the box to learn it is a repository.
def test_codex_command_line_for_a_clone_runs_no_trusted_git(home, monkeypatch):
    record = clones.assemble("p", "tl1", home)
    calls = []
    monkeypatch.setattr(launcher, "git", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    launcher.codex_sandbox({"cwd": record["path"], "role": "tl", "project": "p", "activation": "act-1",
                            "clone": clones.pin(record)},
                           {"role": "tl", "project": "p"})
    assert not [c for c in calls if record["box"] in " ".join(map(str, c))]


# a second launch on the lane never recovers an assembly that is still running.
def test_concurrent_launch_waits_for_a_running_assembly(home, tmp_path, monkeypatch):
    hold = tmp_path / "hold"
    hold.mkdir()
    monkeypatch.setenv("YGG_TEST_ASSEMBLY_HOLD", str(hold))
    errors = []

    def first():
        try:
            clones.assemble("p", "tl1", home)
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    one = threading.Thread(target=first)
    one.start()
    deadline = time.monotonic() + 60
    while not (hold / "entered-assembly").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (hold / "entered-assembly").exists()

    def second():
        try:
            clones.prepare_lane({"id": "tl1w1"}, home, "p", "tl1")
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    two = threading.Thread(target=second)
    two.start()
    time.sleep(1.5)
    (hold / "release-assembly").write_text("")
    one.join(120)
    two.join(120)
    assert not errors, errors
    record = clones.read("p", "tl1")
    folder = clones.lane_folder(home, "p")
    assert sorted(os.listdir(folder)) == ["tl1.g000001"]
    assert record["state"] == "ready" and record["generation"] == 1
    assert record["box"] == str(folder / "tl1.g000001")
    found = os.lstat(record["box"])
    assert [found.st_dev, found.st_ino] == [record["box_device"], record["box_inode"]]
    assert not clones.quarantine_records("p", "tl1")


# tending completes only the feed head the tender confirmed, never a newer one in the record.
def test_feed_completion_uses_only_the_confirmed_head(home, monkeypatch):
    record = clones.assemble("p", "tl1", home)
    confirmed = "1" * 40
    newer = "2" * 40
    with util.locked():
        current = clones.read("p", "tl1")
        current["feeding"] = confirmed
        clones.write("p", "tl1", current)
    calls = {"n": 0}

    def fake_tend(rec, mode, spec, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            # A lane sync started a newer feed after the snapshot the tender checked.
            with util.locked():
                value = clones.read("p", "tl1")
                value["feeding"] = newer
                clones.write("p", "tl1", value)
            return {"result": "ok", "feed_complete": True, "given": confirmed, "fast_forward": "not tried"}
        return {"result": "ok", "fast_forward": "done"}

    monkeypatch.setattr(clones, "tend", fake_tend)
    monkeypatch.setattr(clones, "feed", lambda *a, **k: None)
    monkeypatch.setattr(clones, "_rev", lambda *a: record["given_head"])
    clones._tend_ready({"id": "tl1"}, "p", "tl1", clones.read("p", "tl1"), None)
    after = clones.read("p", "tl1")
    assert after["given_head"] != newer and after["given_ref"] != newer
    assert after["feeding"] == newer


# a git failure inside the box is never read as a clean clone with zero counts.
def test_truncated_index_is_not_a_clean_report(home):
    record = clones.assemble("p", "tl1", home)
    index = Path(record["path"]) / ".git/index"
    index.write_bytes(index.read_bytes()[:20])
    result = clones.tend(record, "report", {"lane_head": record["given_head"]}, 60)
    assert result.get("result") != "ok", result
    assert result.get("check") == "5"
    with pytest.raises(util.YggError) as error:
        clones._refuse_report("p", "tl1", result)
    assert "re-check refused" in error.value.message
