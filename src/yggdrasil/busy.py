"""Busy: an agent inside a long command (A11 AC-02, AC-39, AC-40; lane 2).

The evidence is an in-flight tool call, keyed by activation and `tool_use_id`, that the guard records once all its
checks allow the call (lane 1, `<state root>/inflight/<activation>/<tool_use_id>.json`). A PostToolUse or
PostToolUseFailure call with the same `tool_use_id` clears it, and both post events refresh the heartbeat. Busy is a
display word only: the runtime state stays `running`. A record older than `busy_max_minutes` no longer counts, so the
stall rules apply again. A Codex reviewer has no hooks; its heartbeat is the newest modification time of its output
files (`<state root>/output/<activation>/`).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from . import config, state, util

_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_ACTIVATION = re.compile(r"act-[0-9]{1,12}")


def inflight_dir(activation: str) -> Path:
    return state.root() / "inflight" / activation


def records(activation: str | None) -> list[dict]:
    """The activation's in-flight tool calls (allowed by the guard, no post event yet), oldest first."""
    if not activation or not _ACTIVATION.fullmatch(str(activation)):
        return []
    folder = inflight_dir(activation)
    found = []
    if folder.is_dir():
        for path in folder.glob("*.json"):
            data = util.read_json(path, None)
            if isinstance(data, dict):
                found.append(data)
    return sorted(found, key=lambda r: str(r.get("started_at") or ""))


def _age(record: dict, moment) -> float | None:
    try:
        return (moment - util.parse_time(str(record.get("started_at")))).total_seconds()
    except (ValueError, TypeError):
        return None


def current(activation: str | None, moment=None) -> dict | None:
    """The newest in-flight call still inside `busy_max_minutes`, or None."""
    moment = moment or util.now()
    ceiling = float(config.get("busy_max_minutes", 60)) * 60
    live = [r for r in records(activation) if (age := _age(r, moment)) is not None and age <= ceiling]
    return live[-1] if live else None


def word(record: dict) -> str:
    """`busy (running <program>)`: the program of the call, never its arguments."""
    command = str(record.get("command") or "")
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    while parts and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", parts[0]):
        parts = parts[1:]
    program = Path(parts[0]).name if parts else str(record.get("tool_name") or "a tool")
    return "busy (running " + program + ")"


def on_post(event: dict) -> None:
    """posttool.HANDLERS["busy"]: clear the call by (activation, tool_use_id) and refresh the heartbeat."""
    node = event.get("node")
    activation = event.get("activation")
    tool_use_id = str(event.get("tool_use_id") or "")
    if activation and _ACTIVATION.fullmatch(str(activation)) and _ID.fullmatch(tool_use_id):
        from . import hooks
        clear = getattr(hooks, "clear_inflight", None)  # the hooks module's record (same path as inflight_dir)
        if clear is not None:
            clear(str(activation), tool_use_id)
        else:
            try:
                (inflight_dir(activation) / (tool_use_id + ".json")).unlink()
            except OSError:
                pass
    if node and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(node)):
        try:
            util.atomic_write(state.root() / "heartbeats" / str(node), util.iso() + "\n")
        except OSError:
            pass


def output_heartbeat(activation: str | None):
    """A Codex agent's heartbeat: the newest modification time of its output files, or None."""
    if not activation or not _ACTIVATION.fullmatch(str(activation)):
        return None
    folder = state.root() / "output" / activation
    newest = None
    # Judged on the same clock as everything else (YGG_NOW in test mode): a time after that clock's now is
    # clock skew, not activity, so it never counts as a heartbeat (AC-39 row 3).
    limit = util.now().timestamp() + 5
    if folder.is_dir():
        for path in folder.iterdir():
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
            if stamp > limit:
                continue
            newest = stamp if newest is None else max(newest, stamp)
    if newest is None:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(newest, timezone.utc)


def ceiling_step() -> None:
    """conductor.BEFORE_LAUNCH_STEPS["busy_ceiling"]: drop records past `busy_max_minutes`, so the stall rules
    apply again (the liveness pass already ignores them; this keeps the folder small)."""
    moment = util.now()
    ceiling = float(config.get("busy_max_minutes", 60)) * 60
    base = state.root() / "inflight"
    if not base.is_dir():
        return
    for folder in base.iterdir():
        if not folder.is_dir():
            continue
        for path in folder.glob("*.json"):
            data = util.read_json(path, None)
            age = _age(data, moment) if isinstance(data, dict) else None
            if age is not None and age > ceiling * 4:
                try:
                    path.unlink()
                except OSError:
                    pass

