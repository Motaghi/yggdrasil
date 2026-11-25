"""A start left pending for the second confirm window whose supervisor exits before committing `spawned`.

Two cases. A supervisor that has already exited at the first deadline is held as uncertain, the v1.0 rule that
existing tests pin: its gate write may or may not have landed. A supervisor that was alive at the first deadline
and dies inside the second window leaves a start that reconciliation aborts and revokes, as it does for any
unspawned reservation whose launch step has returned: the revocation stops an agent being made later."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from yggdrasil import util
from yggdrasil.util import YggError
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)

YGG = str(Path(sys.executable).parent / "ygg")


def _seed_reserved():
    return subprocess.run([YGG, "test", "activation", "tl1", "--phase", "reserved"], capture_output=True,
                          text=True, env=dict(os.environ)).stdout.strip()


def _dead():
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


class _ExitedSupervisor:
    def poll(self):
        return 1


def _a_returned_launch_with_a_dead_supervisor(identifier):
    from yggdrasil import launcher
    launcher.update_receipt(identifier, confirm_window_seconds=90.0, launch_returned_at=util.iso(),
                            confirm_left_pending_at=util.iso(), supervisor_pid=_dead(),
                            supervisor_started_at="exited")


def test_a_supervisor_that_dies_inside_the_window_gets_its_start_aborted_and_revoked(world):
    from yggdrasil import conductor, launcher, lifecycle
    identifier = _seed_reserved()
    _a_returned_launch_with_a_dead_supervisor(identifier)
    conductor.reconcile()
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) == "aborted"
    assert launcher.receipt(identifier)["terminal"] == "aborted"
    assert lifecycle.revoked_path(identifier).exists()


def test_a_start_whose_supervisor_already_exited_is_still_held_at_the_first_deadline(world):
    """Not a proven failure: its gate write may or may not have landed, so it is held for the user, as the
    v1.0 tests `test_held_release` and `test_linear_gate_conflict` pin, with or without a recorded window."""
    from yggdrasil import launcher, lifecycle
    identifier = _seed_reserved()
    launcher.update_receipt(identifier, confirm_window_seconds=90.0)
    assert launcher.confirm("tl1", identifier, _ExitedSupervisor())["outcome"] == "uncertain"
    assert launcher.receipt(identifier)["outcome"] == "uncertain"


def test_a_live_supervisor_at_the_first_deadline_leaves_the_start_pending(world):
    from yggdrasil import launcher

    class Alive:
        def poll(self):
            return None

    identifier = _seed_reserved()
    launcher.update_receipt(identifier, confirm_window_seconds=90.0)
    assert launcher.confirm("tl1", identifier, Alive())["outcome"] == "pending"
    assert launcher.receipt(identifier)["outcome"] == "pending"


def test_without_a_recorded_window_the_first_deadline_still_holds_the_start(world):
    from yggdrasil import launcher
    identifier = _seed_reserved()
    result = launcher.confirm("tl1", identifier, _ExitedSupervisor())
    assert result["outcome"] == "uncertain"


# A launcher killed before `confirm` recorded a pending decision (the A8 launcher-race rows): recovery aborts and
# revokes the reservation even though its supervisor is alive and waiting before the gate, with or without a recorded
# window. (A launcher killed after the decision is another case: that start stays pending.)
def _live_supervisor(identifier, **fields):
    from yggdrasil import launcher, lifecycle
    me = lifecycle.own_identity()  # this test process stands for the supervisor: alive
    launcher.update_receipt(identifier, supervisor_pid=me["pid"], supervisor_started_at=me["started_at"], **fields)


def test_a_launcher_killed_before_confirm_decided_is_aborted_and_revoked_whatever_its_supervisor_does(world):
    from yggdrasil import conductor, launcher, lifecycle
    identifier = _seed_reserved()  # its launcher is dead and never recorded a return
    _live_supervisor(identifier, confirm_window_seconds=90.0)
    conductor.reconcile()
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) == "aborted"
    assert launcher.receipt(identifier)["terminal"] == "aborted"
    assert lifecycle.revoked_path(identifier).exists()


def test_a_start_that_confirm_left_pending_with_a_live_supervisor_is_left_for_the_second_window(world):
    from yggdrasil import conductor, launcher, lifecycle
    identifier = _seed_reserved()
    _live_supervisor(identifier, confirm_window_seconds=90.0, launch_returned_at=util.iso(),
                     confirm_left_pending_at=util.iso())
    conductor.reconcile()
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) is None
    assert launcher.receipt(identifier)["outcome"] == "pending"
    assert not lifecycle.revoked_path(identifier).exists()


def test_without_a_recorded_window_a_dead_launcher_with_a_live_supervisor_is_aborted(world):
    from yggdrasil import conductor, lifecycle
    identifier = _seed_reserved()
    _live_supervisor(identifier)
    conductor.reconcile()
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) == "aborted"


# The deliberate decision has a field of its own: a launch step that merely returned is not enough.
class _Flip:
    """A supervisor whose first poll shows it alive past the wait loop's end, as the first deadline needs; every
    later poll says it is still running."""

    def __init__(self):
        self.calls = 0

    def poll(self):
        self.calls += 1
        return 1 if self.calls == 1 else None


@pytest.mark.parametrize("failure", [YggError(util.EXIT_TRACKER, "tracker unavailable"), OSError("disk"),
                                     KeyboardInterrupt()], ids=["yggerror", "oserror", "interrupt"])
def test_a_confirm_step_that_fails_or_is_interrupted_leaves_a_start_that_recovery_aborts_and_revokes(world, monkeypatch,
                                                                                                   failure):
    from yggdrasil import conductor, launcher, lifecycle
    identifier = _seed_reserved()
    _live_supervisor(identifier, confirm_window_seconds=90.0)

    def broken(*args, **kwargs):
        assert util._held()[0].get("journal"), "the fault must fire in the decision, under its lock"
        raise failure

    with monkeypatch.context() as patched:
        patched.setattr(launcher, "classify", broken)
        with pytest.raises(type(failure)):
            launcher.confirm("tl1", identifier, _Flip())
    assert not launcher.receipt(identifier).get("confirm_left_pending_at")  # no decision was recorded
    launcher.launch_returned(identifier)  # what launch()'s `finally` does after a confirm that raised
    conductor.reconcile()
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) == "aborted"
    assert lifecycle.revoked_path(identifier).exists()


def test_confirm_records_its_decision_in_its_own_field_when_it_leaves_a_start_pending(world):
    from yggdrasil import launcher
    identifier = _seed_reserved()
    launcher.update_receipt(identifier, confirm_window_seconds=90.0)
    assert launcher.confirm("tl1", identifier, _Flip())["outcome"] == "pending"
    assert launcher.receipt(identifier)["confirm_left_pending_at"]


def test_a_decision_that_lands_after_the_first_read_is_seen_under_the_lock_and_the_start_stays_pending(
        world, monkeypatch):
    """The interleaving: recovery reads the receipt (no decision yet) and probes the launcher (dead); only then does
    `confirm` record its decision. The abort path must read the receipt again under the lock and stand down."""
    from yggdrasil import conductor, launcher, lifecycle
    identifier = _seed_reserved()
    _live_supervisor(identifier, confirm_window_seconds=90.0)
    real = lifecycle.launch_step
    landed = []

    def probe_then_decide(found, data):
        result = real(found, data)  # the launcher is found dead ...
        if not landed:
            landed.append(True)  # ... and only now does the decision land
            launcher.update_receipt(identifier, confirm_left_pending_at=util.iso(), launch_returned_at=util.iso())
        return result

    monkeypatch.setattr(lifecycle, "launch_step", probe_then_decide)
    conductor.reconcile()
    assert landed
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) is None
    assert launcher.receipt(identifier)["outcome"] == "pending"
    assert not lifecycle.revoked_path(identifier).exists()


def test_a_start_left_pending_whose_supervisor_evidence_is_unknown_is_held_at_the_window_end_never_aborted(
        world, monkeypatch):
    from yggdrasil import conductor, launcher, lifecycle
    identifier = _seed_reserved()
    me = lifecycle.own_identity()
    # alive, but its start identity is unavailable: the evidence is unknown, not alive and not dead
    launcher.update_receipt(identifier, confirm_window_seconds=90.0, launch_returned_at=util.iso(),
                            confirm_left_pending_at=util.iso(), supervisor_pid=me["pid"], supervisor_started_at=None)
    assert lifecycle.unspawned_evidence(lifecycle.facts("alpha", identifier), launcher.receipt(identifier)) == "unknown"
    conductor.reconcile()
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) is None
    assert launcher.receipt(identifier)["outcome"] == "pending" and not lifecycle.revoked_path(identifier).exists()
    monkeypatch.setenv("YGG_NOW", "2026-11-16T02:05:00Z")  # past the 90 s window
    conductor.reconcile()
    conductor.reconcile()
    assert launcher.receipt(identifier)["outcome"] == "uncertain"
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) is None
    assert not lifecycle.revoked_path(identifier).exists()


def test_a_start_left_pending_whose_supervisor_is_dead_is_aborted_and_revoked(world):
    from yggdrasil import conductor, launcher, lifecycle
    identifier = _seed_reserved()
    _a_returned_launch_with_a_dead_supervisor(identifier)
    conductor.reconcile()
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) == "aborted"
    assert lifecycle.revoked_path(identifier).exists()


@pytest.mark.parametrize("window", [None, 90.0], ids=["switch-off", "window-recorded-without-a-decision"])
def test_a_confirm_that_holds_the_start_between_the_first_read_and_the_probe_still_ends_in_v1_abort(
        world, monkeypatch, window):
    """The interleaving: recovery reads an unspawned receipt (outcome pending), the launcher is found dead, and only
    then does `confirm` record `uncertain` and the launcher exit; the supervisor is dead. With no pending decision,
    v1.0 re-checks terminal and spawned facts only, then revokes and aborts: so does this, with the switch off or
    with a window recorded but no decision."""
    from yggdrasil import conductor, launcher, lifecycle
    identifier = _seed_reserved()
    fields = {"supervisor_pid": _dead(), "supervisor_started_at": "exited"}
    if window:
        fields["confirm_window_seconds"] = window
    launcher.update_receipt(identifier, **fields)
    real = lifecycle.launch_step
    landed = []

    def probe_then_hold(found, data):
        result = real(found, data)
        if not landed:
            landed.append(True)
            launcher.update_receipt(identifier, outcome="uncertain", uncertain_reason="no process reported",
                                    launch_returned_at=util.iso())
        return result

    monkeypatch.setattr(lifecycle, "launch_step", probe_then_hold)
    conductor.reconcile()
    assert landed
    assert lifecycle.terminal_of(lifecycle.facts("alpha", identifier)) == "aborted"
    assert lifecycle.revoked_path(identifier).exists()


def test_the_pending_decision_is_a_write_once_field(world):
    from yggdrasil import launcher
    identifier = _seed_reserved()
    launcher.update_receipt(identifier, confirm_left_pending_at="2026-11-16T02:00:00Z")
    launcher.update_receipt(identifier, confirm_left_pending_at=None)  # a writer holding an older copy
    assert launcher.receipt(identifier)["confirm_left_pending_at"] == "2026-11-16T02:00:00Z"
