"""v1.1.1 supervisor loop: each turn sleeps 0.1 s, also for an agent with no relay to answer.

A supervisor without a relay server slept 0.2 s per turn, so an agent's exit was recorded (and its runtime set
idle) up to 0.2 s after the agent had gone, twice the intended bound and slower than v1.1's blocking wait. The
frozen crash-recovery suite's acknowledgement sweep is sensitive to exactly that delay.
"""

import signal
import subprocess
import sys

from test_round2 import world  # noqa: F401  (fixture)


def test_supervisor_without_relay_turns_every_tenth_of_a_second(world, monkeypatch):
    from yggdrasil import launcher, tracker
    activation = tracker.allocate("activation")
    data = {"node": "tl1", "activation": activation, "launcher": "headless", "outcome": "confirmed",
            "phase": "running"}
    launcher.save_receipt(activation, data)
    assert not data.get("relay")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"],
                               stdin=subprocess.PIPE, text=True, start_new_session=True)
    sleeps = []
    real_sleep = launcher.time.sleep

    def recording(seconds):
        sleeps.append(seconds)
        real_sleep(seconds)

    monkeypatch.setattr(launcher.time, "sleep", recording)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        code = launcher._supervise_loop(activation, process, "prompt", data, {})
    finally:
        signal.signal(signal.SIGTERM, previous)
        if process.poll() is None:
            process.kill()
            process.wait()
    assert code == 0
    assert sleeps, "the agent ran for 0.5 s, so the loop turned at least once"
    assert max(sleeps) <= 0.1, sleeps
