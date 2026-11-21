"""Alert delivery (A11 lane 4: AC-28, AC-86, AC-87).

Every alert is written to `alerts.jsonl` by `state.alert`. With `alert_forwarder: auto`, each class-`user`
alert is also delivered at least once, from the conductor's `alert_delivery` step:

- to the main session's T3 thread (`alert_t3_thread`, sent with the `t3` CLI), or, when that send fails or
  times out, or when no thread is set, as a macOS notification through `osascript`;
- to the profile's `alert_command`, when set, with the alert JSON on stdin (its own channel).

`<state root>/alerts-delivered.jsonl` holds one line per alert, channel and step: `pending` when the channel
is queued, `sent` just before its command starts, then `delivered` (exit 0 within
`alert_send_timeout_seconds`) or `failed`. A channel left at `sent` with no outcome (a crash in between) is
sent again. An alert whose T3 and macOS channels both failed stays pending and is retried from the start, one
tick (`tick_seconds`) after the first failure, the gap doubling up to 30 minutes; a retry skips a channel
already delivered. After `alert_max_attempts` tries the alert is recorded `abandoned` and one class-`log`
`ygg-alert-undelivered` is raised. One tick spends at most `alert_tick_budget_seconds` sending.
`alert_forwarder: jsonl` keeps alerts log-only and writes nothing. A11.1 (AC-09, AC-10): a T3 send that
times out is `timed-out` and never sent to T3 again; `alert_macos: off` drops macOS as a channel. A journal
that can't be read stops delivery (one class-`log` alert, a `ygg status` line) until it is repaired.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import time
from datetime import timedelta
from pathlib import Path

from . import alertkinds, config, state, util

USER_CHANNELS = ("t3", "osascript")
ALERT = "alert"  # alert-level records: `pending` with the next try's due time, or `abandoned`
MAX_GAP_SECONDS = 30 * 60
CORRUPT = "alert delivery is stopped: alerts-delivered.jsonl can't be read; ask your main session to repair it"
DONE_STEPS = ("delivered", "failed", "timed-out")


def delivered_path() -> Path:
    return state.root() / "alerts-delivered.jsonl"


def cursor_path() -> Path:
    return state.root() / "alerts-delivery-cursor.json"


def records() -> list[dict]:
    """The delivery journal. A line that doesn't parse (a torn final append included) raises, so delivery
    stops instead of forgetting a delivery or a T3 timeout and sending again (A7 I13, A11.1 AC-09)."""
    return util.read_lines(delivered_path())


def _report_corrupt(error: util.YggError) -> None:
    """One class-`log` alert per corrupt episode; `ygg status` shows the stop line until it is repaired."""
    marker = state.root() / "alerts-delivered.corrupt"
    if marker.exists():
        return
    util.atomic_write(marker, util.iso() + "\n")
    state.alert("ygg-corrupt-evidence", "Alert delivery stopped: " + error.message, path=str(delivered_path()))


def _record(alert: dict, channel: str, step: str, attempt: int, **fields) -> None:
    entry = {"ts": util.iso(), "alert": alert["id"], "kind": alert.get("kind"), "channel": channel,
             "step": step, "attempt": attempt}
    entry.update(fields)
    util.append_line(delivered_path(), entry)


def text_of(alert: dict) -> str:
    """The text a user channel carries: the alert's message, then its next step (AC-87 row 3)."""
    message = str(alert.get("message") or alert.get("kind") or "").strip()
    step = alert.get("next_step") or alertkinds.next_step(str(alert.get("kind")))
    if step and not message.endswith(step):
        message = (message.rstrip(".") + ". " if message else "") + step
    return message


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_on() -> bool:
    """`alert_macos` (A11.1 AC-10): `off` on a host where no one sits at the screen, so macOS is no channel."""
    value = config.get("alert_macos", "on")
    if value is False:
        return False
    return str(value).strip().lower() not in ("off", "false", "no", "0")


