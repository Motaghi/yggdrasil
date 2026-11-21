import pytest


TICK = ("conductor", "tick")


def _observe(driver, node):
    observation = driver.observe(node)
    assert isinstance(observation, dict)
    assert isinstance(observation["status"], dict)
    # Shared Check H(a), using only activation files and OS process evidence.
    assert len(observation["live"]) <= 1
    return observation


def _uncertain(observation, node):
    activations = {record["activation"] for record in observation["records"]}
    for entry in observation["status"]["nodes"]:
        if entry["id"] == node and entry["activation"]:
            activations.add(entry["activation"]["id"])
    return bool(activations.intersection(observation["status"]["uncertain_keys"]))


def _recover(driver, node, *, idempotent=True):
    driver.run(*TICK)
    first = _observe(driver, node)
    driver.run(*TICK)
    second = _observe(driver, node)
    if _uncertain(first, node):
        assert second["starts"] == first["starts"], "Check H(b)"
    observations = [first, second]
    if idempotent:
        driver.run(*TICK)
        third = _observe(driver, node)
        assert {key: value for key, value in second["status"].items()
                if key != "generated_at"} == {
                    key: value for key, value in third["status"].items()
                    if key != "generated_at"
                }, "Check H(c)"
        observations.append(third)
    return observations


def _prepare_launch(driver):
    driver.fake_behavior(claude={"agents": {"pm1tl1": {"mode": "persist"}}})
    driver.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "hello")


def _prepare_pause(driver):
    driver.fake_behavior(claude={"agents": {"pm1tl1": {"mode": "persist"}}})
    result = driver.fault_step("boundary:before-spawn", "launch", "pm1tl1")
    assert result.returncode == 97


def _prepare_acknowledgement(driver):
    driver.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "hello")
    status = driver.status()
    assert isinstance(status, dict)
    messages = [entry["msg_id"] for entry in status["inbox_pending"]
                if entry["node"] == "pm1tl1"]
    assert messages
    driver.fake_behavior(claude={"agents": {"pm1tl1": {
        "mode": "script", "steps": [
            {"id": "ack-" + message,
             "run": ["ygg", "inbox", "pm1tl1", "--ack", message]}
            for message in messages
        ],
    }}})
    return messages


def _prepare_batch(driver):
    driver.fake_behavior(claude={"agents": {
        "pm1": {"mode": "exit", "exit_code": 0},
        "pm1tl1": {"mode": "persist"}, "pm1tl2": {"mode": "persist"},
    }})
    driver.package("WP-41", "pm1tl1", criteria=True, authorise=True)
    driver.package("WP-7", "pm1tl2", criteria=True, authorise=True)
    for node in ("pm1tl1", "pm1tl2"):
        driver.run("launch", node)
        driver.wait_launch(node)
    driver.clear_calls()
    for node, package, body in (("pm1tl1", "WP-41", "first"),
                                ("pm1tl2", "WP-7", "second")):
        driver.run("tracker", "write", "--as", node, "--project", "alpha-p1",
                   "--package", package, "--kind", "checkpoint", "--body", body)
    status = driver.status()
    assert isinstance(status, dict)
    messages = [entry["msg_id"] for entry in status["inbox_pending"]
                if entry["node"] == "pm1"]
    assert messages
    driver.fake_behavior(claude={"agents": {
        "pm1": {"mode": "script", "steps": [
            {"id": "ack-" + message,
             "run": ["ygg", "inbox", "pm1", "--ack", message]}
            for message in messages
        ]},
        "pm1tl1": {"mode": "persist"}, "pm1tl2": {"mode": "persist"},
    }})


