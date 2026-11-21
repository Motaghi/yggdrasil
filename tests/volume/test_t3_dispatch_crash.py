"""A T3 call that outlives its dispatcher holds recovery: no second thread is ever started for one activation."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from driver import Driver

pytestmark = pytest.mark.skipif(os.environ.get("ACCEPTANCE_TARGET", "real") != "real", reason="real target only")

# Inserted into the driver's copy of the fake t3: the first start of pm1tl1 records its pid and waits for a
# release file, like a call T3 is slow to answer.
DELAY = '''    if argv[:2] == ["thread", "start"] and "--title" in argv and argv[argv.index("--title") + 1].startswith("pm1tl1 "):
        barrier = Path(__file__).parent.parent / "first-start.json"
        try:
            descriptor = os.open(barrier, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            descriptor = None
        if descriptor is not None:
            with os.fdopen(descriptor, "w") as handle:
                json.dump({"pid": os.getpid(), "title": argv[argv.index("--title") + 1]}, handle)
            import time
            deadline = time.monotonic() + 60
            while not (barrier.parent / "release-first").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
'''


def _wait(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_t3_call_that_outlives_its_dispatcher_is_waited_for_never_repeated(tmp_path):
    with Driver(tmp_path) as driver:
        config = tmp_path / "fast.yaml"
        config.write_text("settle_delay_seconds: 0\nlaunch_confirm_seconds: 0.1\n")
        driver.env["YGG_CONFIG"] = str(config)
        driver.core_tree()
        driver.a5_t3(cli_version="0.1.9", server_version="0.1.9")
        driver.ready()
        binary = tmp_path / "bin" / "t3"
        marker = '    config_path = Path(os.environ["YGG_FAKE_CONFIG"])'
        binary.write_text(binary.read_text().replace(marker, DELAY + marker))
        barrier, release = tmp_path / "first-start.json", tmp_path / "release-first"
        launcher = subprocess.Popen([driver.cli, "launch", "pm1tl1"], cwd=driver.cwd, env=driver.env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            assert _wait(barrier.exists, 20), "the first t3 call never started"
            first = json.loads(barrier.read_text())
            launcher.kill()  # the dispatcher dies; its t3 call is still in flight
            launcher.wait(timeout=5)
            assert _alive(first["pid"])
            assert driver.run("conductor", "tick").returncode == 0
            threads = driver.json_file(tmp_path / "t3-state.json")["threads"]
            assert [t for t in threads.values() if t["title"] == first["title"]] == []  # held: no retry
            assert driver.a5_t3_calls("thread start", "pm1tl1") == []  # (the fake logs a call once released)
            release.touch()
            assert _wait(lambda: not _alive(first["pid"]), 15)
            assert driver.run("conductor", "tick").returncode == 0
            threads = driver.json_file(tmp_path / "t3-state.json")["threads"]
            matches = [key for key, t in threads.items() if t["title"] == first["title"]]
            assert len(matches) == 1, matches
            assert len(driver.a5_t3_calls("thread start", "pm1tl1")) == 1
            receipt = driver.a5_receipts("pm1tl1")[0]
            assert receipt.get("t3_thread") == matches[0] and not receipt.get("t3_pending")
        finally:
            release.touch()
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait(timeout=5)
