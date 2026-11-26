"""v1.1.1: test clones for a testing Codex senior user, on real files."""

import os
import subprocess
from pathlib import Path

import pytest

from yggdrasil import clones, laneclone, launcher, lifecycle, util
from yggdrasil.util import YggError


def _git(*args, cwd=None, check=True):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=E",
                           "-c", "user.email=e@example.invalid", *args], cwd=cwd, capture_output=True,
                          text=True, check=check)


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
    (path / "b.txt").write_text("b")
    _git("add", "b.txt", cwd=path)
    _git("commit", "-q", "-m", "lane work", cwd=path)
    _git("branch", "ygg/p/lane/tl1", cwd=path)
    _git("reset", "-q", "--hard", "HEAD~1", cwd=path)
    monkeypatch.setattr(clones, "approved_remote", lambda *a: None)
    from yggdrasil import tracker
    monkeypatch.setattr(tracker, "programme", lambda name: {"name": name})
    monkeypatch.setattr(tracker, "programme_home", lambda programme: str(path))
    return str(path)


def _lane_head(home):
    return _git("rev-parse", "refs/heads/ygg/p/lane/tl1", cwd=home).stdout.strip()


def _assemble(home, activation="act-0007"):
    return clones.assemble_test("p", "su1", activation, home, "ygg/p/lane/tl1", programme="prog")


def _settled(monkeypatch, *activations):
    monkeypatch.setattr(lifecycle, "facts", lambda programme, key: [{"phase": "exited"}] if key in activations else
                        [{"phase": "spawned"}])


def test_test_clone_is_detached_at_the_lane_head_with_no_link_to_the_home(home):
    record = _assemble(home)
    box = Path(home) / ".ygg-worktrees/p/test/su1.act-0007"
    checkout = box / "checkout"
    assert clones.read("p", "test-su1.act-0007") == record
    assert (record["kind"], record["state"], record["box"], record["path"]) == ("test", "ready", str(box), str(checkout))
    assert record["made_from"] == _lane_head(home)
    assert _git("rev-parse", "HEAD", cwd=checkout).stdout.strip() == _lane_head(home)
    assert _git("symbolic-ref", "-q", "HEAD", cwd=checkout, check=False).returncode != 0  # detached
    assert _git("rev-parse", "--absolute-git-dir", cwd=checkout).stdout.strip() == str(checkout / ".git")
    assert sorted(os.listdir(box)) == [".git", "checkout", "tmp"]
    assert os.listdir(box / ".git") == []
    assert (box / "tmp" / "cache").is_dir()
    assert "/.ygg-worktrees/p/test" not in _git("worktree", "list", "--porcelain", cwd=home).stdout
    assert not (checkout / ".git/objects/info/alternates").exists()
    hooks = _git("config", "--file", str(checkout / ".git/config"), "core.hooksPath").stdout.strip()
    assert hooks == str(clones.empty_hooks_root()) and os.listdir(hooks) == []
    assert hooks != str(launcher.hooks_root())
    assert (record["box_device"], record["box_inode"]) == (os.lstat(box).st_dev, os.lstat(box).st_ino)


def test_a_test_clone_commit_never_reaches_the_home_and_settlement_removes_it(home):
    record = _assemble(home)
    checkout = Path(record["path"])
    (checkout / "t.txt").write_text("")
    _git("add", "t.txt", cwd=checkout)
    assert _git("commit", "-q", "-m", "t", cwd=checkout).returncode == 0
    head = _lane_head(home)
    assert clones.remove_test_clone("p", "test-su1.act-0007") is True
    assert _lane_head(home) == head
    assert "t" not in _git("log", "--all", "--format=%s", cwd=home).stdout.splitlines()
    assert not Path(record["box"]).exists()
    assert os.listdir(Path(home) / ".ygg-worktrees/p/_removing") == []
    assert clones.read("p", "test-su1.act-0007") is None


def test_the_sweep_removes_only_settled_activations(home, monkeypatch):
    settled = _assemble(home, "act-0007")
    running = _assemble(home, "act-0008")
    _settled(monkeypatch, "act-0007")
    assert clones.sweep_test_clones() == ["test-su1.act-0007"]
    assert not Path(settled["box"]).exists()
    assert Path(running["box"]).is_dir() and clones.read("p", "test-su1.act-0008")["state"] == "ready"


def test_an_activation_with_no_lifecycle_fact_keeps_its_test_clone(home, monkeypatch):
    record = _assemble(home)
    monkeypatch.setattr(lifecycle, "facts", lambda programme, key: [])
    assert clones.sweep_test_clones() == []
    assert Path(record["box"]).is_dir()


