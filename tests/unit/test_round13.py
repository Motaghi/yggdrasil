"""Unit tests for the round-13 review findings: reserved wakes, ids per enqueue, encoded reasons."""

import itertools
import shutil
from collections import Counter

import pytest

from test_round2 import clear_caches, world  # noqa: F401  (fixture and helpers)
from test_round12 import CAPS, launches


# Property: for every load, cap and recovery pattern (none, once, after every tick), each queued wake,
# including same-reason wakes, launches its node exactly once per outstanding enqueue.
@pytest.mark.parametrize("load,cap,recovery", list(itertools.product(["low", "high-then-low"], list(CAPS),
                                                                       ["none", "once", "every-tick"])))
def test_every_queued_wake_launches_exactly_once_under_repeated_recovery(world, load, cap, recovery):
    from yggdrasil import conductor, ratatoskr, state
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nload_max_age_seconds: 999999999\n" + CAPS[cap])
    clear_caches()
    state.wake("tl1", "stage-approved")
    state.wake("tl1", "stage-approved")  # a second outstanding wake with the same reason, queued before launch
    ratatoskr.send("pm1", "su1", "note", "a pending message")
    (world / "load").write_text("0.95" if load == "high-then-low" else "0.1")
    conductor.tick()
    if load == "high-then-low":
        (world / "load").write_text("0.1")
        conductor.tick()
    for number in range(8):
        if recovery == "every-tick" or (recovery == "once" and number == 0):
            shutil.rmtree(world / "state")
        conductor.tick()
    assert launches(world) == Counter({"pm1": 1, "tl1": 1, "su1": 1})
    assert state.wakes() == {}


# Boat events: any job and status the command accepts is journalled encoded and recovered intact.
@pytest.mark.parametrize("job,status", [("job17", "done"), ("job/17", "done"), ("job17", "failed (exit 2)")])
def test_boat_completion_wake_is_recoverable(world, job, status):
    from yggdrasil import commands, rehydrate, state
    commands.event_boat("tl1", job, status)
    shutil.rmtree(world / "state")
    assert rehydrate.replay_journal() == 0  # nothing skipped as invalid
    assert "boat:" + job + ":" + status in state.wakes()["tl1"]["reasons"]