def _prepare_capacity(driver):
    driver.at("2026-11-16T10:00:00+01:00")
    driver.fake_behavior(claude={"agents": {"pm1tl1": {"mode": "capacity"}}})
    driver.run("send", "--from", "pm1", "--to", "pm1tl1",
               "--kind", "work", "--body", "Do WP-1")
    driver.launch_capacity_agent("pm1tl1")
    driver.wait_launch("pm1tl1")
    driver.wait_exit("pm1tl1")
    before = _observe(driver, "pm1tl1")
    current = next(node for node in before["status"]["nodes"]
                   if node["id"] == "pm1tl1")["activation"]["id"]
    receipt = next(record for record in before["records"]
                   if record["activation"] == current)
    return receipt["retries_used"]


def _prepare_stop(driver):
    driver.fake_behavior(claude={"agents": {"pm1tl1": {"mode": "persist"}}})
    driver.run("launch", "pm1tl1")
    driver.wait_launch("pm1tl1")
    records = driver.receipts("pm1tl1")
    pids = [record["pid"] for record in records
            if not record.get("exited_at") and driver.process_alive(record.get("pid"))]
    assert pids
    return pids


def _discover(factory, name, prepare, step):
    with factory.fresh(name + "-trace") as driver:
        prepare(driver)
        _, trace = driver.trace_step(*step)
        writes = [line for line in trace.splitlines() if line.startswith("write ")]
        # An empty trace must fail discovery instead of silently skipping the sweep.
        assert writes, "The specified sweep requires durable-write trace lines"
        return ["write:" + str(index) for index in range(1, len(writes) + 1)]


@pytest.mark.ac("AC-08")
def test_launch_and_confirm_survive_every_write_crash(a7_tree):
    faults = _discover(a7_tree, "launch", _prepare_launch, TICK)
    for fault in faults:
        with a7_tree.fresh("launch-" + fault.replace(":", "-")) as driver:
            _prepare_launch(driver)
            _observe(driver, "pm1tl1")
            result = driver.fault_step(fault, *TICK)
            assert result.returncode == 97, fault
            _observe(driver, "pm1tl1")

            final = _recover(driver, "pm1tl1")[-1]

            assert final["starts"] == 1 or (
                final["starts"] == 0 and (
                    "pm1tl1" in final["status"]["held_launches"]
                    or _uncertain(final, "pm1tl1")
                )
            ), fault


@pytest.mark.ac("AC-09")
def test_pause_during_launch_survives_every_write_crash(a7_tree):
    step = ("pause", "alpha")
    faults = _discover(a7_tree, "pause", _prepare_pause, step)
    for fault in faults:
        with a7_tree.fresh("pause-" + fault.replace(":", "-")) as driver:
            _prepare_pause(driver)
            _observe(driver, "pm1tl1")
            result = driver.fault_step(fault, *step)
            assert result.returncode in (0, 97), fault
            crashed = _observe(driver, "pm1tl1")

            recovered = _recover(driver, "pm1tl1")

            if result.returncode == 0:
                assert all(observation["starts"] == crashed["starts"]
                           for observation in recovered), fault
            else:
                assert driver.run(*step).returncode == 0, fault
                _observe(driver, "pm1tl1")


@pytest.mark.ac("AC-10")
def test_acknowledgement_during_confirmation_survives_every_write_crash(a7_tree):
    faults = _discover(a7_tree, "ack", _prepare_acknowledgement, TICK)
    for fault in faults:
        with a7_tree.fresh("ack-" + fault.replace(":", "-")) as driver:
            messages = _prepare_acknowledgement(driver)
            _observe(driver, "pm1tl1")
            result = driver.fault_step(fault, *TICK)
            assert result.returncode == 97, fault
            crashed = _observe(driver, "pm1tl1")

            observations = [crashed, *_recover(driver, "pm1tl1")]

            for index, observation in enumerate(observations):
                if any(record.get("exited_at") and record.get("exit_code") == 0
                       for record in observation["records"]):
                    assert all(entry["msg_id"] not in messages
                               for entry in observation["status"]["inbox_pending"]
                               if entry["node"] == "pm1tl1"), fault
                    assert all(later["starts"] == observation["starts"]
                               for later in observations[index + 1:]), fault