def test_an_interrupted_assembly_is_removed_with_its_temporary_folder(home, monkeypatch):
    def stop(name):
        if name == "test-clone-before-rename":
            raise RuntimeError("crash")
    monkeypatch.setattr(clones.faults, "boundary", stop)
    with pytest.raises(RuntimeError):
        _assemble(home)
    record = clones.read("p", "test-su1.act-0007")
    assert record["state"] == "creating" and Path(record["box"]).name.startswith(".creating-")
    assert Path(record["box"]).is_dir()
    _settled(monkeypatch, "act-0007")
    assert clones.sweep_test_clones() == ["test-su1.act-0007"]
    assert os.listdir(Path(home) / ".ygg-worktrees/p/test") == []


def test_a_folder_that_is_not_the_recorded_box_is_left_in_place(home, tmp_path):
    record = _assemble(home)
    box = Path(record["box"])
    os.rename(box, tmp_path / "moved")
    box.mkdir()
    (box / "keep.txt").write_text("mine")
    assert clones.remove_test_clone("p", "test-su1.act-0007") is False
    assert (box / "keep.txt").read_text() == "mine"
    left = clones.read("p", "test-su1.act-0007")
    assert left["state"] == "removing" and "left in place" in left["reason"]


def test_a_link_at_the_box_name_is_never_followed(home, tmp_path):
    record = _assemble(home)
    box = Path(record["box"])
    os.rename(box, tmp_path / "moved")
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("mine")
    box.symlink_to(target)
    assert clones.remove_test_clone("p", "test-su1.act-0007") is False
    assert (target / "keep.txt").read_text() == "mine" and box.is_symlink()


def test_read_only_folders_an_agent_left_are_removed(home):
    record = _assemble(home)
    folder = Path(record["path"]) / "ro"
    folder.mkdir()
    (folder / "f").write_text("x")
    os.chmod(folder, 0o500)
    assert clones.remove_test_clone("p", "test-su1.act-0007") is True
    assert not Path(record["box"]).exists()


def test_an_earlier_removal_that_stopped_after_its_rename_is_finished(home):
    record = _assemble(home)
    removing = Path(home) / ".ygg-worktrees/p/_removing"
    removing.mkdir()
    os.rename(record["box"], removing / "su1.act-0007")
    with util.locked():
        clones.write("p", "test-su1.act-0007", dict(record, state="removing"))
    assert clones.remove_test_clone("p", "test-su1.act-0007") is True
    assert os.listdir(removing) == []


def test_a_second_test_clone_for_one_activation_is_refused(home):
    _assemble(home)
    with pytest.raises(YggError) as raised:
        _assemble(home)
    assert "already has a test clone" in raised.value.message


def test_a_non_empty_hooks_folder_is_refused(home):
    folder = clones.empty_hooks_root()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pre-commit").write_text("#!/bin/sh\n")
    with pytest.raises(YggError) as raised:
        _assemble(home)
    assert "must be an empty folder" in raised.value.message


def test_pin_of_a_test_clone_and_its_refusal_once_removed(home):
    record = _assemble(home)
    pinned = clones.pin(record)
    assert (pinned["project"], pinned["name"], pinned["kind"]) == ("p", "test-su1.act-0007", "test")
    assert clones.pin_refusal(pinned) is None
    assert clones.clone_path(record["path"])
    clones.remove_test_clone("p", "test-su1.act-0007")
    assert "has no record" in clones.pin_refusal(pinned)


def test_settle_test_clone_removes_by_the_receipt_pin(home):
    record = _assemble(home)
    clones.settle_test_clone({"clone": clones.pin(record)})
    assert not Path(record["box"]).exists()
    clones.settle_test_clone({"clone": {"kind": "lane", "project": "p", "name": "tl1"}})  # never a lane


def test_the_senior_user_command_line_roots_in_the_test_box_with_no_trusted_git(home, monkeypatch):
    record = _assemble(home)
    calls = []
    monkeypatch.setattr(launcher, "git", lambda *a, **k: calls.append(a))
    args = launcher.codex_sandbox({"activation": "act-0007", "role": "su", "cwd": record["path"],
                                   "clone": clones.pin(record)}, {"role": "su"})
    assert args[args.index("--cd") + 1] == record["box"]
    assert "--add-dir" not in args  # no writable state or tracker root (CB-03)
    assert not calls


