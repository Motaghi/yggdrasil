"""Pilot run, 20 Nov: a start held as uncertain before `spawned` is released by the user while the conductor that
reserved it still runs, and only once nothing of it can still start the agent (A7 I1, A7.5; A11 AC-43 row 3)."""

import shutil
import subprocess
import sys
from datetime import timedelta

import pytest

from yggdrasil import util
from test_round2 import world  # noqa: F401  (shared fixture)


@pytest.fixture
def sleeper():
    process = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    yield process
    process.kill()
    process.wait()


def _dead_identity() -> dict:
    process = subprocess.Popen(["true"])
    process.wait()
    return {"pid": process.pid, "started_at": "exited"}


def _held(*, returned: bool, supervisor: dict | None = None) -> str:
    """A reservation of tl1 its confirm step held as uncertain with only a `reserved` fact (the pilot's act-0003),
    reserved by this test process, which stands for the conductor and is alive throughout."""
    from yggdrasil import launcher, lifecycle, state, tracker
    activation = tracker.allocate("activation")
    me = lifecycle.own_identity()
    lifecycle.commit("tl1", activation, "reserved", launcher_pid=me["pid"], launcher_started_at=me["started_at"],
                     work={})
    data = {"node": "tl1", "activation": activation, "launcher": "headless", "lifecycle": True, "phase": "starting",
            "launcher_pid": me["pid"], "launcher_started_at": me["started_at"], "pid": None,
            "process_started_at": None, "outcome": "uncertain",
            "uncertain_reason": "no process reported within the confirm window", "reserved_at": util.iso(),
            "started_at": util.iso(), "role": "tl"}
    if supervisor is not None:
        data.update(supervisor_pid=supervisor["pid"], supervisor_started_at=supervisor["started_at"])
    if returned:
        data["launch_returned_at"] = util.iso()
    launcher.save_receipt(activation, data)
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "uncertain"})
    return activation


def _released(activation: str, outcome: str = "aborted", *, unblocked: bool = True) -> None:
    from yggdrasil import conductor, lifecycle, noderepair, state
    assert noderepair.release("tl1", outcome) == "released tl1 as " + outcome + "\n"
    assert lifecycle.terminal_of(lifecycle.facts("alpha", activation)) == "released"
    assert lifecycle.revoked_path(activation).exists()  # a supervisor that never reached its gate can't start one
    assert state.runtime("tl1")["state"] == "idle"
    if unblocked:
        assert conductor.blocker(state.all_nodes()["tl1"], []) is None


def _refused(activation: str, text: str) -> None:
    from yggdrasil import lifecycle, noderepair
    with pytest.raises(util.YggError) as caught:
        noderepair.release("tl1", "aborted")
    assert caught.value.code == util.EXIT_REFUSED and text in caught.value.message
    assert lifecycle.terminal_of(lifecycle.facts("alpha", activation)) is None
    assert not lifecycle.revoked_path(activation).exists()


# (a) The launch step returned and its recorded supervisor is dead: the live conductor no longer counts.
@pytest.mark.parametrize("outcome", ["aborted", "failed"])
def test_a_returned_launch_with_a_dead_supervisor_is_released_while_the_conductor_lives(world, outcome):
    from yggdrasil import lifecycle
    activation = _held(returned=True, supervisor=_dead_identity())
    assert lifecycle.open_evidence("tl1", activation) is None
    assert lifecycle.is_uncertain("tl1", activation)  # status shows the hold while the conductor runs
    _released(activation, outcome)


# (b) The recorded supervisor is alive: it may still start the agent, so nothing is settled.
def test_a_returned_launch_with_a_live_supervisor_is_refused(world, sleeper):
    from yggdrasil import lifecycle
    activation = _held(returned=True, supervisor={"pid": sleeper.pid, "started_at": util.process_start(sleeper.pid)})
    assert lifecycle.open_evidence("tl1", activation) == "live"
    _refused(activation, "live activation")


# (c) The launch call is still in progress (no return mark) and its launcher is alive: live, as before the fix.
@pytest.mark.parametrize("recorded", [False, True])
def test_a_launch_still_in_progress_counts_as_live(world, recorded):
    from yggdrasil import lifecycle
    activation = _held(returned=False, supervisor=_dead_identity() if recorded else None)
    assert lifecycle.open_evidence("tl1", activation) == "live"
    _refused(activation, "live activation")


