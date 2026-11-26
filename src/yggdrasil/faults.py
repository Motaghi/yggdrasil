"""Fault injection for the crash harness (Amendment A7.2, test mode only).

`YGG_FAULT_AT=write:<k>` ends the process with exit 97 (`os._exit`, so no cleanup handler runs) right after
its k-th durable write; `YGG_FAULT_AT=boundary:<name>` ends it at a named boundary. `YGG_FAULT_TRACE=<path>`
appends one line per durable write (`write <k> <relative path>`) and per boundary reached (`boundary
<name>`). All of it is inert unless `YGG_TEST_MODE=1` and neither `YGG_NODE` nor `YGG_ACTIVATION` is set (the
A1.12 override rule); otherwise the variables are ignored silently.
"""

from __future__ import annotations

import os
from pathlib import Path

BOUNDARIES = ("before-spawn", "after-spawn", "after-exit", "before-signal")
# The relay's own supervisor boundaries (v1.1.1). Kept out of BOUNDARIES, whose listing frozen A7 pins exactly.
RELAY_BOUNDARIES = ("relay-after-take", "relay-after-run")
EXIT_FAULT = 97

_writes = 0
# The supervisor's own controls (A8): set by `supervisor_setup` in the supervisor process only.
_SUP: dict = {"trace": None, "plan": None, "spec": None}


def active() -> bool:
    return os.environ.get("YGG_TEST_MODE") == "1" and not os.environ.get("YGG_NODE") and \
        not os.environ.get("YGG_ACTIVATION")


def _plan() -> tuple[str, str] | None:
    value = os.environ.get("YGG_FAULT_AT") or ""
    kind, _, target = value.partition(":")
    if kind in ("write", "boundary") and target:
        return kind, target
    return None


def _relative(path) -> str:
    """A written path relative to the state root or the tracker root (absolute when outside both)."""
    resolved = Path(path).resolve()
    for variable in ("YGG_STATE_ROOT", "YGG_FILES_ROOT"):
        root = os.environ.get(variable)
        if root:
            base = Path(root).resolve()
            if resolved == base or resolved.is_relative_to(base):
                return str(resolved.relative_to(base))
    try:
        from . import state, tracker
        for base in (state.root().resolve(), tracker.root().resolve()):
            if resolved.is_relative_to(base):
                return str(resolved.relative_to(base))
        # The safety folder sits beside the state root: its files are named relative to the state root.
        return os.path.relpath(resolved, state.root().resolve())
    except Exception:  # noqa: BLE001  (tracing must never fail a write)
        pass
    return str(resolved)


def _trace(line: str) -> None:
    target = os.environ.get("YGG_FAULT_TRACE")
    if not target:
        return
    with open(target, "a") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _die() -> None:
    os._exit(EXIT_FAULT)


def _sup_line(line: str) -> None:
    target = _SUP.get("trace")
    if not target:
        return
    with open(target, "a") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _sup_die() -> None:
    _sup_line("fault " + str(_SUP.get("spec")))
    os._exit(EXIT_FAULT)


def after_write(path) -> None:
    """Called by the write primitives once a durable write to the state root or tracker has completed."""
    global _writes
    if not active():
        return
    _writes += 1
    _trace("write " + str(_writes) + " " + _relative(path))
    _sup_line("write " + str(_writes) + " " + _relative(path))
    plan = _plan()
    if plan and plan[0] == "write" and plan[1].isdigit() and int(plan[1]) == _writes:
        _die()
    sup = _SUP.get("plan")
    if sup and sup[0] == "write" and sup[1].isdigit() and int(sup[1]) == _writes:
        _sup_die()


def boundary(name: str) -> None:
    """A named boundary of A7.2 was reached."""
    if not active():
        return
    _trace("boundary " + name)
    _sup_line("boundary " + name)
    plan = _plan()
    if plan and plan[0] == "boundary" and plan[1] == name:
        _die()
    sup = _SUP.get("plan")
    if sup and sup[0] == "boundary" and sup[1] == name:
        _sup_die()


def supervisor_setup(node: str, activation: str) -> None:
    """In the supervisor process: honour `YGG_SUPERVISOR_FAULT_AT=<node>:write:<k>|boundary:<name>` and
    `YGG_SUPERVISOR_TRACE_DIR` for this activation's node only (A8.2)."""
    if not active():
        return
    spec = os.environ.get("YGG_SUPERVISOR_FAULT_AT") or ""
    target, _, rest = spec.partition(":")
    kind, _, value = rest.partition(":")
    if target == node and kind in ("write", "boundary") and value:
        _SUP.update(plan=(kind, value), spec=spec)
    elif target in ("write", "boundary") and rest and ":" not in rest:
        # v1.1.1 rows set the plan without a node (`boundary:relay-after-run`): it applies to every supervisor.
        _SUP.update(plan=(target, rest), spec=spec)
    directory = os.environ.get("YGG_SUPERVISOR_TRACE_DIR")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        _SUP["trace"] = str(Path(directory) / (activation + ".txt"))
        _sup_line("start " + node + " " + str(os.getpid()))


def supervisor_done(code: int) -> None:
    _sup_line("done " + str(code))


def barrier(name: str, node: str, activation: str) -> None:
    """A8.3: only the supervisor of `<node>` stops here, holding no lock unless the caller holds one."""
    if not active():
        return
    if (os.environ.get("YGG_TEST_BARRIER") or "") != node + ":" + name:
        return
    directory = os.environ.get("YGG_TEST_BARRIER_DIR")
    if not directory:
        return
    import time
    base = Path(directory)
    base.mkdir(parents=True, exist_ok=True)
    reached = base / (activation + "." + name + ".reached")
    with open(reached, "w") as handle:
        handle.write(str(os.getpid()) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    deadline = time.monotonic() + 120
    while not (base / (activation + "." + name + ".continue")).exists():
        if time.monotonic() > deadline:
            os._exit(EXIT_FAULT)
        time.sleep(0.05)


def probe_unknown() -> bool:
    """A8.4: `YGG_TEST_PROBE=unknown` makes every process probe of this command answer unknown."""
    return active() and os.environ.get("YGG_TEST_PROBE") == "unknown"


def child_env(environment: dict) -> dict:
    """The environment of a ygg child process the harness did not start itself (the supervisor). Writes are
    counted per process and the trace holds only the started process's writes, so the child gets no trace
    and no write fault; it still honours its own boundary faults (after-spawn, after-exit)."""
    environment = dict(environment)
    environment.pop("YGG_FAULT_TRACE", None)
    environment.pop("YGG_TEST_PROBE", None)
    if not str(environment.get("YGG_FAULT_AT") or "").startswith("boundary:"):
        environment.pop("YGG_FAULT_AT", None)
    return environment


def listing() -> str:
    return "".join(name + "\n" for name in BOUNDARIES)
