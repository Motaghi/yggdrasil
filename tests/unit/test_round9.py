"""Unit tests for the round-9 review findings (dev #2 pair review)."""

import os
import subprocess

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (fixture and helpers)
from test_round3 import receipt_file


def dead_pid():
    done = subprocess.Popen(["true"])
    done.wait()
    return done.pid


# 2: hold_uncertain never overwrites a terminal receipt and reports what the receipt says.
def test_hold_uncertain_reports_terminal_receipts_unchanged(world):
    from yggdrasil import launcher, state
    receipt_file("act-0042", outcome="confirmed", phase="launched", pid=999999, exited_at=util.iso(), exit_code=0)
    state.save_runtime("tl1", {"state": "idle", "activation": "act-0042", "outcome": "confirmed"})
    assert launcher.hold_uncertain("tl1", "act-0042", "probe") == "confirmed"
    assert launcher.receipt("act-0042")["outcome"] == "confirmed" and state.runtime("tl1")["state"] == "idle"
    receipt_file("act-0043", outcome="aborted", phase="aborted", pid=None, exited_at=util.iso())
    assert launcher.hold_uncertain("tl1", "act-0043", "probe") == "aborted"
    assert launcher.receipt("act-0043")["outcome"] == "aborted"
    assert not [a for a in state.alerts() if a["kind"] == "ygg-uncertain"]


# 2: confirm decides under the lock from the receipt as it is now; a concurrent resolution is reported.
@pytest.mark.parametrize("resolved", ["exit-0", "aborted"])
def test_confirm_reports_a_concurrent_resolution(world, resolved):
    from yggdrasil import launcher, state
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.1\n")
    clear_caches()
    if resolved == "exit-0":
        receipt_file("act-0042", outcome="confirmed", phase="launched", pid=dead_pid(), process_started_at="gone",
                     exited_at=util.iso(), exit_code=0)
        expected = "confirmed"
    else:
        receipt_file("act-0042", outcome="aborted", phase="aborted", pid=None, exited_at=util.iso())
        expected = "aborted"
    state.save_runtime("tl1", {"state": "idle", "activation": "act-0042", "outcome": "confirmed"})
    assert launcher.confirm("tl1", "act-0042")["outcome"] == expected
    assert launcher.receipt("act-0042")["outcome"] == ("confirmed" if expected == "confirmed" else "aborted")
    assert state.runtime("tl1")["state"] == "idle"
    assert not [a for a in state.alerts() if a["kind"] == "ygg-uncertain"]


# 3: a symlinked profile is pinned absolute but not dereferenced.
def test_pinned_profile_keeps_the_symlink_path(world, monkeypatch, tmp_path):
    from yggdrasil import launcher
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "p.yaml").write_text("platform_pause_dir: state\n")
    (tmp_path / "cfg").mkdir()
    os.symlink(tmp_path / "profiles" / "p.yaml", tmp_path / "cfg" / "active.yaml")
    monkeypatch.chdir(tmp_path / "cfg")
    monkeypatch.setenv("YGG_PROFILE", "active.yaml")
    clear_caches()
    pinned = launcher.pinned_env()["YGG_PROFILE"]
    assert pinned == os.path.join(os.path.abspath(str(tmp_path / "cfg")), "active.yaml")
    assert os.path.islink(pinned)
