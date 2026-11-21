"""Rebuild task 1: the durable write primitive, the safety folder, the two locks and the dual-written
interlock markers."""

import json
import os

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


# Durable writes: the file is flushed before the rename and its directory after it.
def test_atomic_write_flushes_file_and_directory(world, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd) or real(fd))
    util.atomic_write(world / "state" / "probe.json", "{}\n")
    assert len(calls) >= 2


def test_append_line_flushes(world, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd) or real(fd))
    util.append_line(world / "state" / "probe.jsonl", {"a": 1})
    assert calls


# Torn or unparseable store files are reported, never read as empty.
@pytest.mark.parametrize("text", ["", "{\"half\": ", "not json"])
def test_torn_store_file_is_reported(world, text):
    path = world / "state" / "uncertain.json"
    path.write_text(text)
    with pytest.raises(util.YggError) as error:
        util.read_json(path, [])
    assert "corrupt store file" in error.value.message


def test_missing_store_file_reads_as_default(world):
    assert util.read_json(world / "state" / "absent.json", []) == []


def test_tracker_files_keep_their_own_handling(world):
    path = world / "tracker" / "loose.json"
    path.write_text("not json")
    assert util.read_json(path, "default") == "default"


def test_jsonl_reports_a_torn_final_record(world):
    path = world / "state" / "log.jsonl"
    path.write_text('{"a": 1}\n{"b": 2')
    with pytest.raises(util.YggError) as error:
        util.read_lines(path)
    assert "corrupt store file" in error.value.message
    path.write_text('{"a": 1}\nbroken\n{"c": 3}\n')
    with pytest.raises(util.YggError):
        util.read_lines(path)
    path.write_text('{"a": 1}\n')
    assert util.read_lines(path) == [{"a": 1}]


def test_directory_sync_failure_fails_the_write(world, monkeypatch):
    from yggdrasil import faults
    counted = []
    monkeypatch.setattr(faults, "after_write", lambda path: counted.append(path))
    monkeypatch.setattr(util, "fsync_dir", lambda path: (_ for _ in ()).throw(OSError(5, "EIO")))
    with pytest.raises(OSError):
        util.atomic_write(world / "state" / "probe.json", "{}\n")
    assert counted == []


# The safety folder: a sibling of the state root, created durably and recorded once.
def test_safety_folder_is_a_sibling_and_established_once(world):
    from yggdrasil import journal, safety
    assert safety.root() == world / "state.safety"
    first = safety.ensure_established()
    second = safety.ensure_established()
    assert first == second and first.startswith("sf-")
    assert {p.name for p in safety.root().iterdir()} >= {"interlocks", "revoked", "no-agent", "locks", "SAFETY_ROOT_ID"}
    facts = [e for e in journal.entries(None) if e.get("kind") == "safety-root-established"]
    assert [e["id"] for e in facts] == [first]


def test_safety_root_override_needs_test_mode_without_a_session(world, monkeypatch):
    from yggdrasil import safety
    monkeypatch.setenv("YGG_SAFETY_ROOT", str(world / "elsewhere"))
    assert safety.root() == world / "elsewhere"
    monkeypatch.setenv("YGG_NODE", "pm1")
    assert safety.root() == world / "state.safety"


def test_state_wipe_leaves_the_safety_folder(world):
    import shutil
    from yggdrasil import commands, safety
    commands.pause("alpha")
    shutil.rmtree(world / "state")
    assert [m["scope"] for m in safety.markers("alpha")] == ["alpha"]


def test_established_folder_missing_fails_admission_closed(world):
    import shutil
    from yggdrasil import launcher, safety
    safety.ensure_established()
    shutil.rmtree(safety.root())
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1")
    assert error.value.code == 3 and "missing or unreadable" in error.value.message
    assert not (world / "agent.log").exists()


