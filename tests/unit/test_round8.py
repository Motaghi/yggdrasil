"""Unit tests for the round-8 review findings (dev #2 pair review and QA)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_round2 import clear_caches, world  # noqa: F401  (fixture and helpers)
from test_round3 import receipt_file

SRC = Path(__file__).resolve().parents[2] / "src" / "yggdrasil"


def dead_pid():
    done = subprocess.Popen(["true"])
    done.wait()
    return done.pid


def _held_consistently(activation, node="tl1"):
    from yggdrasil import launcher, state
    assert launcher.receipt(activation)["outcome"] == "uncertain"
    assert len([a for a in state.alerts() if a.get("activation") == activation]) == 1
    if state.runtime(node).get("activation") == activation:
        assert state.runtime(node)["outcome"] == "uncertain"


# 2: every path that reports uncertain leaves receipt, runtime and one alert consistent (no stored mirror).
@pytest.mark.parametrize("path", ["confirm", "finalize-pending", "finalize-confirmed", "repair", "rehydrate"])
def test_every_uncertain_path_journals_and_alerts_once(world, path):
    from yggdrasil import conductor, launcher, rehydrate, state
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.1\n")
    clear_caches()
    activation = "act-0042"
    if path == "confirm":
        receipt_file(activation, outcome="pending", phase="starting", pid=None)
        state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "pending"})
        assert launcher.confirm("tl1", activation)["outcome"] == "uncertain"
    elif path == "finalize-pending":
        receipt_file(activation, outcome="pending", phase="starting", pid=dead_pid(), process_started_at="gone")
        state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "pending"})
        assert launcher.finalize("tl1", activation, "failed") == "uncertain"
    else:
        receipt_file(activation, outcome="confirmed", phase="launched", pid=dead_pid(), process_started_at="gone")
        state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "pending"})
        if path == "finalize-confirmed":
            assert launcher.finalize("tl1", activation, "failed") == "uncertain"
        elif path == "repair":
            conductor.reconcile()
        else:
            from yggdrasil import journal
            journal.add("alpha", "uncertain", key=activation, node="tl1")
            launcher.update_receipt(activation, outcome="confirmed")
            rehydrate.replay_journal()
    conductor.reconcile()  # repeating never adds a second entry or alert
    _held_consistently(activation)


# 2: no source line reports uncertain except through the one uncertain-outcome path.
def test_no_code_path_returns_uncertain_without_the_helper():
    offenders = []
    for path in SRC.glob("*.py"):
        lines = path.read_text().splitlines()
        for number, line in enumerate(lines, start=1):
            # A reported outcome (a bare return or a result dict); a classifier's (outcome, payload) tuple
            # only names the class and is reported through the helper by its caller.
            if re.search(r'return\s+"uncertain"\s*$|"outcome":\s*"uncertain"\}', line):
                inside_helper = any(lines[i].startswith("def hold_uncertain") for i in range(max(0, number - 6), number))
                if not inside_helper:
                    offenders.append(path.name + ":" + str(number))
    assert offenders == []


# 3: pinned_env makes a relative YGG_PROFILE absolute, so sessions keep the profile's pause dir.
def test_pinned_env_makes_profile_absolute(world, monkeypatch):
    from yggdrasil import launcher
    (world / "profile.yaml").write_text("name: test\n")
    monkeypatch.chdir(world)
    monkeypatch.setenv("YGG_PROFILE", "profile.yaml")
    clear_caches()
    assert launcher.pinned_env()["YGG_PROFILE"] == str((world / "profile.yaml").resolve())
