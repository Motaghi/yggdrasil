"""`ygg hook posttool`: the PostToolUse and PostToolUseFailure entry point (A11 test conventions, "Post events").

Claude Code sends the same payload as PreToolUse (`cwd`, `tool_use_id`, `session_id`, `permission_mode`,
`transcript_path`, `tool_name`, `tool_input`) plus, for PostToolUse, `tool_response` with the keys
`interrupted`, `isImage`, `noOutputExpected`, `stderr` and `stdout` (no exit status: the call succeeded), or,
for PostToolUseFailure, `error` ("Exit code N") and `is_interrupt`. A call the permission layer refuses after
the guard allowed it sends neither event.

`parse` turns the payload into one event dict; every handler slot below gets it, in order. A post event can't
block anything, so the entry point always exits 0; a handler's refusal is swallowed (seams.run_steps).
"""

from __future__ import annotations

import json
import re
import sys

from . import seams, util

SUCCESS, FAILURE = "PostToolUse", "PostToolUseFailure"
_EXIT = re.compile(r"Exit code (-?\d+)")

# EXTENSION POINT (A11 seam): post-event handlers, in order. Each lane fills only its own slot with a
# "module:function" reference; the function takes the event dict from `parse`.
HANDLERS = (
    ("busy", "busy:on_post"),  # clear the in-flight call by (activation, tool_use_id); refresh the heartbeat
                           #         (AC-02, AC-39, AC-40)
    ("retry_cap", "retrycap:record"),  # count failures of the same command and reset on success (AC-23, AC-78);
                           #         only when release.on("retry_cap")
)


def parse(text: str) -> dict:
    """One post event: the payload's fields plus `event` (PostToolUse or PostToolUseFailure), `exit_code`
    (0 on success, N from "Exit code N", None when the error names no code), `interrupted`, and the caller's
    node and activation from the environment."""
    payload = json.loads(text or "{}")
    if not isinstance(payload, dict):
        raise ValueError("post-event payload is not an object")
    failed = "error" in payload or payload.get("hook_event_name") == FAILURE
    event = dict(payload)
    event["event"] = FAILURE if failed else SUCCESS
    if failed:
        match = _EXIT.search(str(payload.get("error") or ""))
        event["exit_code"] = int(match.group(1)) if match else None
        event["interrupted"] = bool(payload.get("is_interrupt"))
    else:
        response = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
        event["exit_code"] = 0
        event["interrupted"] = bool(response.get("interrupted"))
    event["node"] = util.env("YGG_NODE")
    event["activation"] = util.env("YGG_ACTIVATION")
    return event


def handle(event: dict) -> None:
    seams.run_steps(HANDLERS, event)


def main_posttool() -> int:
    try:
        handle(parse(sys.stdin.read()))
    except (ValueError, util.YggError):
        pass  # a post event never blocks the agent
    return 0
