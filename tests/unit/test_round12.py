"""Unit tests for the round-12 review findings: one durable record per queued wake."""

import itertools
import shutil
from collections import Counter

import pytest

from test_round2 import clear_caches, world  # noqa: F401  (fixture and helpers)

CAPS = {"count": "tick_cap: count\nmax_launches_per_tick: 1\n",
        "time": "tick_cap: time\ntick_budget_seconds: 0.001\nsettle_delay_seconds: 0\n",
        "both": "tick_cap: both\nmax_launches_per_tick: 1\ntick_budget_seconds: 240\nsettle_delay_seconds: 0\n"}


def launches(world):
    log = world / "agent.log"
    return Counter(line.split()[0] for line in log.read_text().splitlines() if line.strip()) if log.exists() else Counter()


# Property: for every combination of load, cap and wipe, every queued wake launches exactly once.
@pytest.mark.parametrize("load,cap,wipe", list(itertools.product(["low", "high-then-low"], list(CAPS),
                                                                   [False, True])))
def test_every_queued_wake_launches_exactly_once(world, load, cap, wipe):
    from yggdrasil import conductor, state
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nload_max_age_seconds: 999999999\n" + CAPS[cap])
    clear_caches()
    for node in ("tl1", "su1"):
        state.wake(node, "stage-approved")  # wakes with no inbox message behind them
    (world / "load").write_text("0.95" if load == "high-then-low" else "0.1")
    conductor.tick()
    if load == "high-then-low":
        assert launches(world) == Counter()
        (world / "load").write_text("0.1")
        conductor.tick()
    if wipe:
        shutil.rmtree(world / "state")  # local state lost; the tracker (and its journal) survives
    for _ in range(8):
        conductor.tick()
    assert launches(world) == Counter({"pm1": 1, "tl1": 1, "su1": 1})
    assert state.wakes() == {}


