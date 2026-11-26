"""pid-ready after an exception plan rejected under version 1.0.0: its follow-up decision has no `exception` key,
so the guard recognises it by the exact shape 1.0.0 wrote, and never a decision raised with `ygg decision raise`."""
from __future__ import annotations

import argparse
import re

import pytest

from yggdrasil import util
from yggdrasil.util import YggError
from test_round2 import clear_caches, world  # noqa: F401  (fixture and helper)

PID_READY_REASON = "pid-ready is only for a new, rejected or replanned PID"


def _state(project="p1"):
    from yggdrasil import tracker
    return tracker.project(project)["state"]


def _write_ready(project="p1", node="pm1"):
    from yggdrasil import commands
    return commands.tracker_write(node, project, "pid-ready", "ready")


def _pid_ready_alerts():
    from yggdrasil import state
    return sum(1 for entry in state.alerts() if entry.get("kind") == "ygg-pid-ready")


def _question(exception):
    return "Exception " + exception + " plan rejected: what next?"


def _reject_new_exception(reason="the scope grew", plan="reduce it"):
    from yggdrasil import commands
    identifier = re.search(r"E-[0-9]+", commands.exception_raise("pm1", reason, plan))[0]
    commands.exception_decide(identifier, "reject")
    return identifier


def _linked_follow_up(exception):
    from yggdrasil import tracker
    return next(i for i in tracker.issues("p1") if i.get("kind") == "decision" and i.get("exception") == exception)


def _reject_as_v100(reason="the scope grew", plan="reduce it"):
    """Reject an exception and leave its follow-up exactly as version 1.0.0 wrote it: no `exception` key."""
    exception = _reject_new_exception(reason, plan)
    follow_up = _linked_follow_up(exception)
    del follow_up.header["exception"]
    follow_up.save()
    return exception, follow_up.id


def _raise_lookalike(exception, aligns="no"):
    from yggdrasil import decisions
    args = argparse.Namespace(sender="pm1", question=_question(exception), option=["replan", "close"],
                              recommended="replan", reversible="no", undo=None, aligns=aligns, scope=None,
                              action=None, target=None)
    return decisions.raise_decision(args).split()[0]


def _refused():
    before = _pid_ready_alerts()
    with pytest.raises(YggError) as caught:
        _write_ready()
    assert caught.value.code == util.EXIT_REFUSED and PID_READY_REASON in caught.value.message
    assert _state() == "exception" and _pid_ready_alerts() == before


def test_a_legacy_follow_up_answered_replan_by_the_user_allows_pid_ready(world):
    from yggdrasil import decisions
    _, follow_up = _reject_as_v100()
    decisions.decide(follow_up, "replan", None)
    before = _pid_ready_alerts()
    _write_ready()
    assert _state() == "pid-ready" and _pid_ready_alerts() == before + 1


def test_a_legacy_follow_up_still_open_keeps_pid_ready_refused(world):
    _reject_as_v100()
    _refused()


def test_a_legacy_follow_up_answered_close_keeps_pid_ready_refused(world):
    from yggdrasil import decisions
    _, follow_up = _reject_as_v100()
    decisions.decide(follow_up, "close", None)
    _refused()


def test_a_legacy_follow_up_answered_by_an_envelope_keeps_pid_ready_refused(world):
    from yggdrasil import tracker
    _, follow_up = _reject_as_v100()
    issue = tracker.read_issue("p1", follow_up)
    issue.header.update(chosen="replan", chosen_by="envelope")
    issue.save()
    _refused()


def test_a_lookalike_raised_with_decision_raise_carries_the_marker_and_is_refused(world):
    from yggdrasil import decisions, tracker
    exception, _ = _reject_as_v100()
    lookalike = _raise_lookalike(exception)
    assert tracker.read_issue("p1", lookalike).get("raised_with") == "decision-raise"
    decisions.decide(lookalike, "replan", None)
    _refused()


def test_a_lookalike_without_the_marker_still_differs_by_its_aligns_answer(world):
    # A decision raised with version 1.0.0's `decision raise` has no marker, but always has aligns yes or no.
    from yggdrasil import decisions, tracker
    exception, _ = _reject_as_v100()
    lookalike = _raise_lookalike(exception)
    issue = tracker.read_issue("p1", lookalike)
    del issue.header["raised_with"]
    issue.save()
    decisions.decide(lookalike, "replan", None)
    _refused()


def test_a_legacy_shape_from_another_origin_is_refused(world):
    from yggdrasil import decisions, tracker
    _, follow_up = _reject_as_v100()
    issue = tracker.read_issue("p1", follow_up)
    issue.header["provenance"] = {"type": "agent", "origin": "tl1"}
    issue.save()
    decisions.decide(follow_up, "replan", None)
    _refused()


def test_a_legacy_follow_up_of_an_older_exception_does_not_cover_a_newer_one(world):
    from yggdrasil import decisions
    _, follow_up = _reject_as_v100()
    decisions.decide(follow_up, "replan", None)
    _reject_as_v100("it grew again", "reduce it more")
    _refused()


def test_the_linked_follow_up_path_is_unchanged(world):
    from yggdrasil import decisions
    exception = _reject_new_exception()
    _refused()
    decisions.decide(_linked_follow_up(exception).id, "replan", None)
    before = _pid_ready_alerts()
    _write_ready()
    assert _state() == "pid-ready" and _pid_ready_alerts() == before + 1


def test_a_linked_follow_up_wins_over_a_legacy_shaped_decision(world):
    # While the linked follow-up is open, a legacy-shaped decision answered replan does not count.
    from yggdrasil import decisions, tracker
    exception = _reject_new_exception()
    linked = _linked_follow_up(exception)
    identifier = tracker.allocate("decision")
    copy = tracker.new_issue("p1", identifier, "decision", ["decision"], "pm1",
                             **{k: v for k, v in linked.header.items()
                                if k not in ("id", "kind", "labels", "standard_version", "actor", "node",
                                             "created_at", "exception")})
    copy.save()
    decisions.decide(identifier, "replan", None)
    _refused()
