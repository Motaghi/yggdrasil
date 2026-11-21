"""Rebuild task 6: the offline safety protocol. Stop and pause act locally first (marker, signals), persist
to the journal last, and resume clears exact intent ids; reconciliation completes stops, persists offline
intents and finishes interrupted resumes."""

import os
import subprocess
import sys
import time

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)
from test_rebuild_t3 import seed


def intents_in_journal(kind):
    from yggdrasil import journal
    return [e["intent"] for p in journal.all_programmes() for e in journal.entries(p)
            if e.get("kind") == kind and e.get("intent")]


def test_a_stop_signals_before_its_tracker_write_and_reports_a_failed_write(world, monkeypatch):
    from yggdrasil import interlock, journal, launcher, safety, stop
    identifier = seed("tl1", "--phase", "spawned", "--process", "live")
    agent = launcher.receipt(identifier)["pid"]
    monkeypatch.setattr(journal, "add", lambda *a, **k: (_ for _ in ()).throw(
        util.YggError(util.EXIT_TRACKER, "tracker write failed: read-only")))
    with pytest.raises(util.YggError) as error:
        stop.stop("alpha")
    assert error.value.code == util.EXIT_TRACKER
    assert "stopped 1 session(s); tracker write failed" in error.value.message
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and util.pid_alive(agent):
        time.sleep(0.05)
    assert not util.pid_alive(agent)
    assert [m["kind"] for m in safety.markers("alpha")] == ["stop"]
    assert "alpha" in interlock.stop_scopes()
    assert not (world / "state" / "stops.json").exists()  # no state-root marker any more


def test_a_stop_returns_pending_when_the_journal_lock_is_busy(world, monkeypatch):
    from yggdrasil import interlock, safety, stop
    monkeypatch.setattr(interlock, "JOURNAL_WAIT_SECONDS", 0.5)
    lock = safety.layout() / "locks" / "journal.lock"
    holder = subprocess.Popen([sys.executable, "-c",
                               "import fcntl, sys, time\nh = open(sys.argv[1], 'a+')\n"
                               "fcntl.flock(h, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(30)\n",
                               str(lock)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        started = time.monotonic()
        with pytest.raises(util.YggError) as error:
            stop.stop("alpha")
        assert time.monotonic() - started < 10
        assert error.value.code == util.EXIT_TRACKER and "tracker write pending" in error.value.message
        assert [m["kind"] for m in safety.markers("alpha")] == ["stop"]
    finally:
        holder.kill()
        holder.wait()


def test_reconciliation_persists_an_offline_intent_once(world):
    from yggdrasil import interlock, safety
    intent = safety.publish_marker("pause", "alpha")  # its journal step never ran
    interlock.reconcile()
    interlock.reconcile()
    assert intents_in_journal("pause") == [intent]
    assert interlock.paused("alpha")


def test_an_interrupted_resume_blocks_until_reconciliation_deletes_its_markers(world):
    from yggdrasil import commands, faults, interlock, safety
    commands.pause("alpha")
    real = faults.boundary

    def crash(name):
        if name == "after-resume-commit":
            raise RuntimeError("crash after the resume commit")
        real(name)

    faults.boundary = crash
    try:
        with pytest.raises(RuntimeError):
            commands.resume("alpha")
    finally:
        faults.boundary = real
    assert interlock.paused("alpha")  # the marker still blocks, though a committed resume lists it
    interlock.reconcile()
    assert not interlock.paused("alpha") and safety.markers("alpha") == []


def test_a_new_offline_pause_survives_an_older_resume(world):
    from yggdrasil import commands, interlock, safety
    commands.pause("alpha")
    commands.resume("alpha")
    newer = safety.publish_marker("pause", "alpha")  # offline: no journal fact yet
    interlock.reconcile()
    assert interlock.paused("alpha")
    assert intents_in_journal("pause")[-1] == newer
    assert [m["intent"] for m in safety.markers("alpha")] == [newer]


def test_resume_lists_journalled_intents_whose_markers_are_gone(world):
    from yggdrasil import commands, interlock, journal, safety
    commands.pause("alpha")
    [intent] = intents_in_journal("pause")
    safety.remove_markers([intent])  # e.g. the safety folder was restored from elsewhere
    assert interlock.paused("alpha")  # the journalled intent alone still blocks
    commands.resume("alpha")
    assert not interlock.paused("alpha")
    resumes = [e for p in journal.all_programmes() for e in journal.entries(p) if e.get("kind") == "resume"]
    assert intent in resumes[-1]["clears"]


def test_pause_writes_no_state_root_file(world):
    from yggdrasil import commands, status
    commands.pause("alpha")
    assert not list((world / "state").glob("pause*"))
    assert status.build_status()["paused"]["programmes"] == ["alpha"]


def test_a_supervisor_whose_agent_was_killed_by_a_signal_writes_no_rehydration_document(world, monkeypatch):
    from yggdrasil import launcher, tracker
    monkeypatch.setenv("YGG_UNIT_AGENT_SLEEP", "30")
    result = launcher.launch("tl1")
    activation = result["activation"]
    pid = launcher.receipt(activation)["pid"]
    document = tracker.document_path("p1", "rehydrate-tl1")
    before = document.stat().st_mtime_ns
    os.killpg(pid, 9)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and launcher.receipt(activation).get("exited_at") is None:
        time.sleep(0.05)
    assert launcher.receipt(activation)["exit_code"] < 0
    assert document.stat().st_mtime_ns == before