# The locks: stable inodes, and nothing acquired under the creation lock.
def test_lock_files_keep_their_inode(world):
    from yggdrasil import safety
    with util.locked():
        pass
    before = safety.lock_inode("journal")
    for _ in range(3):
        with util.locked():
            pass
    assert safety.lock_inode("journal") == before


def test_journal_lock_is_never_taken_under_the_creation_lock(world):
    with util.locked(util.CREATION_LOCK):
        with pytest.raises(RuntimeError, match="lock order"):
            with util.locked():
                pass
    with util.locked():
        with util.locked(util.CREATION_LOCK):
            with pytest.raises(RuntimeError, match="lock order"):
                with util.locked():  # re-entering the journal lock under the creation lock is refused too
                    pass


def test_lock_depth_is_per_thread(world):
    import threading
    seen = {}

    def other():
        try:
            seen["held_here"] = util.lock_held(util.RECEIPT_LOCK)
            with util.locked():  # another thread's receipt lock is no lock-order violation here
                seen["ok"] = True
        except Exception as error:  # noqa: BLE001
            seen["error"] = error

    with util.locked(util.RECEIPT_LOCK):
        worker = threading.Thread(target=other)
        worker.start()
        worker.join(10)
    assert seen == {"held_here": False, "ok": True}


def test_a_lock_held_by_one_thread_blocks_another(world):
    import threading
    import time
    order = []

    def other():
        with util.locked():
            order.append("other")

    with util.locked():
        worker = threading.Thread(target=other)
        worker.start()
        time.sleep(0.3)
        order.append("main")
    worker.join(10)
    assert order == ["main", "other"]


def test_unreadable_safety_folder_refuses_with_exit_3(world, monkeypatch):
    from yggdrasil import launcher, safety
    monkeypatch.setattr(safety, "layout", lambda: (_ for _ in ()).throw(PermissionError(13, "denied")))
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1")
    assert error.value.code == 3 and "missing or unreadable" in error.value.message


def test_admission_needs_a_committed_establishment_fact(world, monkeypatch):
    from yggdrasil import journal, launcher, safety
    journal.clear(None, safety.ESTABLISHED)  # as if the first establishment write never committed
    real_add = journal.add
    monkeypatch.setattr(safety, "_BOOTSTRAPPED", set())

    def failing_add(programme, kind, **fields):
        if kind == safety.ESTABLISHED:
            raise util.YggError(8, "tracker write failed")
        return real_add(programme, kind, **fields)

    monkeypatch.setattr(journal, "add", failing_add)
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1")
    assert error.value.code == 3 and "not established" in error.value.message


def test_conflicting_establishment_records_fail_closed(world):
    from yggdrasil import journal, safety
    safety.ensure_established()
    journal.add(None, safety.ESTABLISHED, id="sf-0000000000000000")
    with pytest.raises(util.YggError) as error:
        safety.check_admission()
    assert "conflict" in error.value.message


def test_untrusted_or_cleared_establishment_records_are_ignored(world):
    from yggdrasil import journal, safety
    current = safety.ensure_established()
    journal.add(None, safety.ESTABLISHED, id="sf-1111111111111111", actor="pm1")
    assert safety._recorded_id() == current
    journal.clear(None, safety.ESTABLISHED, id=current)
    assert safety._recorded_id() is None


# Interlock markers are dual-written by today's stop, pause and resume.
def test_pause_and_stop_publish_intent_markers_and_journal_them(world):
    from yggdrasil import commands, journal, safety, stop
    commands.pause("alpha")
    stop.stop("alpha")
    found = {m["kind"]: m for m in safety.markers("alpha")}
    assert set(found) == {"pause", "stop"}
    assert all(m["intent"].startswith("si-") and len(m["intent"]) == 19 for m in found.values())
    entries = {e["kind"]: e for e in journal.entries("alpha") if e.get("kind") in ("pause", "stop")}
    assert entries["pause"]["intent"] == found["pause"]["intent"]
    assert entries["stop"]["intent"] == found["stop"]["intent"]
    marker = json.loads((world / "state.safety" / "interlocks" / "alpha" / (found["stop"]["intent"] + ".json")).read_text())
    assert marker["kind"] == "stop" and marker["scope"] == "alpha"


