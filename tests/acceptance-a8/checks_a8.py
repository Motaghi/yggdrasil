"""Assertions over public observations, with no I/O."""

import re


def node(status, identifier="pm1tl1"):
    assert isinstance(status, dict), "Expected status JSON"
    matches = [entry for entry in status["nodes"] if entry["id"] == identifier]
    assert len(matches) == 1, "Expected node " + identifier
    return matches[0]


def activation(record):
    assert isinstance(record, dict), "Expected an activation receipt"
    return record["activation"]


def single_intent(intents):
    assert len(intents) == 1, "Expected one safety intent marker"
    assert re.fullmatch(r"si-[0-9a-f]{16}", intents[0])
    return intents[0]


def launch_persistent(driver):
    driver.agents(pm1tl1="persist")
    driver.run("launch", "pm1tl1")
    assert driver.wait_starts("pm1tl1", 1) == 1
    record = driver.latest()
    assert isinstance(record, dict)
    assert type(record["pid"]) is int
    assert driver.process_alive(record["pid"])
    return record["pid"]


def offline_stop(driver):
    pid = launch_persistent(driver)
    driver.readonly(driver.tracker)
    result = driver.run("stop", "alpha")
    assert result.returncode == 8
    assert "stopped 1 session(s); tracker write failed" in result.stderr
    return pid
