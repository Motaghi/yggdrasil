"""v1.1.1 relay: recovery holds while a recorded relay child still runs, and each relay child is recorded
with its start time."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from yggdrasil import launcher, relay, state, tracker, util


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    return tmp_path


# ---------------------------------------------------------------- relay servers in a temporary state root

def _server(root):
    activation = "act-0007"
    fields = relay.make_folder(activation)
    box = root / "box"
    data = dict(fields, node="pm1tl1", activation=activation, role="tl", cli="codex", box=str(box),
                cwd=str(box / "checkout"), relay=str(relay.relay_root(activation)), agent_nonce="n" * 32,
                pinned={"YGG_STATE_ROOT": str(state.root())})
    return relay.Server(activation, data, {"id": "pm1tl1", "role": "tl"}), data


def _fake_child(server, request_id, command, deadline=60.0):
    """Start a relay child as _start does: its own group, output into the request's capture folder."""
    temporary = server.root / "tmp" / request_id
    temporary.mkdir()
    capture = server.root / "tmp" / (request_id + ".run")
    capture.mkdir()
    with open(capture / "stdout", "wb") as out, open(capture / "stderr", "wb") as err:
        process = subprocess.Popen(command, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                   start_new_session=True)
    server.child = {"id": request_id, "process": process, "deadline": time.monotonic() + deadline, "files": [],
                    "tmp": temporary, "capture": capture, "killed_at": None}
    record = getattr(server, "_record_child", None)
    if record is not None:
        record(server.child)
    return process


def _group_gone(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


# ---------------------------------------------------------------- recovery and a live relay child

def _dead_process():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _crashed_receipt(activation):
    dead = _dead_process()
    data = launcher.receipt(activation)
    data.update(node="pm1tl1", activation=activation, supervisor_pid=dead,
                supervisor_started_at="Mon Jan  1 00:00:00 2001", pid=dead, process_started_at="x",
                agent_nonce="n" * 32, outcome="confirmed", phase="starting")
    util.write_json(launcher.activation_path(activation), data)


def test_recovery_holds_while_a_recorded_relay_child_runs(root):
    server, _ = _server(root)
    request_id = "4" * 32
    process = _fake_child(server, request_id, ["sleep", "30"])
    server.close_descriptors()  # the supervisor is gone; its child runs on in its own group
    _crashed_receipt("act-0007")
    try:
        assert relay.recover() == []
        assert relay.relay_root("act-0007").is_dir()
        assert launcher.receipt("act-0007").get("exited_at") is None
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    assert relay.recover() == ["act-0007"]
    assert launcher.receipt("act-0007").get("exited_at") is not None
    kept = state.root() / "output" / "act-0007" / "relay"
    assert (kept / "children.jsonl").is_file()


def test_relay_child_identity_is_recorded_with_its_start_time(root):
    server, _ = _server(root)
    process = _fake_child(server, "5" * 32, ["sleep", "5"])
    try:
        lines = [json.loads(line) for line in (server.root / "children.jsonl").read_text().splitlines()]
        assert lines[0]["id"] == "5" * 32 and lines[0]["pid"] == process.pid and lines[0]["started_at"]
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        server.close_descriptors()
