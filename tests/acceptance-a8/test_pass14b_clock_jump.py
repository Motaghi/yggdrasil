import pytest

from checks_a8 import activation


@pytest.mark.ac("AC-07")
def test_clock_jump_keeps_dead_spawned_activation_uncertain_and_not_aborted(a8):
    with a8.fresh("dead-spawned-clock-jump") as driver:
        driver.seed_activation("pm1tl1", "--phase", "spawned", "--process", "dead")
        identifier = activation(driver.latest())
        for timestamp in ("2026-11-16T10:00:00+01:00", "2026-11-16T12:00:00+01:00"):
            driver.at(timestamp)
            driver.tick()
            assert identifier in driver.status()["uncertain_keys"]
            record = driver.receipt(identifier)
            assert isinstance(record, dict), "Expected the seeded activation record"
            assert record.get("terminal") != "aborted"
            assert driver.starts() == 0
