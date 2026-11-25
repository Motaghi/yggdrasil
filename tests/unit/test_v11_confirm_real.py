"""Confirm and the hold decisions over real receipt files in a temporary state root: the real hold_uncertain,
mark_uncertain and finalize, with only the process table scripted where an ordering needs it."""
from __future__ import annotations

import os

from yggdrasil import launcher
from test_round2 import world  # noqa: F401  (shared fixture: a real state root)
from test_v11_failed_start import _seed_reserved


class _Supervisor:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


def _pid_answers(monkeypatch, answers):
    answers = list(answers)
    monkeypatch.setattr(launcher.util, "pid_alive", lambda pid: answers.pop(0) if len(answers) > 1 else answers[0])


def test_a_death_seen_by_confirm_is_kept_by_the_real_hold_path(world, monkeypatch):
    """Already confirmed; confirm sees the agent gone, then a later probe finds a live process with the matching
    identity. Only an exit record counts after a death: held, with no exit record on file."""
    identifier = _seed_reserved()
    launcher.update_receipt(identifier, outcome="confirmed", phase="launched", pid=4242,
                            process_started_at="s:1000.000000")
    _pid_answers(monkeypatch, [False, True])
    monkeypatch.setattr(launcher.util, "same_start", lambda pid, started: True)
    result = launcher.confirm("tl1", identifier, _Supervisor(code=1))
    record = launcher.receipt(identifier)
    assert result["outcome"] == "uncertain" and record["outcome"] == "uncertain"
    assert record.get("exited_at") is None
    assert record["uncertain_reason"] == "the process is gone and no exit was recorded"


def test_with_the_window_on_a_live_pid_without_identity_is_confirmed_as_in_v1_0(world):
    """The v1.0 decision with the window on (A11 AC-22): not left pending, which would wait on a second window the
    conductor never judges once a pid is recorded."""
    identifier = _seed_reserved()
    launcher.update_receipt(identifier, confirm_window_seconds=90.0, pid=os.getpid(), process_started_at=None)
    result = launcher.confirm("tl1", identifier, _Supervisor())
    record = launcher.receipt(identifier)
    assert result["outcome"] == "confirmed" and record["outcome"] == "confirmed"
    assert not record.get("confirm_left_pending_at")


def test_finalize_holds_a_malformed_exit_record_instead_of_failing_and_retrying_it(world):
    identifier = _seed_reserved()
    launcher.update_receipt(identifier, pid=4242, exited_at="bad", exit_code=False)
    outcome = launcher.finalize("tl1", identifier, "failed", resolving=("pending", "uncertain"))
    record = launcher.receipt(identifier)
    assert outcome == "uncertain" and record["outcome"] == "uncertain"
    assert record["uncertain_reason"] == "the exit record can't be read"


def test_finalize_still_classifies_a_valid_exit_record(world):
    identifier = _seed_reserved()
    launcher.update_receipt(identifier, pid=4242, exited_at="2026-11-23T05:00:00Z", exit_code=1)
    assert launcher.finalize("tl1", identifier, "failed", resolving=("pending", "uncertain")) == "failed"
    assert launcher.receipt(identifier)["outcome"] == "confirmed"


def test_a_live_pid_whose_identity_cannot_be_read_is_held_with_its_own_reason(world):
    from yggdrasil import state
    identifier = _seed_reserved()
    launcher.update_receipt(identifier, pid=os.getpid(), process_started_at=None)
    result = launcher.confirm("tl1", identifier, _Supervisor())
    record = launcher.receipt(identifier)
    assert result["outcome"] == "uncertain"
    assert record["uncertain_reason"] == launcher.UNIDENTIFIED
    [alert] = [a for a in state.alerts() if a.get("kind") == "ygg-uncertain" and a.get("activation") == identifier]
    assert launcher.UNIDENTIFIED in alert["message"] and "release" not in alert["message"]
    assert "no process reported" not in alert["message"]
