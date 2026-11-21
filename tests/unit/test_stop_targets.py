import json
import os
import subprocess
import time

import pytest

from yggdrasil import state, stop, tracker, util


def _receipt(root, activation, process, node="pm1tl1", programme="alpha"):
    path = root / "activations" / (activation + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"node": node, "activation": activation, "programme": programme,
                                "project": "alpha-p1", "pid": process.pid,
                                "process_started_at": util.process_start(process.pid), "exited_at": None}))


def test_stop_signals_live_receipts_without_tracker_or_runtime(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setenv("YGG_STATE_ROOT", str(root))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "tracker"))
    monkeypatch.delenv("YGG_NODE", raising=False)
    root.mkdir()
    child = subprocess.Popen(["sleep", "60"], start_new_session=True)
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        _receipt(root, "act-1", child)
        _receipt(root, "act-2", other, node="pm2tl1", programme="beta")

        def unavailable(*args, **kwargs):
            raise OSError("tracker unavailable")

        monkeypatch.setattr(state, "all_nodes", unavailable)
        monkeypatch.setattr(tracker, "programme", unavailable)
        with pytest.raises(util.YggError) as caught:
            stop.stop("alpha")
        assert caught.value.code == 8
        assert caught.value.message == "stopped 1 session(s); tracker write failed"
        child.wait(timeout=15)
        assert child.returncode is not None
        time.sleep(0.2)
        assert other.poll() is None
        # The stop holds through its safety-folder intent marker (no cached state, no state-root stop list).
        from yggdrasil import safety
        assert [(m["kind"], m["scope"]) for m in safety.markers("alpha")] == [("stop", "alpha")]
    finally:
        for process in (child, other):
            if process.poll() is None:
                os.killpg(process.pid, 9)
                process.wait()
