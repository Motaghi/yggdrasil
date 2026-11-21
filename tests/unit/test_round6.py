"""Round-6 review fixes: retry replay wake order, exit-record-only failures, layered timebox, capacity."""

import json
import subprocess

import pytest

from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)
from yggdrasil import util


def _seed_receipt(activation, **fields):
    from yggdrasil import launcher
    data = {"node": "tl1", "activation": activation, "launcher": "headless", "outcome": "pending",
            "started_at": util.iso(), "retries_used": 0}
    data.update(fields)
    launcher.save_receipt(activation, data)


# dev2 #2: a dead process without the supervisor's exit record is never classified as failed.
def test_finalize_refuses_failed_without_an_exit_record(world):
    from yggdrasil import launcher, state, tracker
    activation = tracker.allocate("activation")
    done = subprocess.Popen(["true"])
    done.wait()
    _seed_receipt(activation, pid=done.pid, process_started_at="gone")
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "pending"})
    assert launcher.finalize("tl1", activation, "failed") != "failed"
    # Round 7: never left pending either; held as uncertain (alerted once).
    assert launcher.receipt(activation)["outcome"] == "uncertain"


def test_confirm_marks_a_dead_process_without_exit_record_uncertain(world):
    from yggdrasil import launcher, state, tracker
    activation = tracker.allocate("activation")
    done = subprocess.Popen(["true"])
    done.wait()
    _seed_receipt(activation, pid=done.pid, process_started_at="gone")
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "pending"})
    result = launcher.confirm("tl1", activation, supervisor=None)
    assert result["outcome"] == "uncertain"


# dev2 #3: the effective, inherited timebox duration satisfies PID approval.
def test_programme_timebox_hours_satisfy_a_pid_timebox_cadence(world):
    from yggdrasil import commands, tracker
    import yaml
    programme = tracker.programme("alpha")
    programme["config"] = {"timebox_hours": 8}
    tracker.save_programme(programme)
    commands.project_create("p2", "alpha", "pm2")
    header = {"schema": "ygg.pid.v1", "project": "p2", "programme": "alpha", "repos": [],
              "stages": [{"number": 1, "planned_end": "2026-11-17T17:00:00Z"}], "tolerances": {"time": "P1D"},
              "cadence": "timebox", "delivery_approach": "agile",
              "business_case": {"why": "w", "benefit": "b", "cost": {}, "risks": [], "stop_if": "s"}}
    commands.tracker_write("pm2", "p2", "pid", "---\n" + yaml.safe_dump(header) + "---\n# PID\n")
    assert "approved" in commands.pid_approve("p2")


def test_explicit_invalid_pid_timebox_hours_is_still_refused():
    from yggdrasil.commands import validate_pid
    header = {"schema": "ygg.pid.v1", "project": "p", "programme": "a", "repos": [],
              "stages": [{"number": 1, "planned_end": "2026-11-17T17:00:00Z"}], "tolerances": {"time": "P1D"},
              "timebox_hours": 0, "business_case": "skipped", "business_case_skip_reason": "r"}
    with pytest.raises(util.YggError) as error:
        validate_pid(header)
    assert "timebox_hours" in error.value.message


# QA #1: a quota exit recorded after confirmation is a capacity hold, not a stall.
def test_quota_exit_after_confirm_becomes_capacity_hold(world, monkeypatch):
    from yggdrasil import conductor, state, tracker
    activation = tracker.allocate("activation")
    _seed_receipt(activation, outcome="confirmed", pid=999999, exit_code=1, exited_at=util.iso(),
                  stderr_tail="quota exceeded")
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "confirmed"})
    monkeypatch.setenv("YGG_NOW", "2026-11-16T08:00:00Z")
    flagged, relaunch = conductor.heimdall_scan()
    assert flagged == [] and relaunch == []
    assert state.runtime("tl1")["state"] == "idle"  # held-capacity is derived from the exit fact
    assert not any(a["kind"] == "ygg-stall" for a in state.alerts())


# QA #2: capacity backoff lies in the future and scheduling honours it.
def test_capacity_backoff_is_in_the_future_and_honoured(world, monkeypatch):
    from yggdrasil import conductor, launcher, state, tracker
    from test_rebuild_t4 import SPAWNED, given
    activation = tracker.allocate("activation")
    record = {"exit_code": 1, "exited_at": util.iso(), "stderr_tail": "HTTP 429"}
    fields = launcher.exit_fields(record)
    given("tl1", activation, SPAWNED, ("exited", dict(fields, exit_code=1)))
    _seed_receipt(activation, outcome="confirmed", **record)
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "confirmed"})
    with util.locked():
        launcher.apply_outcome("tl1", activation, "capacity")
    until = util.parse_time(fields["backoff_until"])
    assert until > util.now()
    assert "tl1" not in conductor.candidates()
    monkeypatch.setenv("YGG_NOW", util.iso(until))
    clear_caches()
    assert "tl1" in conductor.candidates()
