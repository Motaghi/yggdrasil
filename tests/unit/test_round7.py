"""Unit tests for the round-7 review findings (dev #2 pair review and QA)."""

import shutil
import subprocess

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (fixture and helpers)
from test_round3 import receipt_file


def dead_pid():
    done = subprocess.Popen(["true"])
    done.wait()
    return done.pid


# 1: a dead pending activation with no exit record becomes uncertain after the grace, alerted once.
def test_dead_pending_activation_becomes_uncertain_after_grace(world, monkeypatch):
    from yggdrasil import conductor, launcher, state
    receipt_file("act-0042", outcome="pending", phase="starting", pid=dead_pid(), process_started_at="gone",
                 reserved_at="2026-11-16T01:00:00Z", started_at="2026-11-16T01:00:00Z")
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "pending"})
    conductor.reconcile()
    conductor.reconcile()
    assert launcher.receipt("act-0042")["outcome"] == "uncertain"
    assert state.runtime("tl1")["outcome"] == "uncertain"
    assert len([a for a in state.alerts() if a["kind"] == "ygg-uncertain"]) == 1


# 2: a confirmed receipt whose process is gone without an exit record is uncertain, never failed.
def test_confirmed_receipt_without_exit_record_is_uncertain(world):
    from yggdrasil import conductor, launcher, state
    receipt_file("act-0042", outcome="confirmed", phase="launched", pid=dead_pid(), process_started_at="gone")
    assert launcher.receipt_outcome(launcher.receipt("act-0042")) == "uncertain"
    assert launcher.finalize("tl1", "act-0042", "failed") == "uncertain"
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "pending"})
    conductor.reconcile()
    assert state.runtime("tl1")["outcome"] == "uncertain"
    assert launcher.receipt("act-0042")["outcome"] == "uncertain"


# 4: a relative platform_pause_dir resolves against the configuration file, not the working directory.
def test_relative_platform_pause_dir_resolves_against_config(world, monkeypatch, tmp_path):
    from yggdrasil import state
    folder = tmp_path / "configs"
    (folder / "platform").mkdir(parents=True)
    (folder / "config.yaml").write_text("platform_pause_dir: platform\n")
    monkeypatch.setenv("YGG_CONFIG", str(folder / "config.yaml"))
    monkeypatch.delenv("YGG_PLATFORM_STATE", raising=False)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "platform").mkdir(parents=True)
    (elsewhere / "platform" / "pause").write_text("")
    monkeypatch.chdir(elsewhere)
    clear_caches()
    assert not state.platform_paused()
    (folder / "platform" / "pause").write_text("")
    assert state.platform_paused()