def command_argv() -> list[str] | None:
    command = config.get("alert_command")
    if isinstance(command, list) and command:
        return [str(part) for part in command]
    return None


def command_channel() -> str | None:
    """The phone command's channel name: its program's file name (`phone-push`)."""
    argv = command_argv()
    if not argv:
        return None
    name = Path(argv[0]).name or "command"
    return name if name not in USER_CHANNELS + (ALERT,) else "command-" + name


def _argv(channel: str, alert: dict) -> list[str] | None:
    text = text_of(alert)
    if channel == "t3":
        from . import t3
        binary = t3.binary()
        thread = config.get("alert_t3_thread")
        if not binary or not thread:
            return None
        return [binary, "thread", "send", "--json", "--", str(thread), text]
    if channel == "osascript":
        if not macos_on():
            return None
        return ["osascript", "-e", "display notification " + _applescript_string(text) +
                " with title " + _applescript_string("Yggdrasil")]
    if channel == command_channel():
        return command_argv()
    return None


def _send(argv: list[str], stdin_text: str | None, timeout: float, alert_id: str, started=None) -> str:
    """Run one channel command: `delivered` when it exits 0 within `timeout`, `timed-out` when it is still
    running then (it is killed with its whole process group), else `failed`. The alert id goes in the
    command's environment (`YGG_ALERT_ID`). `started` runs once the command's process exists (or failed to
    start), before any wait: the `sent` record, so a channel seen at `sent` always has a send behind it."""
    environment = dict(os.environ, YGG_ALERT_ID=alert_id)
    deadline = time.monotonic() + max(timeout, 0.1)  # fixed before the spawn: the record never stretches the wait
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                                   start_new_session=True, env=environment)
    except OSError:
        if started:
            started()
        return "failed"
    if started:
        started()
    try:
        left = deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)  # the record used the whole wait: a timeout now
        process.communicate(stdin_text, timeout=left)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return "timed-out"
    except OSError:
        return "failed"
    return "delivered" if process.returncode == 0 else "failed"


class _Budget:
    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds

    def left(self) -> float:
        return self.deadline - time.monotonic()


def _state_of(alert_id: str, rows: list[dict]) -> dict:
    """What the records say about one alert: its tries, each channel's last step in the current try, whether a
    user channel was ever delivered, the channels delivered or timed out on any try, the retry due time and a
    give-up."""
    found = {"attempt": 0, "steps": {}, "user_delivered": False, "delivered": set(), "timed_out": set(),
             "due": None, "due_attempt": 0, "abandoned": False}
    for row in rows:
        if row.get("alert") != alert_id:
            continue
        attempt = int(row.get("attempt") or 0)
        channel, step = row.get("channel"), row.get("step")
        if channel == ALERT:
            if step == "abandoned":
                found["abandoned"] = True
            elif step == "pending":
                found["due"] = row.get("due")  # the alert waits for its next try (A11.1 AC-09 row 2)
                found["due_attempt"] = attempt
            continue
        if attempt > found["attempt"]:
            found["attempt"] = attempt
            found["steps"] = {}
        if attempt == found["attempt"]:
            found["steps"][channel] = step
        if step == "delivered":
            found["delivered"].add(channel)
            if channel in USER_CHANNELS:
                found["user_delivered"] = True
        elif step == "timed-out" and channel == "t3":
            found["timed_out"].add(channel)
    return found


def _gap(failures: int) -> float:
    tick = float(config.get("tick_seconds", 15) or 15)
    return min(tick * (2 ** max(failures - 1, 0)), MAX_GAP_SECONDS)


def _candidates(rows: list[dict]) -> list[dict]:
    """Class-`user` alerts from the cursor on, whatever their age (A11 AC-28): the cursor only ever moves past
    alerts that need nothing more (`_advance_cursor`)."""
    alerts = state.alerts()
    cursor = util.read_json(cursor_path())
    start = int(cursor.get("seen") or 0) if isinstance(cursor, dict) else 0
    found = []
    for alert in alerts[start:]:
        if not alert.get("id"):
            continue
        kind = str(alert.get("kind"))
        cls = alert.get("class") or alertkinds.classify(kind, alert)
        if cls == alertkinds.USER:
            found.append(alert)
    return found


