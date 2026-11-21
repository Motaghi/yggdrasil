import pytest

from checks_a8 import activation, node


@pytest.mark.ac("AC-05")
def test_reused_pid_is_held_and_unrelated_sleep_survives(a8):
    with a8.fresh("pid-reuse") as driver:
        with driver.unrelated_sleep() as pid:
            assert type(pid) is int, "U needs an OS-assigned pid"
            driver.seed_activation(
                "pm1tl1", "--phase", "spawned", "--process", "live", "--pid", str(pid),
                "--process-start", "Thu Jan  1 00:00:00 1970",
            )
            identifier = activation(driver.latest())
            driver.tick()
            assert identifier in driver.status()["uncertain_keys"]
            record = driver.receipt(identifier)
            assert record["pid"] == pid
            assert record["process_started_at"] == "Thu Jan  1 00:00:00 1970"
            # The specified stop signals, SIGINT and SIGKILL, terminate sleep.
            assert driver.process_alive(pid)
            assert driver.starts() == 0


@pytest.mark.ac("AC-06")
def test_unknown_probe_holds_then_live_evidence_adopts(a8):
    with a8.fresh("unknown-probe") as driver:
        driver.seed_activation("pm1tl1", "--phase", "spawned", "--process", "live")
        identifier = activation(driver.latest())
        override = {"YGG_TEST_PROBE": "unknown"}
        driver.tick(env=override)
        driver.tick(env=override)
        status = driver.status(env=override)
        assert driver.starts() == 0
        assert identifier in status["uncertain_keys"]
        driver.tick()
        status = driver.status()
        assert node(status)["state"] == "running"
        assert status["uncertain_keys"] == []


@pytest.mark.ac("AC-08")
def test_capacity_deadline_expires_without_another_journal_event(a8):
    with a8.fresh("capacity-deadline") as driver:
        driver.agents(pm1tl1="capacity")
        driver.message()
        driver.at("2026-11-16T10:00:00+01:00")
        driver.tick()
        record = driver.wait_exited(timeout=30)
        assert isinstance(record, dict) and record.get("exited_at")
        driver.agents(pm1tl1="exit0")
        driver.at("2026-11-16T10:04:00+01:00")
        driver.tick()
        assert driver.starts() == 1
        driver.at("2026-11-16T10:06:00+01:00")
        driver.tick()
        assert driver.starts() == 2


@pytest.mark.ac("AC-08")
def test_checkpoint_batch_deadline_wakes_pm_without_all_lanes(a8):
    with a8.fresh("checkpoint-deadline") as driver:
        driver.agents(pm1="exit0", pm1tl1="persist", pm1tl2="persist")
        driver.package("WP-41", "pm1tl1", criteria=True, authorise=True)
        driver.package("WP-7", "pm1tl2", criteria=True, authorise=True)
        driver.run("launch", "pm1tl1")
        assert driver.wait_starts("pm1tl1", 1) == 1
        driver.run("launch", "pm1tl2")
        assert driver.wait_starts("pm1tl2", 1) == 1
        driver.quiesce()
        driver.clear_calls()
        driver.at("2026-11-16T10:00:00+01:00")
        driver.run("tracker", "write", "--as", "pm1tl1", "--project", "alpha-p1",
                   "--package", "WP-41", "--kind", "checkpoint", "--body", "first")
        driver.at("2026-11-16T13:59:00+01:00")
        driver.tick()
        assert driver.starts("pm1") == 0
        driver.at("2026-11-16T14:01:00+01:00")
        driver.tick()
        assert driver.starts("pm1") == 1
