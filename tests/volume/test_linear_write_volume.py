"""The Linear write-volume test: requests per activation and per idle tick, measured against the suite's
stateful Linear fake, and projected against the app's request window.

Scenario (port plan, "The Linear write-volume test"): the core tree and `ready()`, quiesced; then 20 launches
of `pm1tl1`, each waited until its supervisor has written the exit; then 20 idle conductor ticks. Every
GraphQL request the fake logged is counted by operation. The report is written to `YGG_VOLUME_REPORT` when
set, and printed.

Stop rule (ruling 114): the port stops for the founder if idle polling exceeds 25 percent of the request
window, or a busy hour (5 launches per cycle) would exceed 60 percent.
"""

import collections
import json
import os
import time

import pytest

pytest.importorskip("graphql")  # the suite's Linear fake validates every request against the schema

from driver import Driver

LAUNCHES = 20
IDLE_TICKS = 20
WINDOW_REQUESTS = 10_000      # x-ratelimit-requests-limit (A4.3)
WINDOW_SECONDS = 3600         # the M1 resets lie one hour after the recorded probe run (see the report notes)
TICK_SECONDS = 30             # ruling 109: the fallback tick under Linear
SETTLE_SECONDS = 30           # settle_delay_seconds default: Heimdall waits between launches of one tick
LAUNCHES_PER_CYCLE = 5        # max_launches_per_tick default (A4.2), the ruling-114 busy hour


def requests(driver):
    return [r for r in driver.a5_linear_requests() if r["path"] == "/graphql"]


def kinds(records):
    counted = collections.Counter()
    for record in records:
        body = record.get("body") or {}
        mutation = str(body.get("query", "")).lstrip().startswith("mutation")
        counted[("mutation " if mutation else "query ") + str(body.get("operationName"))] += 1
    return counted


def settle(driver, node, timeout=60):
    """Until every activation of the node has its terminal fact: the supervisor writes the rehydration
    document before the `exited` fact, so nothing of the activation is still being written."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        receipts = [driver.json_file(p) for p in (driver.state / "activations").glob("*.json")]
        mine = [r for r in receipts if r and r.get("node") == node]
        if mine and all(r.get("terminal") for r in mine):
            return
        time.sleep(0.05)
    raise AssertionError("activation of " + node + " did not settle")


@pytest.mark.skipif(os.environ.get("ACCEPTANCE_TARGET", "real") != "real", reason="real target only")
def test_linear_request_volume(tmp_path):
    with Driver(tmp_path) as driver:
        with driver.a5_linear():
            driver.core_tree()
            driver.ready()
            driver.quiesce()
            setup = requests(driver)
            per_launch = []
            launch_kinds = collections.Counter()
            for _ in range(LAUNCHES):
                before = len(requests(driver))
                result = driver.run("launch", "pm1tl1")
                assert result.returncode == 0, result.stderr
                settle(driver, "pm1tl1")
                made = requests(driver)[before:]
                per_launch.append(len(made))
                launch_kinds.update(kinds(made))
            assert len(driver.launches("pm1tl1")) == LAUNCHES
            before = len(requests(driver))
            for _ in range(IDLE_TICKS):
                assert driver.run("conductor", "tick").returncode == 0
            idle = requests(driver)[before:]
            assert len(driver.launches("pm1tl1")) == LAUNCHES  # the idle ticks launched nothing
            assert driver.a5_linear_live_schema_errors() == []

    steady = per_launch[1:]
    per_activation = sum(steady) / len(steady)
    mutations = sum(v for k, v in launch_kinds.items() if k.startswith("mutation")) / LAUNCHES
    per_idle_tick = len(idle) / IDLE_TICKS
    ticks_per_hour = WINDOW_SECONDS / TICK_SECONDS
    idle_share = per_idle_tick * ticks_per_hour / WINDOW_REQUESTS
    # A busy cycle starts 5 sessions with a settle delay before each one after the first, then waits a tick.
    cycle_seconds = (LAUNCHES_PER_CYCLE - 1) * SETTLE_SECONDS + TICK_SECONDS
    cycles = WINDOW_SECONDS / cycle_seconds
    busy = cycles * (LAUNCHES_PER_CYCLE * per_activation + per_idle_tick)
    literal = ticks_per_hour * (LAUNCHES_PER_CYCLE * per_activation + per_idle_tick)
    report = {
        "setup_requests": len(setup), "setup_by_operation": dict(kinds(setup)),
        "per_launch": per_launch, "per_activation_steady": per_activation,
        "mutations_per_activation": mutations,
        "per_activation_by_operation": {k: v / LAUNCHES for k, v in sorted(launch_kinds.items())},
        "per_idle_tick": per_idle_tick, "idle_by_operation": dict(kinds(idle)),
        "window": {"requests": WINDOW_REQUESTS, "seconds": WINDOW_SECONDS},
        "idle_hour": {"ticks": ticks_per_hour, "requests": per_idle_tick * ticks_per_hour, "share": idle_share},
        "busy_hour": {"cycle_seconds": cycle_seconds, "cycles": cycles, "requests": busy,
                      "share": busy / WINDOW_REQUESTS},
        "busy_hour_without_settle_delay": {"requests": literal, "share": literal / WINDOW_REQUESTS},
    }
    text = json.dumps(report, indent=2)
    print(text)
    if os.environ.get("YGG_VOLUME_REPORT"):
        with open(os.environ["YGG_VOLUME_REPORT"], "w") as handle:
            handle.write(text + "\n")
    assert idle_share <= 0.25, "ruling 114: idle polling exceeds 25 percent of the request window"
    assert busy / WINDOW_REQUESTS <= 0.60, "ruling 114: a busy hour exceeds 60 percent of the request window"
