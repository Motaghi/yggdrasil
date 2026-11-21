import pytest


def check_h(observations):
    for observation in observations:
        assert isinstance(observation, dict), "Expected recovery observations"
        assert len(observation["live"]) <= 1
    first, second, third = observations[1:]
    identifiers = {record["activation"] for record in first["records"]}
    if identifiers.intersection(first["status"]["uncertain_keys"]):
        assert second["starts"] == first["starts"]
    assert {key: value for key, value in second["status"].items()
            if key != "generated_at"} == {
                key: value for key, value in third["status"].items()
                if key != "generated_at"}


@pytest.mark.ac("AC-01")
def test_every_supervisor_write_fault_preserves_check_h_and_one_safe_outcome(a8):
    with a8.fresh("supervisor-write-discovery") as driver:
        driver.agents(pm1tl1="exit0")
        driver.message()
        driver.supervisor_tick()
        trace = driver.wait_supervisor(timeout=30)
        assert isinstance(trace, dict), "Expected a supervisor trace"
        writes = [line for line in trace["lines"] if line.startswith("write ")]
        assert len(writes) >= 3

    failures = []
    for k in range(1, len(writes) + 1):
        with a8.fresh("supervisor-write-" + str(k)) as driver:
            fault = "pm1tl1:write:" + str(k)
            try:
                driver.agents(pm1tl1="exit0")
                driver.message()
                directory = "sup-" + str(k)
                driver.supervisor_tick(trace=directory, fault=fault)
                trace = driver.wait_supervisor(trace=directory, timeout=30)
                assert isinstance(trace, dict), "Expected a faulted supervisor trace"
                assert trace["lines"][-1] == "fault " + fault
                identifier = trace["activation"]

                observations = [driver.recovery_observation()]
                driver.tick()
                observations.append(driver.recovery_observation())
                driver.tick()
                observations.append(driver.recovery_observation())
                # Check H(c) requires a third tick after the two recovery ticks.
                driver.tick()
                observations.append(driver.recovery_observation())
                check_h(observations)

                record = driver.receipt(identifier)
                assert isinstance(record, dict), "Expected the faulted activation record"
                faulted_starts = driver.starts(activation=identifier)
                assert faulted_starts <= 1
                total_starts = driver.starts()
                starts = driver.launches("pm1tl1")
                followed = any(
                    call.get("activation", call.get("env", {}).get("YGG_ACTIVATION"))
                    not in {None, identifier} for call in starts
                )
                exited = bool(record.get("exited_at")) and record.get("exit_code") == 0
                uncertain = identifier in observations[-1]["status"]["uncertain_keys"]
                aborted_then_launched = (record.get("terminal") == "aborted"
                                         and faulted_starts == 0
                                         and total_starts == 1 and followed)
                assert exited or uncertain or aborted_then_launched
            except AssertionError as error:
                failures.append(fault + ": " + str(error))
    assert not failures, "\n".join(failures)
