"""v1.1.1 invariant T at `interlock.release_locks`: a stopped agent whose working folder is a lane clone (inside a
box) gets no trusted git and no stat of that folder, whatever its receipt says about the CLI; the tender's
`prepare --sole` removes a stale lock in a box. A worktree lane's stale lock is still removed as before."""

import json
import os
import subprocess

import pytest

from yggdrasil import interlock, launcher, util


@pytest.fixture
def stopped(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("YGG_STATE_ROOT", str(state))
    home = tmp_path / "home"
    box = home / ".ygg-worktrees" / "alpha-p1" / "lane" / "pm1tl1.g000001"
    checkout = box / "checkout"
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    (checkout / ".git" / "index.lock").write_text("")
    folder = state / "lane-clones" / "alpha-p1"
    folder.mkdir(parents=True)
    (folder / "pm1tl1.json").write_text(json.dumps({"kind": "lane", "generation": 1, "state": "ready",
                                                    "box": str(box), "path": str(checkout), "home": str(home)}))
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    (worktree / ".git" / "index.lock").write_text("")
    receipts = {}
    monkeypatch.setattr(launcher, "receipt", lambda key: receipts.get(key, {}))
    monkeypatch.setattr(interlock, "group_alive", lambda pid: False)
    monkeypatch.setattr(interlock, "_open_elsewhere", lambda path: False)
    calls = []
    real = util.trusted_git
    monkeypatch.setattr(util, "trusted_git", lambda *args, **kw: calls.append(args) or real(*args, **kw))
    return {"box": box, "checkout": checkout, "worktree": worktree, "receipts": receipts, "calls": calls}


@pytest.mark.parametrize("cli", ["codex", "claude-code", None])
def test_a_clone_working_folder_gets_no_git_whatever_the_receipt_says(stopped, cli, monkeypatch):
    stopped["receipts"]["act-1"] = dict({"cwd": str(stopped["checkout"])}, **({"cli": cli} if cli else {}))
    statted = []
    real_isdir = os.path.isdir
    monkeypatch.setattr(interlock.os.path, "isdir", lambda p: statted.append(str(p)) or real_isdir(p))
    notes = interlock.release_locks([(None, "act-1", 4242, None)])
    assert notes == []
    assert stopped["calls"] == []
    assert str(stopped["checkout"]) not in statted
    assert (stopped["checkout"] / ".git" / "index.lock").exists()


def test_a_folder_inside_a_box_is_skipped_too(stopped):
    inner = stopped["checkout"] / "sub"
    inner.mkdir()
    stopped["receipts"]["act-2"] = {"cwd": str(inner), "cli": "claude-code"}
    assert interlock.release_locks([(None, "act-2", 4242, None)]) == []
    assert stopped["calls"] == []


def test_a_worktree_lanes_stale_lock_is_still_removed(stopped):
    stopped["receipts"]["act-3"] = {"cwd": str(stopped["worktree"]), "cli": "claude-code"}
    notes = interlock.release_locks([(None, "act-3", 4242, None)])
    assert notes == ["index.lock removed: " + str(stopped["worktree"] / ".git" / "index.lock")]
    assert not (stopped["worktree"] / ".git" / "index.lock").exists()
