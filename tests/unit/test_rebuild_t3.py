"""Rebuild task 3: activation lifecycle facts, process evidence and recovery from them."""

import os
import subprocess
import sys
from pathlib import Path

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)

YGG = str(Path(sys.executable).parent / "ygg")


def seed(*args):
    return subprocess.run([YGG, "test", "activation", *args], capture_output=True, text=True,
                          env=dict(os.environ)).stdout.strip()


def test_first_terminal_fact_wins(world):
    from yggdrasil import lifecycle
    identifier = seed("tl1", "--phase", "spawned", "--process", "dead")
    assert lifecycle.commit("tl1", identifier, "exited", exit_code=0)
    assert not lifecycle.commit("tl1", identifier, "aborted", reason="late")
    found = lifecycle.facts("alpha", identifier)
    assert lifecycle.terminal_of(found) == "exited"
    assert [e["phase"] for e in found] == ["reserved", "spawned", "exited"]


def test_evidence_is_alive_dead_or_unknown(world, monkeypatch):
    from yggdrasil import lifecycle
    me = lifecycle.own_identity()
    assert lifecycle.evidence(me["pid"], me["started_at"]) == "alive"
    assert lifecycle.evidence(me["pid"], "Thu Jan  1 00:00:00 1970") == "dead"
    assert lifecycle.evidence(me["pid"], None) == "unknown"  # a missing start time never matches
    assert lifecycle.evidence(None, None) == "dead"
    monkeypatch.setenv("YGG_TEST_PROBE", "unknown")
    assert lifecycle.evidence(me["pid"], me["started_at"]) == "unknown"


def test_reservation_with_a_dead_launcher_is_aborted_and_its_work_stays_owed(world):
    from yggdrasil import conductor, launcher, lifecycle, ratatoskr
    ratatoskr.send("pm1", "tl1", "note", "hello")
    identifier = seed("tl1", "--phase", "reserved")
    conductor.reconcile()
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) == "aborted"
    assert launcher.receipt(identifier)["terminal"] == "aborted"


def test_spawned_with_no_evidence_is_uncertain_never_aborted(world):
    from yggdrasil import conductor, launcher, lifecycle, status
    identifier = seed("tl1", "--phase", "spawned", "--process", "dead")
    conductor.reconcile()
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) is None
    assert identifier in status.build_status()["uncertain_keys"]
    assert launcher.receipt(identifier).get("terminal") is None


def test_unknown_evidence_is_a_visible_hold_that_is_not_stored(world, monkeypatch):
    from yggdrasil import conductor, journal, status
    identifier = seed("tl1", "--phase", "spawned", "--process", "live")
    monkeypatch.setenv("YGG_TEST_PROBE", "unknown")
    conductor.reconcile()
    assert identifier in status.build_status()["uncertain_keys"]
    assert not [e for e in journal.entries("alpha") if e.get("kind") == "uncertain" and e.get("key") == identifier]
    monkeypatch.delenv("YGG_TEST_PROBE")
    conductor.reconcile()
    view = status.build_status()
    assert identifier not in view["uncertain_keys"]
    os.killpg(__import__("yggdrasil").launcher.receipt(identifier)["pid"], 9)


def test_no_agent_record_is_positive_evidence(world):
    from yggdrasil import conductor, lifecycle
    identifier = seed("tl1", "--phase", "spawned")
    lifecycle.write_local(lifecycle.no_agent_path(identifier), {"activation": identifier, "reason": "test"})
    conductor.reconcile()
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) == "aborted"
    assert not lifecycle.no_agent_path(identifier).exists()


def test_receipt_lock_is_a_leaf(world):
    import pytest
    with util.locked(util.RECEIPT_LOCK):
        with pytest.raises(RuntimeError, match="receipt lock"):
            with util.locked():
                pass
    with util.locked(util.CREATION_LOCK):
        with util.locked(util.RECEIPT_LOCK):
            pass  # identity is recorded under the creation lock