def _abandon(alert: dict, attempt: int) -> None:
    _record(alert, ALERT, "abandoned", attempt)
    state.alert("ygg-alert-undelivered", "Alert " + alert["id"] + " (" + str(alert.get("kind")) +
                ") couldn't be delivered after " + str(attempt) + (" try" if attempt == 1 else " tries"),
                alert=alert["id"], alert_kind=alert.get("kind"))


def _deliver_one(alert: dict, rows: list[dict], budget: _Budget) -> None:
    status = _state_of(alert["id"], rows)
    if status["abandoned"] or status["user_delivered"]:
        return
    steps = status["steps"]
    attempt = status["attempt"] or 1
    if status["attempt"] and status["due_attempt"] == status["attempt"]:
        # The last try ended with every user channel failed: retry from the start once it is due. A try a tick's
        # budget cut short, or a crash left at `sent`, carries on instead.
        if status["due"]:
            try:
                if util.parse_time(str(status["due"])) > util.now():
                    return
            except ValueError:
                pass
        attempt = status["attempt"] + 1
        steps = {}
    timeout = float(config.get("alert_send_timeout_seconds", 120) or 120)
    max_attempts = int(config.get("alert_max_attempts", 10) or 10)
    payload = json.dumps(alert)

    def run(channel: str) -> str | None:
        argv = _argv(channel, alert)
        if argv is None:
            return None
        if steps.get(channel) in DONE_STEPS:
            return steps[channel]
        if budget.left() <= 0:
            return "deferred"
        if steps.get(channel) not in ("pending", "sent"):
            _record(alert, channel, "pending", attempt)
        # `sent` is recorded once the command's process exists, before waiting on it: a crash with the record at
        # `sent` always had a send under way (resent after the restart); a crash before it leaves `pending`, also
        # resent, so delivery stays at least once either way.
        outcome = _send(argv, payload if channel not in USER_CHANNELS else None,
                        min(timeout, max(budget.left(), 0.1)), alert["id"],
                        started=lambda: _record(alert, channel, "sent", attempt))
        if outcome == "timed-out" and channel != "t3":
            outcome = "failed"  # only a T3 send that may have arrived counts as timed out (A11.1 AC-09)
        _record(alert, channel, outcome, attempt)
        steps[channel] = outcome
        return outcome

    phone = command_channel()
    if phone and phone not in status["delivered"] and run(phone) == "deferred":
        return
    user = None
    tried = False
    if "t3" not in status["timed_out"] and _argv("t3", alert) is not None:
        tried = True
        user = run("t3")
        if user == "deferred":
            return
    if user != "delivered" and _argv("osascript", alert) is not None:
        tried = True
        user = run("osascript")
        if user == "deferred":
            return
    if user == "delivered":
        return
    left = ("t3" not in status["timed_out"] and steps.get("t3") != "timed-out" and _argv("t3", alert) is not None) \
        or _argv("osascript", alert) is not None
    if not tried or not left or attempt >= max_attempts:
        _abandon(alert, attempt)
        return
    due = util.now() + timedelta(seconds=_gap(attempt))
    _record(alert, ALERT, "pending", attempt, due=util.iso(due))