def test_the_slot_returns_the_work_area_and_its_pin(home):
    found = laneclone.test_box({"id": "su1", "project": "p", "programme": "prog"}, {}, "act-0009",
                               {"branch": "ygg/p/lane/tl1", "package": "WP-1"})
    record = clones.read("p", "test-su1.act-0009")
    assert found["box"] == record["box"] and found["checkout"] == record["path"] and found["kind"] == "test"
    assert (found["box_device"], found["box_inode"]) == (record["box_device"], record["box_inode"])
    assert found["pin"] == clones.pin(record)
    assert "nothing you commit or change there ever reaches the project" in found["prompt"][0] and "WP-1" in found["prompt"][0]


@pytest.mark.parametrize("change", [{"home": "/"}, {"nonce": "../../x"}, {"activation": "../act"},
                                    {"programme": "elsewhere"}])
def test_a_record_with_untrusted_paths_is_never_acted_on(home, tmp_path, monkeypatch, change):
    from yggdrasil import tracker
    record = _assemble(home)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(tracker, "programme_home", lambda programme: home if programme["name"] == "prog" else str(other))
    with util.locked():
        clones.write("p", "test-su1.act-0007", dict(record, **change))
    _settled(monkeypatch, "act-0007", "../act")
    assert clones.remove_test_clone("p", "test-su1.act-0007") is False
    assert Path(record["box"]).is_dir()
    assert not (Path(home) / ".ygg-worktrees/p/_removing").exists()


# ---------------------------------------------------------------- removal of folders an agent made unreadable

def test_an_unreadable_folder_an_agent_left_is_removed(home):
    record = _assemble(home)
    folder = Path(record["path"]) / "locked"
    folder.mkdir()
    os.chmod(folder, 0o000)
    assert clones.remove_test_clone("p", "test-su1.act-0007") is True
    assert not Path(record["box"]).exists()
    assert clones.read("p", "test-su1.act-0007") is None


def test_an_unreadable_folder_with_children_is_removed(home):
    record = _assemble(home)
    folder = Path(record["path"]) / "locked"
    inner = folder / "deeper"
    inner.mkdir(parents=True)
    (inner / "f").write_text("x")
    (folder / "g").write_text("y")
    os.chmod(inner / "f", 0o000)
    os.chmod(inner, 0o000)
    os.chmod(folder, 0o000)
    assert clones.remove_test_clone("p", "test-su1.act-0007") is True
    assert not Path(record["box"]).exists()


def test_a_link_inside_the_box_to_a_folder_outside_is_never_followed(home, tmp_path):
    record = _assemble(home)
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "keep.txt").write_text("mine")
    os.chmod(outside / "sub", 0o500)
    os.chmod(outside, 0o500)
    modes = {path: os.lstat(path).st_mode for path in (outside, outside / "sub", outside / "keep.txt")}
    folder = Path(record["path"]) / "locked"
    folder.mkdir()
    (folder / "out").symlink_to(outside, target_is_directory=True)
    (Path(record["path"]) / "out").symlink_to(outside, target_is_directory=True)
    os.chmod(folder, 0o000)
    try:
        assert clones.remove_test_clone("p", "test-su1.act-0007") is True
        assert not Path(record["box"]).exists()
        assert (outside / "keep.txt").read_text() == "mine"
        assert {path: os.lstat(path).st_mode for path in modes} == modes
    finally:
        os.chmod(outside, 0o700)
        os.chmod(outside / "sub", 0o700)


def test_a_removal_that_fails_any_way_holds_the_clone_and_shows_it(home, monkeypatch):
    record = _assemble(home)
    real_rmtree = clones.shutil.rmtree

    def broken(path, *args, **kwargs):
        raise TypeError("not an OSError")
    monkeypatch.setattr(clones.shutil, "rmtree", broken)
    monkeypatch.setattr(broken, "avoids_symlink_attacks", True, raising=False)
    assert clones.remove_test_clone("p", "test-su1.act-0007") is False
    held = clones.read("p", "test-su1.act-0007")
    stuck = Path(home) / ".ygg-worktrees/p/_removing/su1.act-0007"
    assert held["state"] == "removing" and held["reason"].startswith("could not be removed from " + str(stuck))
    from yggdrasil import status
    lines = status._held_test_clone_lines({"projects": [{"id": "p"}]})
    assert lines == ["test clone test-su1.act-0007 of p: removal held; " + held["reason"]]
    monkeypatch.setattr(clones.shutil, "rmtree", real_rmtree)
    assert clones.remove_test_clone("p", "test-su1.act-0007") is True  # the next try finishes it
    assert not stuck.exists() and status._held_test_clone_lines({"projects": [{"id": "p"}]}) == []