# Adoption reads each candidate's real environment: a decoy whose arguments carry the strings is ignored,
# the nonce is required, and two matches are never guessed between.
def _spawn_orphan(env: dict, *argv: str) -> int:
    import tempfile
    marker = tempfile.mktemp()
    command = '"%s" -c "import time; time.sleep(30)" %s </dev/null >/dev/null 2>&1 & echo $! > "%s"' % (
        sys.executable, " ".join("'" + a + "'" for a in argv), marker)
    subprocess.run(["/bin/sh", "-c", command], env=env, check=True)
    import time
    for _ in range(50):
        if os.path.exists(marker) and open(marker).read().strip():
            break
        time.sleep(0.02)
    return int(open(marker).read().strip())


def test_adoption_ignores_arguments_and_requires_the_nonce(world):
    import signal
    import time
    from yggdrasil import lifecycle
    root = str(util.state_root())
    base = {k: v for k, v in os.environ.items() if k != "YGG_ACTIVATION"}
    decoy = _spawn_orphan(base, "YGG_ACTIVATION=act-0777", "YGG_STATE_ROOT=" + root)
    real = _spawn_orphan(dict(base, YGG_ACTIVATION="act-0777", YGG_STATE_ROOT=root, YGG_ACTIVATION_NONCE="n-1"))
    try:
        time.sleep(0.5)
        status, pids = lifecycle.orphan_agents("act-0777", "n-1")
        assert status == "ok" and pids == [real]
        assert lifecycle.orphan_agents("act-0777", "other-nonce") == ("ok", [])
    finally:
        for pid in (decoy, real):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_an_unreadable_candidate_fails_closed(world, monkeypatch):
    from yggdrasil import lifecycle
    other = subprocess.Popen(["sleep", "30"])
    try:
        monkeypatch.setattr(lifecycle, "_candidates", lambda not_before: ("ok", [os.getppid(), other.pid]))
        real_env = {"YGG_ACTIVATION": "act-0888", "YGG_STATE_ROOT": str(util.state_root())}
        monkeypatch.setattr(lifecycle, "process_environment",
                            lambda pid: real_env if pid == os.getppid() else None)
        assert lifecycle.orphan_agents("act-0888") == ("unknown", [])  # one matches, one cannot be read
    finally:
        other.kill()


def test_two_matching_processes_are_never_adopted(world, monkeypatch):
    from yggdrasil import lifecycle
    identifier = seed("tl1", "--phase", "spawned")
    monkeypatch.setattr(lifecycle, "orphan_agents",
                        lambda activation, nonce=None, not_before=None: ("ok", [os.getpid(), os.getppid()]))
    data = __import__("yggdrasil").launcher.receipt(identifier)
    assert lifecycle.settle("tl1", identifier, data, lifecycle.facts("alpha", identifier)) == "unsettled"


# Review round 1 of task 3.
def test_corrupt_no_agent_record_is_held_not_proof(world):
    from yggdrasil import conductor, lifecycle, status
    identifier = seed("tl1", "--phase", "spawned")
    lifecycle.no_agent_path(identifier).write_text("{torn")
    conductor.reconcile()
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) is None
    assert identifier in status.build_status()["uncertain_keys"]
    lifecycle.no_agent_path(identifier).write_text('{"activation": "act-9999"}')  # names another activation
    assert lifecycle.no_agent_record(identifier) is None


def test_confirmed_activation_with_dead_agent_and_no_exit_is_held_before_any_retry(world):
    from yggdrasil import conductor, launcher, lifecycle
    identifier = seed("tl1", "--phase", "spawned", "--process", "dead")
    launcher.update_receipt(identifier, outcome="confirmed", phase="launched")
    conductor.reconcile()
    _, relaunch = conductor.heimdall_scan()
    assert not [r for r in relaunch if r["node"] == "tl1"]
    assert launcher.receipt(identifier)["outcome"] == "uncertain"
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) is None


