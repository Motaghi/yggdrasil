"""The sign-in lease of a Codex agent whose supervisor died.

A supervisor holds the shared sign-in lease on the login folder until its Codex exits; when the supervisor dies
first, the lease goes with it. Recovery that finds the agent still alive starts this small process, which claims
the activation's holder lock (so only one holder runs per activation), takes the lease again on exactly the
login folder recorded at launch, and keeps it until the agent's process and process group are gone. While a
logout holds the lease exclusively, it keeps trying, so the lease is back as soon as that logout lets go.

Run as `python -I -m yggdrasil.codexlease <holder lock> <login folder> <pid> <process start>`. It reads no
configuration: every path it uses is given on its command line.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

POLL_SECONDS = 1.0


def _gone(pid: int, started: str) -> bool:
    from . import interlock, lifecycle, util
    dead = not util.pid_alive(pid) or lifecycle.evidence(pid, started) == "dead"
    return dead and not interlock.group_alive(pid)


def hold(lock: Path, login: Path, pid: int, started: str) -> int:
    from . import codexlaunch
    claim = codexlaunch.claim_holder(lock)
    if claim is None:
        return 0  # the supervisor or another holder keeps this activation's lease
    lease = None
    try:
        while not _gone(pid, started):
            if lease is None:
                lease = codexlaunch.take_lease_at(login, 0)
            time.sleep(POLL_SECONDS)
    finally:
        if lease is not None:
            os.close(lease)
        os.close(claim)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        return 1
    try:
        pid = int(argv[3])
    except ValueError:
        return 1
    return hold(Path(argv[1]), Path(argv[2]), pid, argv[4])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
