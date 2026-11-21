import pytest


@pytest.mark.ac("AC-02")
def test_user_releases_uncertain_activation_without_process(a7_tree):
    a7_tree.seed_activation(
        "pm1tl1", "--state", "running", "--process", "none",
        "--outcome", "uncertain",
    )

    result = a7_tree.run("node", "release", "pm1tl1", "--as", "aborted")

    assert result.returncode == 0
    status = a7_tree.status()
    assert isinstance(status, dict)
    node = next(node for node in status["nodes"] if node["id"] == "pm1tl1")
    assert node["state"] == "idle"
    assert status["uncertain_keys"] == []


@pytest.mark.ac("AC-02")
def test_managed_node_cannot_release_uncertain_activation(a7_tree):
    a7_tree.seed_activation(
        "pm1tl1", "--state", "running", "--process", "none",
        "--outcome", "uncertain",
    )

    result = a7_tree.run(
        "node", "release", "pm1tl1", "--as", "aborted", env={"YGG_NODE": "pm1"},
    )

    assert result.returncode == 3
    assert result.stderr.rstrip("\n") == "node release is user-only"


@pytest.mark.ac("AC-02")
def test_user_cannot_release_uncertain_live_activation(a7_tree):
    a7_tree.seed_activation(
        "pm1tl1", "--state", "running", "--process", "live",
        "--outcome", "uncertain",
    )

    result = a7_tree.run("node", "release", "pm1tl1", "--as", "failed")

    assert result.returncode == 3
    assert result.stderr.rstrip("\n") == (
        "pm1tl1 has a live activation; reset it with --kill first"
    )


@pytest.mark.ac("AC-03")
def test_user_resets_stalled_node_with_spent_retry_budget(a7_tree):
    a7_tree.at("2026-11-16T10:05:00+01:00")
    a7_tree.seed_activation(
        "pm1tl1", "--state", "running", "--process", "dead",
        "--heartbeat", "2026-11-15T00:00:00+01:00", "--retries-used", "2",
    )
    a7_tree.run("conductor", "tick")
    before = a7_tree.status()
    assert isinstance(before, dict)
    assert next(node for node in before["nodes"]
                if node["id"] == "pm1tl1")["state"] == "stalled"

    result = a7_tree.run("node", "reset", "pm1tl1")

    assert result.returncode == 0
    status = a7_tree.status()
    assert isinstance(status, dict)
    assert next(node for node in status["nodes"]
                if node["id"] == "pm1tl1")["state"] == "idle"


@pytest.mark.ac("AC-03")
def test_reset_refuses_stalled_suspect_live_activation(a7_tree):
    a7_tree.stalled_suspect_seed()

    result = a7_tree.run("node", "reset", "pm1tl1")

    assert result.returncode == 3
    assert result.stderr.rstrip("\n") == "pm1tl1 has a live activation; use --kill"


@pytest.mark.ac("AC-03")
def test_reset_with_kill_ends_seeded_process(a7_tree):
    a7_tree.stalled_suspect_seed()
    seeded = a7_tree.receipts("pm1tl1")
    assert seeded
    pids = [record["pid"] for record in seeded]
    assert any(a7_tree.process_alive(pid) for pid in pids)

    result = a7_tree.run("node", "reset", "pm1tl1", "--kill")

    assert result.returncode == 0
    assert all(not a7_tree.process_alive(pid) for pid in pids)
    status = a7_tree.status()
    assert isinstance(status, dict)
    assert next(node for node in status["nodes"]
                if node["id"] == "pm1tl1")["state"] == "idle"


@pytest.mark.ac("AC-06")
def test_managed_node_cannot_tick_heimdall(a7_tree):
    result = a7_tree.run("heimdall", "tick", env={"YGG_NODE": "pm1"})

    assert result.returncode == 3
    assert result.stderr.rstrip("\n") == "REFUSED: user-only: 'heimdall tick' is a user verb and cannot run in a managed session"


@pytest.mark.ac("AC-06")
def test_fault_injection_is_ignored_in_managed_session(a7_tree):
    result = a7_tree.run(
        "send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "hello",
        env={"YGG_FAULT_AT": "write:1", "YGG_NODE": "pm1"},
    )

    assert result.returncode == 0


@pytest.mark.ac("AC-06")
def test_fault_boundary_listing_has_exact_order(a7_tree):
    result = a7_tree.run("test", "faults")

    assert result.returncode == 0
    assert result.stdout == "before-spawn\nafter-spawn\nafter-exit\nbefore-signal\n"


@pytest.mark.ac("AC-07")
def test_send_traces_first_durable_write(a7_tree):
    # The example's /tmp path is relocated inside this test's temporary root.
    result, trace = a7_tree.trace_step(
        "send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "hello",
    )

    assert result.returncode == 0
    assert any(line.startswith("write 1 ") for line in trace.splitlines())


@pytest.mark.ac("AC-07")
def test_send_fault_after_first_durable_write(a7_tree):
    result = a7_tree.fault_step(
        "write:1", "send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "hello",
    )

    assert result.returncode == 97


@pytest.mark.ac("AC-07")
def test_before_spawn_fault_records_no_agent_start(a7_tree):
    result = a7_tree.fault_step("boundary:before-spawn", "launch", "pm1tl1")

    assert result.returncode == 97
    assert a7_tree.launches("pm1tl1") == []
