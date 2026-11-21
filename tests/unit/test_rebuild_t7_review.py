"""Final review fixes: scope restored on rebuilt records, pending stops before cleanup, positive identity
before any signal, release recovery from its fact, and uncertainty derived with no stored mirror."""

import shutil
import subprocess
import time

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)
from test_rebuild_t3 import seed


def test_an_adopted_agent_keeps_its_programme_scope_for_an_offline_stop(world, monkeypatch):
    from yggdrasil import interlock, launcher, lifecycle
    identifier = seed("tl1", "--phase", "spawned")
    shutil.rmtree(world / "state")  # the activation record is gone
    orphan = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        monkeypatch.setattr(lifecycle, "orphan_agents", lambda *a, **k: ("ok", [orphan.pid]))
        assert lifecycle.settle("tl1", identifier, {}, lifecycle.facts("alpha", identifier)) == "running"
        assert launcher.receipt(identifier)["programme"] == "alpha"
        with util.locked(util.CREATION_LOCK):
            targets = interlock.collect_targets([], [], "alpha")  # local evidence only
        assert [t[2] for t in targets] == [orphan.pid]
    finally:
        orphan.kill()
        orphan.wait()


def test_pending_stops_complete_before_any_cleanup(world, monkeypatch):
    from yggdrasil import conductor, interlock, rehydrate, upgrade
    order = []
    monkeypatch.setattr(interlock, "complete_stops", lambda exclude=None: order.append("stops"))
    monkeypatch.setattr(upgrade, "run", lambda **kw: order.append("cleanup"))
    conductor.tick()
    rehydrate.rehydrate_all()
    assert order == ["stops", "cleanup", "stops", "cleanup"]


def test_a_live_pid_of_unknown_identity_is_reported_never_signalled(world):
    from yggdrasil import launcher, stop
    identifier = seed("tl1", "--phase", "spawned")
    bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        launcher.update_receipt(identifier, pid=bystander.pid)  # no start time recorded: unknown identity
        with pytest.raises(util.YggError) as error:
            stop.stop("alpha")
        assert error.value.code == util.EXIT_UNCERTAIN and "identity unresolved" in error.value.message
        time.sleep(0.2)
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_recovery_finishes_a_release_committed_before_its_local_effects(world):
    from yggdrasil import conductor, launcher, lifecycle, ratatoskr, state, status
    identifier = seed("tl1", "--phase", "spawned", "--process", "dead")
    conductor.reconcile()
    assert identifier in status.build_status()["uncertain_keys"]
    lifecycle.commit("tl1", identifier, "released", released_as="aborted")  # the release stopped here
    conductor.reconcile()
    record = launcher.receipt(identifier)
    assert record["released_as"] == "aborted" and record["outcome"] == "aborted"
    runtime = state.runtime("tl1")
    assert runtime["state"] == "idle" and runtime["outcome"] == "aborted"
    assert identifier not in status.build_status()["uncertain_keys"]
    ratatoskr.send("pm1", "tl1", "note", "work")
    assert "tl1" in conductor.candidates()


def test_uncertainty_is_derived_and_no_mirror_is_kept(world):
    from yggdrasil import conductor, journal, reducer, state, status
    identifier = seed("tl1", "--phase", "spawned", "--process", "dead")
    conductor.reconcile()
    assert not (state.root() / "uncertain.json").exists()
    assert not [e for e in journal.entries("alpha") if e.get("kind") == "uncertain"]
    util.write_json(state.root() / "uncertain.json", ["act-0099"])  # a stale file is never read as truth
    view = status.build_status()
    assert view["uncertain_keys"] == [identifier] and reducer.shadow_diff(view) == {}
    shutil.rmtree(world / "state")
    assert status.build_status()["uncertain_keys"] == [identifier]


ADOPT_WITH_SLOW_TRACKER = """
import sys, time
from yggdrasil import lifecycle, state
orphan, key = int(sys.argv[1]), sys.argv[2]
lifecycle.orphan_agents = lambda *a, **k: ("ok", [orphan])
real = state.all_nodes

def slow(*a, **k):
    print("reading", flush=True)
    time.sleep(30)  # a hung tracker read during scope reconstruction
    return real(*a, **k)

state.all_nodes = slow
lifecycle.settle("tl1", key, {}, lifecycle.facts("alpha", key))
"""


def test_a_hung_tracker_read_while_rebuilding_a_record_never_holds_the_receipt_lock(world):
    import sys
    from yggdrasil import stop
    identifier = seed("tl1", "--phase", "spawned")
    shutil.rmtree(world / "state")
    orphan = subprocess.Popen(["sleep", "60"], start_new_session=True)
    adopter = subprocess.Popen([sys.executable, "-c", ADOPT_WITH_SLOW_TRACKER, str(orphan.pid), identifier],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert adopter.stdout.readline().strip() == "reading"
        with util.locked(util.RECEIPT_LOCK, timeout=2):  # free: the tracker read happens outside it
            pass
        started = time.monotonic()
        assert stop.stop("alpha").startswith("stopped")  # it never waits on the adopter's tracker read
        assert time.monotonic() - started < 10
    finally:
        adopter.kill()
        adopter.wait()
        orphan.kill()
        orphan.wait()