def test_open_activation_after_a_wipe_blocks_a_second_reservation(world):
    import shutil
    import pytest
    from yggdrasil import conductor, launcher, lifecycle
    identifier = seed("tl1", "--phase", "spawned", "--process", "live")
    pid = launcher.receipt(identifier)["pid"]
    try:
        shutil.rmtree(world / "state")
        conductor.reconcile()
        with pytest.raises(util.YggError) as error:
            launcher.launch("tl1")
        assert error.value.code == 3
        assert lifecycle.open_activation("tl1") == identifier
    finally:
        os.killpg(pid, 9)


def test_release_refuses_live_or_unknown_evidence_and_keeps_another_terminal(world, monkeypatch):
    import pytest
    from yggdrasil import conductor, launcher, lifecycle, noderepair
    identifier = seed("tl1", "--phase", "spawned", "--process", "dead")
    conductor.reconcile()  # held as uncertain
    monkeypatch.setenv("YGG_TEST_PROBE", "unknown")
    with pytest.raises(util.YggError, match="unknown"):
        noderepair.release("tl1", "aborted")
    monkeypatch.delenv("YGG_TEST_PROBE")
    assert lifecycle.commit("tl1", identifier, "exited", exit_code=0)  # a late exit fact lands first
    with pytest.raises(util.YggError, match="no uncertain activation"):
        noderepair.release("tl1", "aborted")  # settled by its fact: nothing is held any more
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) == "exited"
    assert launcher.receipt(identifier).get("outcome") != "aborted"


def test_release_commits_the_released_fact(world):
    from yggdrasil import conductor, lifecycle, noderepair
    identifier = seed("tl1", "--phase", "spawned", "--process", "dead")
    conductor.reconcile()
    noderepair.release("tl1", "failed")
    found = lifecycle.facts("alpha", identifier)
    assert lifecycle.terminal_of(found) == "released"
    assert lifecycle.revoked_path(identifier).exists()


def _supervisor_fact(identifier, pid):
    from yggdrasil import journal, launcher
    started = util.process_start(pid)
    journal.clear_where("alpha", lambda e: e.get("kind") == "activation" and e.get("phase") == "spawned"
                        and e.get("key") == identifier)
    journal.add("alpha", "activation", key=identifier, node="tl1", phase="spawned", supervisor_pid=pid,
                supervisor_started_at=started)
    return started


def test_reset_kills_a_verified_supervisor_and_commits_facts(world):
    import time
    from yggdrasil import journal, launcher, lifecycle, noderepair, state
    identifier = seed("tl1", "--phase", "spawned", "--process", "live")
    agent = launcher.receipt(identifier)["pid"]
    supervisor = _spawn_orphan(dict(os.environ, YGG_SUPERVISOR_FOR=identifier, YGG_STATE_ROOT=str(util.state_root())))
    time.sleep(0.3)
    started = _supervisor_fact(identifier, supervisor)
    launcher.update_receipt(identifier, supervisor_pid=supervisor, supervisor_started_at=started)
    state.set_state("tl1", "stalled-suspect")
    noderepair.reset("tl1", kill=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (util.pid_alive(agent) or util.pid_alive(supervisor)):
        time.sleep(0.05)
    assert not util.pid_alive(agent) and not util.pid_alive(supervisor)
    found = lifecycle.facts("alpha", identifier)
    assert lifecycle.terminal_of(found) == "exited"
    assert any(e.get("kind") == "chain-reset" and e.get("node") == "tl1" for e in journal.entries("alpha"))
    assert state.runtime("tl1")["state"] == "idle"


def test_reset_never_kills_an_unverified_supervisor_pid(world):
    import pytest
    from yggdrasil import launcher, noderepair, state
    identifier = seed("tl1", "--phase", "spawned", "--process", "live")
    agent = launcher.receipt(identifier)["pid"]
    bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)  # pid named by tracker data only
    try:
        _supervisor_fact(identifier, bystander.pid)
        state.set_state("tl1", "stalled-suspect")
        with pytest.raises(util.YggError) as error:
            noderepair.reset("tl1", kill=True)
        assert error.value.code == 7 and "could not be verified" in error.value.message
        assert bystander.poll() is None  # never signalled
        assert util.pid_alive(agent)
    finally:
        bystander.kill()
        os.killpg(agent, 9)
