"""The load-scaled second confirm window (v1.1 optional item O-2; A11 AC-22 and AC-77)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from yggdrasil import conductor, launcher, util


@pytest.fixture(autouse=True)
def _test_mode(monkeypatch):
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.delenv("YGG_TEST_RELEASE_OFF", raising=False)
    monkeypatch.delenv("YGG_RELEASE_FILE", raising=False)
    monkeypatch.setenv("YGG_CONFIG", "none")  # no config file of the machine running the tests
    from yggdrasil import config
    config.effective.cache_clear()
    config.file_layers.cache_clear()


@pytest.mark.parametrize("load,seconds", [(0.0, 60.0), (0.5, 60.0), (1.0, 60.0), (1.5, 90.0), (4.0, 240.0),
                                          (9.0, 300.0), (100.0, 300.0)])
def test_the_window_is_sixty_seconds_times_the_load_and_at_most_three_hundred(monkeypatch, load, seconds):
    monkeypatch.setattr(conductor, "load_sample", lambda: (load, ""))
    assert launcher.second_confirm_window() == {"confirm_window_seconds": seconds}


def test_no_load_sample_gives_the_minimum_window(monkeypatch):
    monkeypatch.setattr(conductor, "load_sample", lambda: (None, "missing load sample"))
    assert launcher.second_confirm_window() == {"confirm_window_seconds": 60.0}


def test_switched_off_records_no_window(monkeypatch):
    monkeypatch.setenv("YGG_TEST_RELEASE_OFF", "confirm_window")
    monkeypatch.setattr(conductor, "load_sample", lambda: (3.0, ""))
    assert launcher.second_confirm_window() == {}


@pytest.mark.parametrize("age,over", [(89, False), (90, True), (91, True)])
def test_the_conductor_judges_the_recorded_window_against_now(monkeypatch, age, over):
    monkeypatch.setenv("YGG_NOW", "2026-11-22T10:00:00Z")
    reserved = util.iso(util.parse_time("2026-11-22T10:00:00Z") - timedelta(seconds=age))
    data = {"outcome": "pending", "reserved_at": reserved, "confirm_window_seconds": 90.0, "pid": None}
    assert conductor._second_window_over(data) is over


def test_a_start_with_a_process_or_an_exit_or_no_window_is_never_held(monkeypatch):
    monkeypatch.setenv("YGG_NOW", "2026-11-22T10:10:00Z")
    base = {"outcome": "pending", "reserved_at": "2026-11-22T10:00:00Z", "confirm_window_seconds": 60.0, "pid": None}
    assert conductor._second_window_over(base) is True
    assert conductor._second_window_over(dict(base, pid=123)) is False
    assert conductor._second_window_over(dict(base, exited_at="2026-11-22T10:01:00Z")) is False
    assert conductor._second_window_over(dict(base, outcome="uncertain")) is False
    assert conductor._second_window_over({k: v for k, v in base.items() if k != "confirm_window_seconds"}) is False


def test_a_pending_start_is_not_a_failure_in_the_tick_output(monkeypatch):
    monkeypatch.setattr(conductor, "admit", lambda: (True, "ok"))
    monkeypatch.setattr(conductor, "concurrency_admit", lambda: (True, "ok"))
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: {"node": node, "outcome": "pending", "started": False})
    output = conductor.launch_queue([{"node": "pm1"}])
    assert output == {"launched": ["pm1"], "held": []}


def test_a_start_that_created_no_process_is_still_listed_as_failed(monkeypatch):
    monkeypatch.setattr(conductor, "admit", lambda: (True, "ok"))
    monkeypatch.setattr(conductor, "concurrency_admit", lambda: (True, "ok"))
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: {"node": node, "outcome": "failed", "started": False})
    assert conductor.launch_queue([{"node": "pm1"}])["failed"] == ["pm1"]


@pytest.mark.parametrize("outcome,code", [("confirmed", 0), ("failed", 1), ("pending", util.EXIT_UNCERTAIN),
                                          ("uncertain", util.EXIT_UNCERTAIN), ("capacity", util.EXIT_ADMISSION)])
def test_ygg_launch_exit_code_follows_the_outcome(monkeypatch, capsys, outcome, code):
    from yggdrasil import cli, interlock
    monkeypatch.setattr(interlock, "reconcile", lambda: None)
    monkeypatch.setattr(launcher, "launch", lambda node, **kw: {"node": node, "outcome": outcome, "started": False})
    assert cli.dispatch(cli.build().parse_args(["launch", "pm1"]))[0] == code
