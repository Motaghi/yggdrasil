"""Amendment A7 step 1: fault injection (A7.2) and the pilot-blocker rules (A7.5) on today's core."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)

YGG = str(Path(sys.executable).parent / "ygg")


def ygg(*args, env=None):
    environment = dict(os.environ)
    environment.update(env or {})
    for key in [k for k, v in environment.items() if v is None]:
        environment.pop(key)
    return subprocess.run([YGG, *args], env=environment, capture_output=True, text=True, timeout=60)


def cli(*args, env=None, monkeypatch=None):
    """Run the CLI in-process (no fault may fire here); returns (exit, stderr)."""
    from yggdrasil import cli as ygg_cli
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = ygg_cli.main(list(args))
    return code, err.getvalue().strip()


# A7.2: the boundary listing, in the order of the spec.
def test_fault_boundary_listing(world):
    result = ygg("test", "faults")
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["before-spawn", "after-spawn", "after-exit", "before-signal"]


# A7.2: the trace names every durable write in order, relative to the state or tracker root.
def test_fault_trace_lists_durable_writes(world):
    trace = world / "trace.txt"
    result = ygg("send", "--from", "pm1", "--to", "tl1", "--kind", "work", "--body", "hello", env={"YGG_FAULT_TRACE": str(trace)})
    assert result.returncode == 0, result.stderr
    lines = trace.read_text().splitlines()
    assert lines[0].startswith("write 1 ")
    assert [int(line.split()[1]) for line in lines] == list(range(1, len(lines) + 1))
    assert not any(line.split()[2].startswith("/") for line in lines)


# A7.2: write:k ends the process with exit 97 right after its k-th durable write (no cleanup runs).
def test_fault_at_write_exits_97_after_that_write(world):
    trace = world / "trace.txt"
    result = ygg("send", "--from", "pm1", "--to", "tl1", "--kind", "work", "--body", "hello",
                 env={"YGG_FAULT_AT": "write:2", "YGG_FAULT_TRACE": str(trace)})
    assert result.returncode == 97
    assert [line.split()[1] for line in trace.read_text().splitlines()] == ["1", "2"]


# A7.2: inert in managed sessions and outside test mode.
@pytest.mark.parametrize("extra", [{"YGG_NODE": "pm1"}, {"YGG_ACTIVATION": "act-0001"}, {"YGG_TEST_MODE": "0"}])
def test_faults_are_inert_in_managed_sessions_and_outside_test_mode(world, extra):
    trace = world / "trace.txt"
    result = ygg("send", "--from", "pm1", "--to", "tl1", "--kind", "work", "--body", "hello",
                 env=dict({"YGG_FAULT_AT": "write:1", "YGG_FAULT_TRACE": str(trace)}, **extra))
    assert result.returncode == 0, result.stderr
    assert not trace.exists()


# A7.2: boundary before-spawn: the activation is reserved and no agent starts.
def test_fault_at_before_spawn_starts_no_agent(world):
    result = ygg("launch", "tl1", env={"YGG_FAULT_AT": "boundary:before-spawn"})
    assert result.returncode == 97
    assert not (world / "agent.log").exists()


# A7.2: boundary before-signal: the stop markers are written, no signal is sent.
def test_fault_at_before_signal_keeps_the_stop_marker(world):
    from yggdrasil import interlock
    result = ygg("stop", "alpha", env={"YGG_FAULT_AT": "boundary:before-signal"})
    assert result.returncode == 97
    assert "alpha" in interlock.stop_scopes()  # the safety-folder marker holds it


# A7.2: the trace holds only the started process's writes and boundaries; the supervisor (a child) neither
# traces nor counts writes, but still honours its own boundary faults.
def test_trace_holds_only_the_started_process(world):
    import time
    from yggdrasil import launcher
    trace = world / "trace.txt"
    result = ygg("launch", "tl1", env={"YGG_FAULT_TRACE": str(trace)})
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not launcher.receipt("act-0001").get("exited_at"):
        time.sleep(0.05)
    lines = trace.read_text().splitlines()
    assert [line for line in lines if line.startswith("boundary ")] == ["boundary before-spawn"]
    numbers = [int(line.split()[1]) for line in lines if line.startswith("write ")]
    assert numbers == list(range(1, len(numbers) + 1))


def test_supervisor_honours_its_boundary_fault(world):
    import time
    from yggdrasil import launcher
    ygg("launch", "tl1", env={"YGG_FAULT_AT": "boundary:after-spawn"})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (world / "agent.log").exists():
        time.sleep(0.05)
    assert (world / "agent.log").exists()  # the agent started
    assert launcher.receipt("act-0001").get("pid") is None  # its pid was never recorded


def test_child_env_drops_trace_and_write_faults():
    from yggdrasil import faults
    env = faults.child_env({"YGG_FAULT_TRACE": "/tmp/t", "YGG_FAULT_AT": "write:3", "OTHER": "1"})
    assert env == {"OTHER": "1"}
    assert faults.child_env({"YGG_FAULT_AT": "boundary:after-exit"}) == {"YGG_FAULT_AT": "boundary:after-exit"}


# A7.5 AC-01: launch refuses while any activation record of the node has a live process.
def test_launch_refuses_while_an_activation_has_a_live_process(world):
    from yggdrasil import launcher
    seeded = ygg("test", "activation", "tl1", "--state", "queued", "--process", "live").stdout.strip()
    try:
        with pytest.raises(util.YggError) as error:
            launcher.launch("tl1")
        assert error.value.code == 3 and error.value.message == "tl1 has a live activation; release or reset it first"
        assert not (world / "agent.log").exists()
    finally:
        os.killpg(launcher.receipt(seeded)["pid"], 9)


def test_launch_proceeds_when_the_seeded_process_is_dead(world):
    from yggdrasil import launcher
    ygg("test", "activation", "tl1", "--state", "queued", "--process", "dead")
    assert launcher.launch("tl1")["outcome"] == "confirmed"


# A7.5 AC-02: the user releases an uncertain activation without a live process.
def test_release_settles_an_uncertain_activation(world, monkeypatch):
    from yggdrasil import journal, rehydrate, state, status
    seeded = ygg("test", "activation", "tl1", "--state", "running", "--process", "none",
                 "--outcome", "uncertain").stdout.strip()
    assert cli("node", "release", "tl1", "--as", "aborted", env={"YGG_NODE": "pm1"}, monkeypatch=monkeypatch) == \
        (3, "node release is user-only")
    monkeypatch.delenv("YGG_NODE")
    assert cli("node", "release", "tl1", "--as", "aborted", monkeypatch=monkeypatch)[0] == 0
    view = status.build_status()
    assert {n["id"]: n for n in view["nodes"]}["tl1"]["state"] == "idle"
    assert view["uncertain_keys"] == []
    assert not [e for e in journal.entries("alpha") if e.get("kind") == "uncertain" and not e.get("cleared_at")]
    rehydrate.replay_journal()
    assert state.runtime("tl1")["outcome"] != "uncertain" and seeded not in status.build_status()["uncertain_keys"]


def test_release_refuses_with_a_live_process(world, monkeypatch):
    from yggdrasil import launcher
    seeded = ygg("test", "activation", "tl1", "--state", "running", "--process", "live",
                 "--outcome", "uncertain").stdout.strip()
    try:
        assert cli("node", "release", "tl1", "--as", "failed", monkeypatch=monkeypatch) == \
            (3, "tl1 has a live activation; reset it with --kill first")
    finally:
        os.killpg(launcher.receipt(seeded)["pid"], 9)


# A7.5 AC-03: the user resets a stalled or stalled-suspect node.
def test_reset_returns_a_stalled_node_to_idle(world, monkeypatch):
    from yggdrasil import state
    ygg("test", "activation", "tl1", "--state", "running", "--process", "dead", "--retries-used", "2")
    state.set_state("tl1", "stalled")
    assert cli("node", "reset", "tl1", monkeypatch=monkeypatch)[0] == 0
    assert state.runtime("tl1")["state"] == "idle"


def test_reset_of_a_live_stalled_suspect_needs_kill(world, monkeypatch):
    from yggdrasil import launcher, state
    seeded = ygg("test", "activation", "tl1", "--state", "running", "--process", "live").stdout.strip()
    pid = launcher.receipt(seeded)["pid"]
    state.set_state("tl1", "stalled-suspect")
    assert cli("node", "reset", "tl1", monkeypatch=monkeypatch) == (3, "tl1 has a live activation; use --kill")
    assert cli("node", "reset", "tl1", "--kill", monkeypatch=monkeypatch)[0] == 0
    assert not util.pid_alive(pid)
    assert state.runtime("tl1")["state"] == "idle" and not launcher.live_activations("tl1")


def test_reset_is_user_only(world, monkeypatch):
    from yggdrasil import state
    state.set_state("tl1", "stalled")
    assert cli("node", "reset", "tl1", env={"YGG_NODE": "pm1"}, monkeypatch=monkeypatch) == (3, "node reset is user-only")


# A7.5 AC-04: judgements are recorded only by the acting node itself, never by its parent.
def test_parent_cannot_write_criteria_or_record_judgements_for_a_child(world, monkeypatch):
    from yggdrasil import commands
    commands.tracker_write("pm1", "p1", "work-package", "Build WP-41", stage="1", package="WP-41")
    monkeypatch.setenv("YGG_NODE", "pm1")
    with pytest.raises(util.YggError) as error:
        commands.tracker_write("su1", "p1", "criteria", "WP-41", package="WP-41")
    assert (error.value.code, error.value.message) == (3, "criteria for su1 must be written by su1 itself")
    with pytest.raises(util.YggError) as error:
        commands.review_record("tl1", "WP-41", 1, "pass")
    assert error.value.message == "review for tl1 must be recorded by tl1 itself"
    for verdict, record in (("accept", "acceptance"), ("reject", "rejection")):
        with pytest.raises(util.YggError) as error:
            commands.acceptance_record("su1", "WP-41", "abc1234", verdict)
        assert error.value.message == record + " for su1 must be recorded by su1 itself"
    monkeypatch.setenv("YGG_NODE", "su1")
    commands.tracker_write("su1", "p1", "criteria", "WP-41", package="WP-41")


# A7.5 AC-05 and AC-06: managed sessions cannot schedule.
@pytest.mark.parametrize("args,verb,variable", [
    (("launch", "tl1"), "launch", "YGG_NODE"),
    (("conductor", "tick"), "conductor tick", "YGG_ACTIVATION"),
    (("heimdall", "tick"), "heimdall tick", "YGG_NODE"),
    (("rehydrate",), "rehydrate", "YGG_NODE"),
])
def test_scheduling_verbs_refuse_in_managed_sessions(world, monkeypatch, args, verb, variable):
    value = "act-0001" if variable == "YGG_ACTIVATION" else "pm1"
    # A7.5 I12, through the user-verb table.
    assert cli(*args, env={variable: value}, monkeypatch=monkeypatch) == (
        3, "REFUSED: user-only: '" + verb + "' is a user verb and cannot run in a managed session")
