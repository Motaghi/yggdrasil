"""A settled T3 activation's thread is reused only while T3 reports its run ended (ruling 106)."""

import json
import os

import pytest

from driver import Driver

pytestmark = pytest.mark.skipif(os.environ.get("ACCEPTANCE_TARGET", "real") != "real", reason="real target only")


def _settled_seed(driver):
    driver.core_tree()
    driver.m3_t3_seed()
    seed = next(n for n in driver.status()["nodes"] if n["id"] == "pm1tl1")["activation"]["id"]
    assert driver.run("conductor", "tick").returncode == 0
    record = json.loads((driver.state / "activations" / (seed + ".json")).read_text())
    assert record.get("exited_at") and record.get("exit_code") == 0
    return seed


@pytest.mark.parametrize("status,active", [("running", True), ("interrupted", False), ("failed", True)])
def test_a_thread_that_is_active_again_or_unknown_gets_no_send(tmp_path, status, active):
    with Driver(tmp_path) as driver:
        _settled_seed(driver)
        driver.m3_t3_thread(status=status, active=active)
        assert driver.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work",
                          "--body", "next work").returncode == 0
        driver.clear_calls()
        assert driver.run("conductor", "tick").returncode == 0
        assert [c for c in driver.a5_t3_calls("thread send") if "th-4" in c["argv"]] == []
        assert driver.a5_t3_calls("thread start") == []


def test_a_thread_whose_run_ended_is_reused(tmp_path):
    with Driver(tmp_path) as driver:
        _settled_seed(driver)
        assert driver.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work",
                          "--body", "next work").returncode == 0
        driver.clear_calls()
        assert driver.run("conductor", "tick").returncode == 0
        assert len([c for c in driver.a5_t3_calls("thread send") if "th-4" in c["argv"]]) == 1


def test_the_hold_is_visible_in_status_and_clears_when_the_run_ends(tmp_path):
    with Driver(tmp_path) as driver:
        _settled_seed(driver)
        driver.m3_t3_thread(status="running", active=True)
        assert driver.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work",
                          "--body", "next work").returncode == 0
        driver.run("conductor", "tick")
        view = next(n for n in driver.status()["nodes"] if n["id"] == "pm1tl1")
        assert view["state"] == "queued" and "th-4" in str(view["held_reason"])
        driver.m3_t3_thread(status="completed", active=False)
        view = next(n for n in driver.status()["nodes"] if n["id"] == "pm1tl1")
        assert not view["held_reason"]