def test_resume_removes_the_markers_of_its_scope(world):
    from yggdrasil import commands, journal, safety
    commands.pause("alpha")
    commands.pause()
    commands.resume("alpha")
    assert safety.markers("alpha") == []
    assert [m["scope"] for m in safety.markers()] == ["*"]
    resumes = [e for e in journal.entries("alpha") if e.get("kind") == "resume"]
    assert resumes and resumes[-1]["scope"] == "alpha" and len(resumes[-1]["clears"]) == 1
    commands.resume()
    assert safety.markers() == []


def test_resume_never_removes_a_marker_published_after_its_snapshot(world, monkeypatch):
    from yggdrasil import commands, safety
    commands.pause("alpha")
    real = safety.marker_ids
    late = {}

    def snapshot_then_stop(scope=None):
        ids = real(scope)
        late["id"] = safety.publish_marker("stop", "alpha")  # a stop lands after the snapshot
        return ids

    monkeypatch.setattr(safety, "marker_ids", snapshot_then_stop)
    commands.resume("alpha")
    assert [m["intent"] for m in safety.markers("alpha")] == [late["id"]]


def test_stop_signals_despite_corrupt_state_files(world):
    import subprocess
    from yggdrasil import stop
    seeded = subprocess.run([str(__import__("pathlib").Path(__import__("sys").executable).parent / "ygg"), "test",
                             "activation", "tl1", "--state", "running", "--process", "live"],
                            capture_output=True, text=True, env=dict(os.environ)).stdout.strip()
    from yggdrasil import launcher
    pid = launcher.receipt(seeded)["pid"]
    (world / "state" / "nodes" / "tl1.json").write_text("{torn")
    (world / "state" / "activations" / "act-0099.json").write_text("{torn")  # a record stop reads: reported
    with pytest.raises(util.YggError) as error:
        stop.stop("alpha")
    assert "state store error" in error.value.message and "stopped 1 session(s)" in error.value.message
    import time
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and util.pid_alive(pid):
        time.sleep(0.05)
    assert not util.pid_alive(pid)


# Round 2: an undecodable state file is a corrupt store too; stop still signals and reports.
def test_stop_signals_despite_undecodable_state_files(world):
    import subprocess
    import sys
    import time
    from pathlib import Path
    from yggdrasil import launcher, stop
    seeded = subprocess.run([str(Path(sys.executable).parent / "ygg"), "test", "activation", "tl1", "--state",
                             "running", "--process", "live"], capture_output=True, text=True,
                            env=dict(os.environ)).stdout.strip()
    pid = launcher.receipt(seeded)["pid"]
    (world / "state" / "nodes" / "tl1.json").write_bytes(b"\xff\xfe\xfa")
    (world / "state" / "activations" / "act-0099.json").write_bytes(b"\xff\xff")  # read by stop: reported
    with pytest.raises(util.YggError) as error:
        stop.stop("alpha")
    assert "state store error" in error.value.message and "stopped 1 session(s)" in error.value.message
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and util.pid_alive(pid):
        time.sleep(0.05)
    assert not util.pid_alive(pid)


# Round 2: the launch folder is made durable before a settings file is written in it.
def test_launch_folder_sync_failure_refuses_the_launch(world, monkeypatch):
    from yggdrasil import launcher
    real = util.ensure_dir

    def failing(path):
        if str(path).endswith("/launch"):
            raise OSError(5, "EIO")  # the folder entry could not be synced
        return real(path)

    monkeypatch.setattr(util, "ensure_dir", failing)
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1")
    assert error.value.code == 3 and "hook injection failed" in error.value.message
    assert not (world / "agent.log").exists()
