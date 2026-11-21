"""Review fixes for the offline safety protocol: stop targets from local evidence alone, every journal wait
inside stop bounded, no admission from cached stopped state, and no late dispatch for a given stamp."""

import subprocess
import sys
import time

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)
from test_rebuild_t3 import seed
from test_rebuild_t4 import given


def _dead(pid, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and util.pid_alive(pid):
        time.sleep(0.05)
    return not util.pid_alive(pid)


def test_stop_finds_a_live_agent_with_the_tracker_unreadable(world, monkeypatch):
    from yggdrasil import journal, launcher, lifecycle, state, stop
    identifier = seed("tl1", "--phase", "spawned", "--process", "live")
    agent = launcher.receipt(identifier)["pid"]
    unreadable = util.YggError(util.EXIT_TRACKER, "tracker unreachable")
    monkeypatch.setattr(state, "all_nodes", lambda *a, **k: (_ for _ in ()).throw(unreadable))
    monkeypatch.setattr(lifecycle, "open_activation", lambda *a, **k: (_ for _ in ()).throw(unreadable))
    monkeypatch.setattr(journal, "add", lambda *a, **k: (_ for _ in ()).throw(unreadable))
    with pytest.raises(util.YggError) as error:
        stop.stop("alpha")
    assert "stopped 1 session(s)" in error.value.message and error.value.code == util.EXIT_TRACKER
    assert _dead(agent)


def test_stop_finds_an_unrecorded_agent_by_its_locally_recorded_nonce(world, monkeypatch):
    from yggdrasil import interlock, launcher, lifecycle
    identifier = seed("tl1", "--phase", "spawned")
    launcher.update_receipt(identifier, supervisor_started_at="Mon Nov 16 01:00:00 2026", agent_nonce="n-1")
    orphan = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        monkeypatch.setattr(lifecycle, "orphan_agents",
                            lambda key, nonce, not_before: ("ok", [orphan.pid] if nonce == "n-1" else []))
        with util.locked(util.CREATION_LOCK):
            targets = interlock.collect_targets([], [], "alpha")  # no tracker evidence at all
        assert [(t[0], t[1], t[2]) for t in targets] == [("tl1", identifier, orphan.pid)]
    finally:
        orphan.kill()
        orphan.wait()


def test_stopping_a_live_target_never_waits_on_a_busy_journal(world, monkeypatch):
    from yggdrasil import interlock, launcher, safety, stop
    monkeypatch.setattr(interlock, "JOURNAL_WAIT_SECONDS", 0.5)
    identifier = seed("tl1", "--phase", "spawned", "--process", "live")
    agent = launcher.receipt(identifier)["pid"]
    lock = safety.layout() / "locks" / "journal.lock"
    holder = subprocess.Popen([sys.executable, "-c",
                               "import fcntl, sys, time\nh = open(sys.argv[1], 'a+')\n"
                               "fcntl.flock(h, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(60)\n",
                               str(lock)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        started = time.monotonic()
        with pytest.raises(util.YggError) as error:
            stop.stop("alpha")
        assert time.monotonic() - started < 15  # grace for the signal escalation, never the journal
        assert "stopped 1 session(s); tracker write pending" in error.value.message
        assert _dead(agent)
    finally:
        holder.kill()
        holder.wait()


def test_a_stale_stopped_state_without_an_intent_never_blocks(world):
    from yggdrasil import conductor, ratatoskr, state
    state.save_runtime("tl1", {"state": "stopped", "outcome": "aborted"})  # left by an interrupted resume
    ratatoskr.send("pm1", "tl1", "note", "work")
    assert "tl1" in conductor.candidates()


def test_a_dispatch_reserved_by_a_running_activation_is_not_delivered(world, monkeypatch):
    from yggdrasil import commands, journal, ratatoskr, tracker
    commands.tracker_write("pm1", "p1", "work-package", "Build", stage="1", package="WP-9")
    commands.tracker_write("su1", "p1", "criteria", "Test", package="WP-9")
    monkeypatch.setattr(ratatoskr, "deliver_dispatches", lambda project_id=None: None)
    commands.wp_authorise("p1", "WP-9", "tl1")
    monkeypatch.undo()
    [dispatch] = [s["id"] for s in tracker.read_issue("p1", "WP-9").header["owed"]
                  if s["reason"].startswith("dispatch:")]
    given("tl1", "act-0042", records=[dispatch])  # reserved, still being started
    ratatoskr.deliver_dispatches()
    assert not [e for e in journal.entries("alpha") if e.get("kind") == "inbox" and e.get("dispatch")]
