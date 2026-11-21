import pytest

from checks_a8 import launch_persistent, node, offline_stop, single_intent


@pytest.mark.ac("AC-09")
def test_offline_stop_kills_agent_and_blocks_relaunch(a8):
    with a8.fresh("offline-stop") as driver:
        pid = offline_stop(driver)
        driver.tick()
        assert driver.wait_dead(pid, timeout=15)
        assert node(driver.status())["state"] == "stopped"
        assert driver.starts() == 1


@pytest.mark.ac("AC-09")
def test_offline_pause_blocks_pending_work(a8):
    with a8.fresh("offline-pause") as driver:
        driver.message()
        driver.readonly(driver.tracker)
        result = driver.run("pause", "alpha")
        assert result.returncode == 8
        driver.tick()
        driver.tick()
        assert driver.starts() == 0
        assert "alpha" in driver.status()["paused"]["programmes"]


@pytest.mark.ac("AC-09")
def test_offline_stop_survives_full_state_wipe_and_is_journalled(a8):
    with a8.fresh("offline-stop-wipe") as driver:
        offline_stop(driver)
        intent = single_intent(driver.intents())
        driver.wipe_state()
        assert driver.intents() == [intent]
        driver.tracker_writable()
        driver.run("rehydrate")
        driver.tick()
        driver.tick()
        assert node(driver.status())["state"] == "stopped"
        assert driver.starts() == 1
        assert intent in driver.intents()
        assert any(entry.get("kind") == "stop" and entry.get("scope") == "alpha"
                   and entry.get("intent") == intent for entry in driver.journal_entries())


@pytest.mark.ac("AC-10")
def test_tick_completes_stop_that_crashed_before_signalling(a8):
    with a8.fresh("stop-before-signal") as driver:
        pid = launch_persistent(driver)
        result = driver.fault_step("boundary:before-signal", "stop", "alpha")
        assert result.returncode == 97
        single_intent(driver.intents())
        assert driver.process_alive(pid)
        driver.tick()
        assert driver.wait_dead(pid, timeout=15)
        assert node(driver.status())["state"] == "stopped"
        assert driver.starts() == 1


@pytest.mark.ac("AC-11")
def test_interrupted_resume_blocks_until_tick_then_launches_once(a8):
    with a8.fresh("interrupted-resume") as driver:
        driver.agents(pm1tl1="exit0")
        driver.run("pause", "alpha")
        driver.message()
        result = driver.fault_step("boundary:after-resume-commit", "resume", "alpha")
        assert result.returncode == 97
        status = driver.status()
        assert "alpha" in status["paused"]["programmes"]
        assert driver.starts() == 0
        driver.tick()
        assert driver.starts() == 1
        assert "alpha" not in driver.status()["paused"]["programmes"]
        driver.tick()
        assert driver.starts() == 1


@pytest.mark.ac("AC-11")
def test_old_resume_cannot_clear_new_offline_pause(a8):
    with a8.fresh("new-offline-pause") as driver:
        driver.agents(pm1tl1="exit0")
        driver.run("pause", "alpha")
        old_intent = single_intent(driver.intents())
        result = driver.run("resume", "alpha")
        assert result.returncode == 0
        driver.readonly(driver.tracker)
        result = driver.run("pause", "alpha")
        assert result.returncode == 8
        new_intent = single_intent(driver.intents())
        assert new_intent != old_intent
        driver.tracker_writable()
        driver.message()
        driver.tick()
        driver.tick()
        assert driver.starts() == 0
        assert "alpha" in driver.status()["paused"]["programmes"]
        entries = driver.journal_entries()
        assert any(entry.get("kind") == "pause" and entry.get("intent") == new_intent
                   for entry in entries)
        resumes = [entry for entry in entries if entry.get("kind") == "resume"]
        assert any(old_intent in entry["clears"] for entry in resumes)
        assert all(new_intent not in entry["clears"] for entry in resumes)
