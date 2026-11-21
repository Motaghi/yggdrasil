import pytest

from checks_a8 import node, single_intent


@pytest.mark.ac("AC-03")
def test_recovery_aborts_before_gate_and_launches_one_successor(a8):
    with a8.fresh("recovery-before-gate") as driver:
        driver.agents(pm1tl1="persist")
        driver.message()
        with driver.barrier_tick("before-gate") as launcher:
            barrier = driver.wait_barrier("before-gate", timeout=30)
            assert isinstance(barrier, dict), "Expected the before-gate barrier"
            identifier = barrier["activation"]
            driver.kill_launcher(launcher)
            driver.tick()
            driver.continue_barrier(barrier)
            assert driver.wait_dead(barrier["pid"], timeout=10)
            driver.tick()

            record = driver.receipt(identifier)
            assert isinstance(record, dict), "Expected A1's activation record"
            assert record.get("terminal") == "aborted"
            assert driver.starts(activation=identifier) == 0
            assert driver.starts() == 1
            live = [record for record in driver.receipts("pm1tl1")
                    if type(record.get("pid")) is int
                    and driver.process_alive(record["pid"])]
            assert len(live) == 1


@pytest.mark.ac("AC-03")
def test_recovery_keeps_spawned_reservation_before_final_check(a8):
    with a8.fresh("recovery-before-final-check") as driver:
        driver.agents(pm1tl1="persist")
        driver.message()
        with driver.barrier_tick("before-final-check") as launcher:
            barrier = driver.wait_barrier("before-final-check", timeout=30)
            assert isinstance(barrier, dict), "Expected the before-final-check barrier"
            identifier = barrier["activation"]
            before = {item["name"] for item in driver.activation_files("pm1tl1")}
            driver.kill_launcher(launcher)
            driver.tick()
            assert all(item["name"] in before
                       for item in driver.activation_files("pm1tl1"))
            driver.continue_barrier(barrier)
            assert driver.wait_starts("pm1tl1", 1, timeout=30) == 1
            driver.tick()

            assert driver.starts() == 1
            files = driver.activation_files("pm1tl1")
            assert all(item["name"] in before for item in files)
            original = [item for item in files
                        if item["record"]["activation"] == identifier]
            assert len(original) == 1
            assert all(item["mtime"] <= original[0]["mtime"] for item in files)


@pytest.mark.ac("AC-04")
def test_stop_before_final_check_aborts_without_starting_agent(a8):
    with a8.fresh("stop-before-final-check") as driver:
        driver.agents(pm1tl1="persist")
        driver.message()
        with driver.barrier_tick("before-final-check") as launcher:
            barrier = driver.wait_barrier("before-final-check", timeout=30)
            assert isinstance(barrier, dict), "Expected the before-final-check barrier"
            driver.kill_launcher(launcher)
            result = driver.run("stop", "alpha")
            assert result.returncode == 0
            assert result.stdout.strip() == "stopped 0 session(s)"
            driver.continue_barrier(barrier)
            assert driver.wait_dead(barrier["pid"], timeout=10)
            driver.tick()

            assert driver.starts() == 0
            assert node(driver.status())["state"] == "stopped"
            record = driver.receipt(barrier["activation"])
            assert isinstance(record, dict), "Expected A1's activation record"
            assert record.get("terminal") == "aborted"


@pytest.mark.ac("AC-04")
def test_pause_before_final_check_blocks_until_resume(a8):
    with a8.fresh("pause-before-final-check") as driver:
        driver.agents(pm1tl1="persist")
        driver.message()
        with driver.barrier_tick("before-final-check") as launcher:
            barrier = driver.wait_barrier("before-final-check", timeout=30)
            assert isinstance(barrier, dict), "Expected the before-final-check barrier"
            driver.kill_launcher(launcher)
            result = driver.run("pause", "alpha")
            assert result.returncode == 0
            driver.continue_barrier(barrier)
            assert driver.wait_dead(barrier["pid"], timeout=10)
            driver.tick()
            assert driver.starts() == 0
            driver.run("resume", "alpha")
            driver.tick()
            assert driver.starts() == 1


@pytest.mark.ac("AC-04")
def test_stop_returns_while_journal_locked_then_reconciles_intent(a8):
    with a8.fresh("stop-in-journal-write") as driver:
        driver.agents(pm1tl1="persist")
        driver.message()
        with driver.barrier_tick("in-journal-write"):
            barrier = driver.wait_barrier("in-journal-write", timeout=30)
            assert isinstance(barrier, dict), "Expected the in-journal-write barrier"
            result, elapsed = driver.timed_run("stop", "alpha")
            assert result.returncode == 8
            assert elapsed <= 20
            assert "tracker write pending" in result.stderr
            intent = single_intent(driver.intents())
            driver.continue_barrier(barrier)
            assert driver.wait_dead(barrier["pid"], timeout=15)
            driver.tick()

            assert driver.starts() == 0
            assert node(driver.status())["state"] == "stopped"
            assert any(entry.get("kind") == "stop" and entry.get("intent") == intent
                       for entry in driver.journal_entries("alpha"))