def test_one_stuck_test_clone_never_stops_the_sweep_or_the_settlement(home, monkeypatch):
    _assemble(home, "act-0007")
    second = _assemble(home, "act-0008")
    _settled(monkeypatch, "act-0007", "act-0008")
    real = clones.remove_test_clone

    def remove(project, name):
        if name == "test-su1.act-0007":
            raise TypeError("stuck")
        return real(project, name)
    monkeypatch.setattr(clones, "remove_test_clone", remove)
    assert clones.sweep_test_clones() == ["test-su1.act-0008"]
    assert not Path(second["box"]).exists()
    clones.settle_test_clone({"clone": {"kind": "test", "project": "p", "name": "test-su1.act-0007"}})


def test_a_box_that_cannot_be_moved_into_removing_is_held_and_shown(home, monkeypatch):
    _assemble(home)
    real_rename = clones.os.rename

    def refused(source, target):
        raise PermissionError("no write permission on _removing")
    monkeypatch.setattr(clones.os, "rename", refused)
    assert clones.remove_test_clone("p", "test-su1.act-0007") is False
    held = clones.read("p", "test-su1.act-0007")
    removing = Path(home) / ".ygg-worktrees/p/_removing"
    assert held["state"] == "removing"
    assert held["reason"] == "could not be moved into " + str(removing) + "; it is tried again on every tick"
    from yggdrasil import status
    assert status._held_test_clone_lines({"projects": [{"id": "p"}]}) == [
        "test clone test-su1.act-0007 of p: removal held; " + held["reason"]]
    monkeypatch.setattr(clones.os, "rename", real_rename)
    assert clones.remove_test_clone("p", "test-su1.act-0007") is True
    assert status._held_test_clone_lines({"projects": [{"id": "p"}]}) == []


def test_a_removing_record_with_no_reason_is_shown_as_pending(home):
    _assemble(home)
    record = clones.read("p", "test-su1.act-0007")
    record["state"] = "removing"
    clones.write("p", "test-su1.act-0007", record)
    from yggdrasil import status
    assert status._held_test_clone_lines({"projects": [{"id": "p"}]}) == [
        "test clone test-su1.act-0007 of p: removal started but not finished; it is tried again on every tick"]


def test_a_test_folder_that_cannot_be_searched_keeps_the_record_and_holds_it(home):
    record = _assemble(home)
    folder = Path(home) / ".ygg-worktrees/p/test"
    os.chmod(folder, 0o600)
    try:
        assert clones.remove_test_clone("p", "test-su1.act-0007") is False
        held = clones.read("p", "test-su1.act-0007")
        assert held is not None and held["state"] == "removing"
        assert held["reason"].startswith("could not be checked in " + str(folder))
        from yggdrasil import status
        assert status._held_test_clone_lines({"projects": [{"id": "p"}]}) == [
            "test clone test-su1.act-0007 of p: removal held; " + held["reason"]]
    finally:
        os.chmod(folder, 0o755)
    assert clones.remove_test_clone("p", "test-su1.act-0007") is True
    assert not Path(record["box"]).exists()


def test_a_removing_folder_that_cannot_be_searched_keeps_the_record_and_holds_it(home):
    _assemble(home)
    removing = Path(home) / ".ygg-worktrees/p/_removing"
    removing.mkdir(parents=True, exist_ok=True)
    os.chmod(removing, 0o600)
    try:
        assert clones.remove_test_clone("p", "test-su1.act-0007") is False
        held = clones.read("p", "test-su1.act-0007")
        assert held is not None and held["reason"].startswith("could not be checked in ")
    finally:
        os.chmod(removing, 0o755)
    assert clones.remove_test_clone("p", "test-su1.act-0007") is True




def test_rmtree_that_cannot_confirm_the_folder_is_gone_reports_failure(home, monkeypatch):
    record = _assemble(home)
    box = Path(record["box"])
    parent = box.parent

    def leaves_it_unsearchable(path, *args, **kwargs):
        os.chmod(parent, 0o600)  # the folder stays, and its parent can no longer be searched
    leaves_it_unsearchable.avoids_symlink_attacks = True
    monkeypatch.setattr(clones.shutil, "rmtree", leaves_it_unsearchable)
    try:
        assert clones._rmtree(box) is False
    finally:
        os.chmod(parent, 0o755)
    assert box.exists()
