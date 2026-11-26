"""v1.1.1 lane clones: the runner's command line and descriptor spawn, the box check, and the tender's walk and
odd-clone checks run on real files (no model, no Codex)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from yggdrasil import clones, tender, util


def _git(*args, cwd=None):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.delenv("YGG_TEST_CODEX_VERSION", raising=False)
    monkeypatch.delenv("YGG_TEST_PLATFORM", raising=False)
    monkeypatch.delenv("YGG_FAULT_AT", raising=False)
    root = tmp_path / "lane" / "tl1.g000001"
    (root / ".git").mkdir(parents=True)
    (root / "tmp" / "cache").mkdir(parents=True)
    clone = root / "checkout"
    _git("init", "-q", "--initial-branch=ygg/p/lane/tl1", str(clone))
    (clone / "a.txt").write_text("a")
    _git("add", "a.txt", cwd=clone)
    _git("-c", "user.name=E", "-c", "user.email=e@example.invalid", "commit", "-q", "-m", "a", cwd=clone)
    head = _git("rev-parse", "HEAD", cwd=clone)
    _git("update-ref", "refs/ygg/given", head, cwd=clone)
    found = os.lstat(root)
    fmt = subprocess.run(["git", "config", "--file", str(clone / ".git/config"), "--get-regexp",
                          r"^(core\.repositoryformatversion|extensions\..*)$"], capture_output=True,
                         text=True).stdout
    return {"kind": "lane", "box": str(root), "path": str(clone), "branch": "ygg/p/lane/tl1",
            "box_device": found.st_dev, "box_inode": found.st_ino, "given_head": head, "given_ref": head,
            "feeding": None, "format": fmt, "generation": 1}


def test_runner_command_line_outside_test_mode(monkeypatch):
    monkeypatch.delenv("YGG_TEST_MODE", raising=False)
    argv = clones.tender_argv("report", "{}", "read-only", "/opt/codex")
    assert argv[:4] == ["/opt/codex", "sandbox", "-c", 'sandbox_mode="read-only"']
    assert "-C" not in argv
    for key in clones.SANDBOX_KEYS:
        assert argv[argv.index(key) - 1] == "-c"
    assert argv[argv.index("--") + 1:] == [sys.executable, "-I", "-m", "yggdrasil", "_tend", "report", "{}"]


def test_helper_starts_in_the_verified_descriptor(box, monkeypatch):
    monkeypatch.setattr(clones, "tender_argv", lambda *a: ["/bin/pwd"])
    run = clones.run_tender(box, "report", {}, timeout=10)
    assert os.path.realpath(run.stdout.strip()) == os.path.realpath(box["box"])


def test_replaced_box_or_checkout_link_is_refused_before_any_spawn(box, tmp_path):
    os.rename(Path(box["box"]) / "checkout", tmp_path / "old")
    os.symlink(tmp_path / "old", Path(box["box"]) / "checkout")
    with pytest.raises(clones.BoxOdd) as error:
        clones.open_box(box)
    assert error.value.check == "3a"
    moved = Path(box["box"]).with_name("tl1.g000009")
    os.rename(box["box"], moved)
    os.symlink(moved, box["box"])
    with pytest.raises(clones.BoxOdd):
        clones.open_box(box)


def test_clean_box_reports_counts(box):
    result = clones.tend(box, "report", {"lane_head": box["given_head"]}, 60)
    assert result["result"] == "ok", result
    assert result["counts"] == {"refs": 0, "stash": 0, "commits": 0, "changed": 0, "untracked": 0, "ignored": 0}


def test_link_fifo_and_ancestry_overrides_are_odd(box):
    git = Path(box["path"]) / ".git"
    os.mkfifo(git / "commondir.fifo")
    result = clones.tend(box, "prepare", {"config": "", "lane_head": None, "sole": False}, 60)
    assert (result["check"], result["kind"], result["path"]) == ("3", "special", "commondir.fifo")
    os.unlink(git / "commondir.fifo")
    (git / "shallow").write_text(box["given_head"] + "\n")
    result = clones.tend(box, "report", {"lane_head": None}, 60)
    assert (result["check"], result["kind"], result["path"]) == ("3", "ancestry", "shallow")


def test_alternates_is_an_outside_dependency_and_never_edited(box):
    alternates = Path(box["path"]) / ".git/objects/info/alternates"
    alternates.parent.mkdir(exist_ok=True)
    alternates.write_text("../../elsewhere\n")
    result = clones.tend(box, "report", {"lane_head": None}, 60)
    assert (result["check"], result["kind"]) == ("3a", "outside")
    assert result["walk"]["outside"][0]["lines"] == ["../../elsewhere"]
    assert alternates.read_text() == "../../elsewhere\n"


def test_unlistable_folder_raises_the_walk(box):
    folder = Path(box["path"]) / ".git/objects/zz"
    folder.mkdir()
    os.mkfifo(folder / "f")
    folder.chmod(0o111)
    try:
        if os.access(folder, os.R_OK):
            pytest.skip("this user can list a mode 0111 folder")
        result = clones.tend(box, "report", {"lane_head": None}, 60)
    finally:
        folder.chmod(0o755)
    assert result["check"] == "3" and "could not be checked" in result["path"] and "objects/zz" in result["path"]


def test_moved_given_ref_is_odd_check_4(box):
    clone = box["path"]
    (Path(clone) / "b.txt").write_text("b")
    _git("add", "b.txt", cwd=clone)
    _git("-c", "user.name=E", "-c", "user.email=e@example.invalid", "commit", "-q", "-m", "b", cwd=clone)
    _git("update-ref", "refs/ygg/given", "HEAD", cwd=clone)
    result = clones.tend(box, "prepare", {"config": "", "lane_head": None, "sole": False}, 60)
    assert (result["check"], result["kind"]) == ("4", "given")


def test_tender_git_environment_writes_no_global_config(tmp_path):
    environment = tender.git_env(str(tmp_path / "missing.gitconfig"))
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in environment
    assert not (tmp_path / "missing.gitconfig").exists()


def test_helper_version_pin_texts(monkeypatch):
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.setenv("YGG_TEST_PLATFORM", "linux")
    with pytest.raises(util.YggError) as error:
        clones.check_codex("re-check refused")
    assert "re-check refused: Codex builders have not been probed on linux" in error.value.message
    monkeypatch.setenv("YGG_TEST_PLATFORM", "darwin")
    monkeypatch.setenv("YGG_TEST_CODEX_VERSION", "codex-cli 0.170.01")
    with pytest.raises(util.YggError) as error:
        clones.check_codex("re-check refused")
    assert "re-check refused: codex-cli 0.170.01 has not been probed with Yggdrasil" in error.value.message
    monkeypatch.setenv("YGG_TEST_CODEX_VERSION", "codex-cli 0.170.0\n")
    assert clones.check_codex()["version"] == "codex-cli 0.170.0"


def test_generation_names_are_fixed_width():
    assert clones.box_name("pm1tl1", 1) == "pm1tl1.g000001"
    assert clones.box_name("pm1tl1", 10) == "pm1tl1.g000010"
    assert not clones.box_name("pm1tl1", 10).startswith(clones.box_name("pm1tl1", 1))
