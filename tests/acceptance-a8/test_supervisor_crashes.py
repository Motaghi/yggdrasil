import pytest

from checks_a8 import activation, node


@pytest.mark.ac("AC-02")
def test_unrecorded_live_agent_is_adopted_and_stopped(a8):
    with a8.fresh("after-spawn") as driver:
        driver.agents(pm1tl1="persist")
        driver.message()
        fault = "pm1tl1:boundary:after-spawn"
        driver.supervisor_tick(fault=fault)
        trace = driver.wait_supervisor()
        assert isinstance(trace, dict)
        assert trace["lines"][-1] == "fault " + fault
        assert driver.starts() == 1
        driver.tick()
        driver.tick()
        record = driver.latest()
        identifier = activation(record)
        status_node = node(driver.status())
        assert status_node["state"] == "running"
        assert status_node["activation"]["id"] == identifier == trace["activation"]
        assert type(record["pid"]) is int
        assert driver.process_alive(record["pid"])
        assert driver.starts() == 1
        result = driver.run("stop", "alpha")
        assert result.returncode == 0
        assert driver.wait_dead(record["pid"], timeout=15)
        assert node(driver.status())["state"] == "stopped"


@pytest.mark.ac("AC-02")
def test_unrecorded_exit_is_held_until_user_release(a8):
    with a8.fresh("after-exit") as driver:
        driver.agents(pm1tl1="exit0")
        driver.message()
        fault = "pm1tl1:boundary:after-exit"
        driver.supervisor_tick(fault=fault)
        trace = driver.wait_supervisor()
        assert isinstance(trace, dict)
        assert trace["lines"][-1] == "fault " + fault
        driver.tick()
        driver.tick()
        identifier = activation(driver.latest())
        assert identifier == trace["activation"]
        assert identifier in driver.status()["uncertain_keys"]
        assert driver.starts() == 1
        result = driver.run("node", "release", "pm1tl1", "--as", "failed")
        assert result.returncode == 0
        status = driver.status()
        assert node(status)["state"] == "idle"
        assert status["uncertain_keys"] == []


@pytest.mark.ac("AC-12")
def test_every_launch_write_fault_has_idempotent_rehydrate(a8):
    # AC-12 refers to spec-a7.md AC-08's launching-tick sweep.
    with a8.fresh("launch-discovery") as driver:
        driver.agents(pm1tl1="persist")
        driver.message("hello")
        _, text = driver.trace_step("conductor", "tick")
        writes = [line for line in text.splitlines() if line.startswith("write ")]
        assert writes, "The launching tick must expose its durable writes"

    failures = []
    for k in range(1, len(writes) + 1):
        with a8.fresh("launch-write-" + str(k)) as driver:
            try:
                driver.agents(pm1tl1="persist")
                driver.message("hello")
                result = driver.fault_step("write:" + str(k), "conductor", "tick")
                assert result.returncode == 97
                before_starts = driver.starts()
                driver.run("rehydrate")
                first = driver.journal_bytes()
                first_starts = driver.starts()
                driver.run("rehydrate")
                second = driver.journal_bytes()
                second_starts = driver.starts()
                assert "_ygg/journal.md" in first
                assert "initiatives/alpha/documents/journal.md" in first
                assert first == second
                assert second_starts == first_starts
                assert first_starts == before_starts
            except AssertionError as error:
                failures.append("write:" + str(k) + ": " + str(error))
    assert not failures, "\n".join(failures)
