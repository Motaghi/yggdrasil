"""Process start identity is the same whatever the reader's timezone or locale."""

import os
import subprocess
import sys

import pytest

from yggdrasil import util

ZONES = ("Europe/Copenhagen", "UTC", "America/Los_Angeles")


@pytest.fixture
def child():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield process.pid
    process.kill()
    process.wait()


def _run(code: str, zone: str, *args) -> str:
    env = dict(os.environ, TZ=zone, LC_ALL="de_DE.UTF-8", LANG="de_DE.UTF-8")
    result = subprocess.run([sys.executable, "-c", code, *map(str, args)], capture_output=True, text=True,
                            env=env, check=True)
    return result.stdout.strip()


PROBE = "import sys; from yggdrasil import util; print(util.process_start(int(sys.argv[1])))"
EVIDENCE = ("import sys; from yggdrasil import lifecycle; "
            "print(lifecycle.evidence(int(sys.argv[1]), sys.argv[2]))")


def _legacy_lstart(pid: int, zone: str) -> str:
    """A start time as older versions recorded it: `ps -o lstart` in the recording process's timezone."""
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                         env=dict(os.environ, TZ=zone, LC_ALL="C", LANG="C"), check=True).stdout
    return " ".join(out.split())


def test_the_probe_gives_one_identity_in_every_timezone(child):
    seen = {zone: _run(PROBE, zone, child) for zone in ZONES}
    assert len(set(seen.values())) == 1, seen
    assert seen["UTC"] not in ("", "None")


def test_a_receipt_written_under_utc_reads_alive_under_cet(child):
    recorded = _legacy_lstart(child, "UTC")
    assert _run(EVIDENCE, "Europe/Copenhagen", child, recorded) == "alive"
    assert _run(EVIDENCE, "UTC", child, recorded) == "alive"


def _machine_zone():
    try:
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return None


@pytest.mark.skipif(_machine_zone() is None, reason="the machine's timezone can't be read")
def test_a_receipt_written_in_the_machines_local_time_still_reads_alive(child):
    recorded = _legacy_lstart(child, _machine_zone())
    assert _run(EVIDENCE, "UTC", child, recorded) == "alive"
    assert _run(EVIDENCE, "Europe/Copenhagen", child, recorded) == "alive"


def test_a_current_identity_reads_alive_in_every_timezone(child):
    recorded = _run(PROBE, "UTC", child)
    for zone in ZONES:
        assert _run(EVIDENCE, zone, child, recorded) == "alive", zone


def test_another_start_time_is_dead(child):
    assert _run(EVIDENCE, "Europe/Copenhagen", child, "Mon Jan  1 00:00:00 2024") == "dead"
    assert _run(EVIDENCE, "UTC", child, "s:1704067200.000000") == "dead"


def test_an_unreadable_record_is_unknown_not_alive(child):
    from yggdrasil import lifecycle
    assert lifecycle.evidence(child, "not a time") == "dead"
    assert lifecycle.evidence(child, None) == "unknown"


def test_start_epochs_agree_with_the_kernel(child):
    identity = util.process_start(child)
    epoch = util.start_epoch(identity)
    assert util.start_epoch(_legacy_lstart(child, "UTC")) == int(epoch)
    if _machine_zone():
        assert int(epoch) in util.start_epochs(_legacy_lstart(child, _machine_zone()))
