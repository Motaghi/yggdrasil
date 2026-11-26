"""v1.1.1 relay: a request's outcome covers its whole process group. A descendant that outlives the request's
leader is waited for, ended at the deadline, or ended by a stop, before the outcome is written."""

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


# ---------------------------------------------------------------- the whole request group

def test_outcome_waits_for_a_surviving_descendant(root):
    server, _ = _server(root)
    request_id = "1" * 32
    marker = root / "descendant-done"
    process = _fake_child(server, request_id, ["sh", "-c", "(sleep 1.5; touch " + str(marker) + ") & exit 0"])
    process.wait()
    server.reap()
    assert server.child is not None, "the leader exited while its descendant still runs"
    assert not (server.root / "out" / (request_id + ".json")).exists()
    deadline = time.monotonic() + 10
    while server.child is not None and time.monotonic() < deadline:
        server.reap()
        time.sleep(0.05)
    assert marker.exists()
    assert json.loads((server.root / "out" / (request_id + ".json")).read_text())["exit"] == 0
    server.close_descriptors()


def test_deadline_ends_a_descendant_after_its_leader_exited(root, monkeypatch):
    monkeypatch.setattr(relay, "KILL_GRACE", 0.5)
    server, _ = _server(root)
    request_id = "2" * 32
    process = _fake_child(server, request_id, ["sh", "-c", "trap '' TERM; (trap '' TERM; sleep 60) & exit 0"],
                          deadline=0.2)
    process.wait()
    deadline = time.monotonic() + 15
    while server.child is not None and time.monotonic() < deadline:
        server.reap()
        time.sleep(0.05)
    assert server.child is None
    assert _group_gone(process.pid)
    response = json.loads((server.root / "out" / (request_id + ".json")).read_text())
    assert response["exit"] == 75 and "ran out of time; its outcome is unknown" in response["stderr"]
    server.close_descriptors()


def test_stop_ends_a_descendant_before_the_outcome(root, monkeypatch):
    monkeypatch.setattr(relay, "KILL_GRACE", 0.5)
    server, _ = _server(root)
    request_id = "3" * 32
    process = _fake_child(server, request_id, ["sh", "-c", "(trap '' TERM; sleep 60) & exit 0"])
    process.wait()
    server.reap()
    server.stop()
    deadline = time.monotonic() + 15
    while server.child is not None and time.monotonic() < deadline:
        server.reap()
        time.sleep(0.05)
    assert server.child is None and _group_gone(process.pid)
    response = json.loads((server.root / "out" / (request_id + ".json")).read_text())
    assert response["exit"] == 75 and "the session was stopped while request" in response["stderr"]
    server.close_descriptors()