def escalate_uncertain() -> None:
    """AC-43 row 3: an uncertain hold raises a class-`log` `ygg-uncertain` when it is first held; still held 300 s
    past its reservation, it raises one more, of class `user` (once per activation)."""
    from . import launcher, lifecycle
    raised = {str(row.get("activation")) for row in state.alerts()
              if row.get("kind") == "ygg-uncertain" and row.get("class") == alertkinds.USER}
    moment = util.now()
    for node_id in state.all_nodes():
        runtime = state.runtime(node_id)
        key = runtime.get("activation")
        if not key or key in raised or runtime.get("state") not in ("running", "stopped"):
            continue
        data = launcher.receipt(key) or {}
        held = runtime.get("outcome") == "uncertain" or data.get("outcome") == "uncertain" or \
            (data.get("exited_at") is None and lifecycle.is_uncertain(node_id, key))
        if not held or data.get("terminal") is not None:
            continue
        reserved = data.get("reserved_at") or data.get("started_at")
        try:
            age = (moment - util.parse_time(str(reserved))).total_seconds() if reserved else None
        except ValueError:
            age = None
        if age is None or age < alertkinds.UNCERTAIN_USER_AFTER_SECONDS:
            continue
        state.alert("ygg-uncertain", "Activation " + key + " of " + node_id + " is still uncertain " +
                    str(int(age)) + " s after its launch; held, not relaunched", node=node_id, activation=key,
                    age_seconds=int(age),
                    next_step="Check the agent is not running, then run `ygg node release " + node_id +
                    " --as aborted` to offer its work again (or --as failed).")


def tick_step() -> None:
    """The conductor's `alert_delivery` step: the uncertain escalation, then delivery. One delivery runs at a
    time (a lock beside the records); a tick that finds it taken leaves delivery to the one that holds it."""
    try:
        escalate_uncertain()
    except util.YggError:
        pass
    if str(config.get("alert_forwarder", "auto")) != "auto":
        return  # jsonl: alerts stay log-only, and nothing is written
    lock_path = state.root() / "alerts-delivery.lock"
    try:
        with open(lock_path, "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                _deliver_all()
            except util.YggError as error:
                if "corrupt store file" not in error.message:
                    raise
                _report_corrupt(error)
                return
            (state.root() / "alerts-delivered.corrupt").unlink(missing_ok=True)
    except OSError:
        return


def _deliver_all() -> None:
    rows = records()
    candidates = _candidates(rows)
    if not candidates:
        return
    budget = _Budget(float(config.get("alert_tick_budget_seconds", 150) or 150))
    for alert in candidates:
        if budget.left() <= 0:
            break
        _deliver_one(alert, rows, budget)
        rows = records()
    _advance_cursor(rows)


def _advance_cursor(rows: list[dict]) -> None:
    """Move the cursor past the leading alerts that need nothing more (log class, delivered or abandoned)."""
    alerts = state.alerts()
    cursor = util.read_json(cursor_path(), {}) or {}
    start = int(cursor.get("seen") or 0)
    index = start
    while index < len(alerts):
        alert = alerts[index]
        cls = alert.get("class") or alertkinds.classify(str(alert.get("kind")), alert)
        if cls == alertkinds.USER and alert.get("id"):
            status = _state_of(alert["id"], rows)
            if not (status["user_delivered"] or status["abandoned"]):
                break
        index += 1
    if index != start:
        util.write_json(cursor_path(), {"seen": index})


def undelivered_count() -> int:
    """User-class alerts abandoned since the last user-class delivery by `t3` or `osascript` (AC-87 row 2;
    with `alert_macos: off` only a `t3` delivery clears it, A11.1 AC-10)."""
    count = 0
    clearing = USER_CHANNELS if macos_on() else ("t3",)
    for row in records():
        if row.get("channel") in clearing and row.get("step") == "delivered":
            count = 0
        elif row.get("channel") == ALERT and row.get("step") == "abandoned":
            count += 1
    return count


def status_lines() -> list[str]:
    """The `ygg status` line for abandoned alerts, or nothing; the stop line while the journal is corrupt."""
    try:
        count = undelivered_count()
    except util.YggError:
        return [CORRUPT]
    if not count:
        return []
    if count == 1:
        return ["1 alert couldn't be delivered; ask your main session to check it"]
    return [str(count) + " alerts couldn't be delivered; ask your main session to check them"]