# (d) The supervisor's evidence can't be read (a live pid whose start time is missing): unknown, never dead.
def test_a_returned_launch_whose_supervisor_evidence_is_unreadable_is_unknown(world, sleeper):
    from yggdrasil import launcher, lifecycle
    activation = _held(returned=True, supervisor={"pid": sleeper.pid, "started_at": None})
    assert lifecycle.open_evidence("tl1", activation) == "unknown"
    assert lifecycle.evidence_unknown("tl1", activation, launcher.receipt(activation))
    _refused(activation, "process evidence is unknown")


# (e) No supervisor was ever created (Popen failed) and the launch step returned: nothing can start the agent.
def test_a_returned_launch_that_created_no_supervisor_is_released(world):
    from yggdrasil import lifecycle
    activation = _held(returned=True)
    assert lifecycle.open_evidence("tl1", activation) is None
    _released(activation)


# Reconciliation by the live conductor keeps the returned, held reservation held: never aborted and relaunched.
def test_reconciliation_keeps_a_returned_held_reservation_held(world):
    from yggdrasil import launcher, lifecycle
    activation = _held(returned=True, supervisor=_dead_identity())
    found = lifecycle.facts("alpha", activation)
    assert lifecycle.settle("tl1", activation, launcher.receipt(activation), found) == "unsettled"
    assert lifecycle.terminal_of(lifecycle.facts("alpha", activation)) is None


# The real launch path: a supervisor that exits without reporting anything leaves its identity and the return mark,
# and the user's release then succeeds with the launching process (this test, standing for the conductor) alive.
def test_a_launch_held_uncertain_by_confirm_is_released_with_its_launcher_alive(world, monkeypatch):
    from yggdrasil import launcher, lifecycle
    monkeypatch.setattr(sys, "executable", shutil.which("true"))  # a supervisor that exits at once, reporting nothing
    result = launcher.launch("tl1")
    assert result["outcome"] == "uncertain" and result["started"] is False
    activation = result["activation"]
    data = launcher.receipt(activation)
    assert isinstance(data.get("supervisor_pid"), int) and data.get("launch_returned_at")
    assert data["launcher_pid"] == lifecycle.own_identity()["pid"]
    assert lifecycle.is_uncertain("tl1", activation)
    _released(activation, unblocked=False)


# The real launch path when Popen fails: the return mark is written with no supervisor, so the release succeeds.
def test_a_launch_whose_supervisor_could_not_be_created_is_released(world, monkeypatch, tmp_path):
    from yggdrasil import launcher, state
    monkeypatch.setattr(sys, "executable", str(tmp_path / "missing-python"))
    with pytest.raises(util.YggError) as caught:
        launcher.launch("tl1")
    assert caught.value.code == util.EXIT_UNCERTAIN
    activation = state.runtime("tl1")["activation"]
    data = launcher.receipt(activation)
    assert data["outcome"] == "uncertain" and data.get("supervisor_pid") is None and data.get("launch_returned_at")
    _released(activation, unblocked=False)


# A confirm step that raises still records that the launch step returned, after the supervisor's identity.
def test_the_return_mark_is_written_when_confirm_raises(world, monkeypatch):
    from yggdrasil import launcher, state
    monkeypatch.setattr(sys, "executable", shutil.which("true"))

    def broken(*args, **kwargs):
        raise RuntimeError("confirm failed")

    monkeypatch.setattr(launcher, "confirm", broken)
    with pytest.raises(RuntimeError):
        launcher.launch("tl1")
    data = launcher.receipt(state.runtime("tl1")["activation"])
    assert isinstance(data.get("supervisor_pid"), int) and data.get("launch_returned_at")


# The user-class `ygg-uncertain` alert names the exact command that settles the hold.
def test_the_user_uncertain_alert_names_the_release_command(world):
    from yggdrasil import alertdelivery, launcher, state
    activation = _held(returned=True)
    launcher.update_receipt(activation, reserved_at=util.iso(util.now() - timedelta(seconds=400)))
    alertdelivery.escalate_uncertain()
    [alert] = [a for a in state.alerts() if a.get("kind") == "ygg-uncertain" and a.get("class") == "user"]
    assert alert["next_step"] == ("Check the agent is not running, then run `ygg node release tl1 --as aborted` "
                                  "to offer its work again (or --as failed).")


def test_the_return_mark_is_write_once(world):
    from yggdrasil import launcher
    activation = _held(returned=False)
    launcher.update_receipt(activation, launch_returned_at="2026-01-01T00:00:00Z")
    launcher.launch_returned(activation)
    assert launcher.receipt(activation)["launch_returned_at"] == "2026-01-01T00:00:00Z"