@pytest.mark.ac("AC-11")
def test_checkpoint_batch_survives_every_write_crash(a7_tree):
    faults = _discover(a7_tree, "batch", _prepare_batch, TICK)
    for fault in faults:
        with a7_tree.fresh("batch-" + fault.replace(":", "-")) as driver:
            _prepare_batch(driver)
            _observe(driver, "pm1")
            result = driver.fault_step(fault, *TICK)
            assert result.returncode == 97, fault
            _observe(driver, "pm1")

            final = _recover(driver, "pm1")[-1]

            assert final["starts"] <= 1, fault


@pytest.mark.ac("AC-12")
def test_capacity_hold_and_rehydrate_survive_every_write_crash(a7_tree):
    faults = _discover(a7_tree, "capacity", _prepare_capacity, TICK)
    for fault in faults:
        with a7_tree.fresh("capacity-" + fault.replace(":", "-")) as driver:
            retries_before = _prepare_capacity(driver)
            result = driver.fault_step(fault, *TICK)
            assert result.returncode == 97, fault
            crashed = _observe(driver, "pm1tl1")

            driver.at("2026-11-16T10:02:00+01:00")
            driver.run("rehydrate")
            rehydrated = _observe(driver, "pm1tl1")
            observations = [crashed, rehydrated, *_recover(driver, "pm1tl1")]

            for observation in observations:
                assert observation["starts"] == 1, fault
                current = next(node for node in observation["status"]["nodes"]
                               if node["id"] == "pm1tl1")["activation"]["id"]
                receipt = next(record for record in observation["records"]
                               if record["activation"] == current)
                assert receipt["retries_used"] >= retries_before, fault


@pytest.mark.ac("AC-13")
def test_stop_survives_every_write_and_before_signal_crash(a7_tree):
    step = ("stop", "alpha")
    faults = _discover(a7_tree, "stop", _prepare_stop, step)
    faults.append("boundary:before-signal")
    for fault in faults:
        with a7_tree.fresh("stop-" + fault.replace(":", "-")) as driver:
            pids = _prepare_stop(driver)
            starts_before = len(driver.launches("pm1tl1"))
            result = driver.fault_step(fault, *step)
            assert result.returncode in (0, 97), fault
            if fault == "boundary:before-signal":
                assert result.returncode == 97, fault
            if result.returncode == 97:
                assert driver.run(*step).returncode == 0, fault

            driver.run(*TICK)
            driver.run(*TICK)

            assert all(not driver.process_alive(pid) for pid in pids), fault
            status = driver.status()
            assert isinstance(status, dict)
            assert next(node for node in status["nodes"]
                        if node["id"] == "pm1tl1")["state"] == "stopped", fault
            assert len(driver.launches("pm1tl1")) == starts_before, fault
            driver.run(*TICK)
            assert len(driver.launches("pm1tl1")) == starts_before, fault
            driver.run("resume", "alpha")


@pytest.mark.ac("AC-14")
def test_full_wipe_rehydrates_every_crashed_launch_state(a7_tree):
    faults = _discover(a7_tree, "wipe", _prepare_launch, TICK)
    for fault in faults:
        with a7_tree.fresh("wipe-" + fault.replace(":", "-")) as driver:
            _prepare_launch(driver)
            _observe(driver, "pm1tl1")
            result = driver.fault_step(fault, *TICK)
            assert result.returncode == 97, fault
            captured = _observe(driver, "pm1tl1")

            driver.wipe_state()
            driver.run("rehydrate")
            _observe(driver, "pm1tl1")
            final = _recover(driver, "pm1tl1", idempotent=False)[-1]

            assert (
                final["status"]["uncertain_keys"] == captured["status"]["uncertain_keys"]
                and final["status"]["held_launches"] == captured["status"]["held_launches"]
            ) or final["starts"] == captured["starts"], fault
