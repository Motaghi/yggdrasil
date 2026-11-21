"""The retry cap on failing commands (A11 AC-23, AC-78, AC-79; lane 1).

A command's identity is its normalised form (the guard's `segments()`, which unwraps `env`, `nice`, `timeout`
and `sh -c`, words joined by single spaces) plus the payload's `cwd`. A PostToolUseFailure call counts one
failure (an interruption doesn't); a PostToolUse call of the same command resets the count. The count lives
per activation, so a new activation starts at zero. Once the count reaches `command_retry_limit`, the guard
refuses the command and escalates once to the project's PM, with the command redacted.

Only with the `retry_cap` release switch on; off, nothing is counted and nothing is refused. The reset on a
PM message to the node follows the `retry_pm_reset` switch (AC-79).
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from . import state, util

_SAFE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def enabled(name: str = "retry_cap") -> bool:
    from . import release
    try:
        return release.on(name)
    except ValueError:
        return False


def normalised(command: str) -> str | None:
    """The command's normalised form, or None when the guard can't read it."""
    from . import hooks
    found = hooks.segments(command)
    if not found or any(parts == hooks.UNRESOLVED for parts in found):
        return None
    pieces = [" ".join(parts) for parts in found if parts and parts[0] != "__assign__"]
    return " ; ".join(pieces) if pieces else None


def _key(command: str, cwd: str) -> str | None:
    text = normalised(command)
    if text is None:
        return None
    folder = os.path.realpath(cwd) if cwd else ""
    return hashlib.sha256((text + "\0" + folder).encode("utf-8")).hexdigest()


def _path(activation: str):
    return state.root() / "retries" / (activation + ".json")


def _load(activation: str) -> dict:
    data = util.read_json(_path(activation), {})
    return data if isinstance(data, dict) else {}


def _pm_messages(node: str, pm: str | None) -> int:
    if not pm:
        return 0
    try:
        return sum(1 for item in state.inbox(node) if isinstance(item, dict) and item.get("from") == pm)
    except (OSError, ValueError):
        return 0


def _pm_of(info: dict) -> str | None:
    project = info.get("project")
    if not project:
        return None
    try:
        from . import tracker
        return (tracker.project(str(project)) or {}).get("pm")
    except (util.YggError, OSError, ValueError):
        return None


def record(event: dict) -> None:
    """The `retry_cap` post-event handler (posttool.HANDLERS): count a failure, reset on a success."""
    if not enabled():
        return
    node, activation = event.get("node"), event.get("activation")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    command = str(tool_input.get("command") or "")
    if not node or not activation or not command or not _SAFE.match(str(activation)):
        return
    key = _key(command, str(event.get("cwd") or ""))
    if key is None:
        return
    with util.locked():
        data = _load(str(activation))
        entry = data.get(key) or {}
        if event.get("event") == "PostToolUse":
            if not entry:
                return
            data.pop(key, None)
        else:
            if event.get("interrupted"):
                return
            info = state.all_nodes().get(str(node)) or {}
            exit_code = event.get("exit_code")
            entry = {"count": int(entry.get("count") or 0) + 1, "escalated": bool(entry.get("escalated")),
                     "command": util.redact(normalised(command) or ""),
                     "error": "Exit code " + str(exit_code) if exit_code is not None else "failed",
                     "pm_messages": _pm_messages(str(node), _pm_of(info))}
            data[key] = entry
        util.atomic_write(_path(str(activation)), json.dumps(data, indent=2))


def check(node: str, activation: str | None, command: str, cwd: str, info: dict) -> str | None:
    """The guard's refusal once the command has failed `command_retry_limit` times in this activation; the
    first refusal sends the PM one message naming the command and its last exit."""
    if not enabled() or not activation or not _SAFE.match(str(activation)):
        return None
    key = _key(command, cwd)
    if key is None:
        return None
    data = _load(activation)
    entry = data.get(key)
    if not entry:
        return None
    from . import config
    limit = int(config.get("command_retry_limit") or 3)
    count = int(entry.get("count") or 0)
    if count < limit:
        return None
    pm = _pm_of(info)
    if enabled("retry_pm_reset") and _pm_messages(node, pm) > int(entry.get("pm_messages") or 0):
        with util.locked():
            data = _load(activation)
            data.pop(key, None)
            util.atomic_write(_path(activation), json.dumps(data, indent=2))
        return None
    shown = str(entry.get("command") or "")
    if not entry.get("escalated") and pm and pm != node:
        from . import ratatoskr
        body = (node + ": " + shown + " failed " + str(count) + " times (" + str(entry.get("error")) +
                "); it is refused until you reply or it succeeds.")
        try:
            ratatoskr.send(node, pm, "issue", body, internal=True)
        except util.YggError:
            pass
        else:
            with util.locked():
                data = _load(activation)
                if key in data:
                    data[key]["escalated"] = True
                    data[key]["pm_messages"] = _pm_messages(node, pm)
                    util.atomic_write(_path(activation), json.dumps(data, indent=2))
    return shown + " failed " + str(count) + " times; tell your PM"
