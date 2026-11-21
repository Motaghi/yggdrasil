"""Test-mode fixture commands (A1.12)."""

from __future__ import annotations

import subprocess

from . import launcher, state, tracker, util


def _guard() -> None:
    # A managed session (YGG_NODE, YGG_ACTIVATION or a managed worktree) may never run a fixture verb: they write
    # judgements and state as the user (A7 I11, A11 amendment 25).
    if not util.test_mode() or state.managed_session():
        raise util.refuse("test-mode: fixture commands need YGG_TEST_MODE=1 and no YGG_NODE")


def set_next(kind: str, number: int) -> str:
    _guard()
    tracker.set_next(kind, number)
    return "next " + kind + " is " + str(number) + "\n"


def _dead_identity() -> dict:
    process = subprocess.Popen(["true"])
    process.wait()
    return {"pid": process.pid, "started_at": "exited"}


def _phased(args, node: dict) -> str:
    """Seed a reserved or spawned activation through the lifecycle facts production writes (A7.4): the
    reservation's launcher and the spawned supervisor are dead processes; the agent is live, dead or the
    given identity."""
    from . import lifecycle
    identifier = tracker.allocate("activation")
    launcher_id = _dead_identity()
    lifecycle.commit(node["id"], identifier, "reserved", launcher_pid=launcher_id["pid"],
                     launcher_started_at=launcher_id["started_at"])
    data = {"node": node["id"], "activation": identifier, "launcher": args.launcher, "lifecycle": True,
            "phase": "reserved", "launcher_pid": launcher_id["pid"], "launcher_started_at": launcher_id["started_at"],
            "pid": None, "process_started_at": None, "reserved_at": util.iso(), "started_at": util.iso(),
            "outcome": "pending", "retries_used": args.retries_used, "reason": "launch",
            "programme": node.get("programme"), "project": node.get("project"), "role": node.get("role")}
    if args.phase == "spawned":
        supervisor = _dead_identity()
        lifecycle.commit(node["id"], identifier, "spawned", supervisor_pid=supervisor["pid"],
                         supervisor_started_at=supervisor["started_at"])
        data.update(phase="starting", supervisor_pid=supervisor["pid"],
                    supervisor_started_at=supervisor["started_at"])
        if args.pid is not None:
            data.update(pid=args.pid, process_started_at=args.process_start)
        elif args.process == "live":
            process = subprocess.Popen(["sleep", "3600"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            data.update(pid=process.pid, process_started_at=util.process_start(process.pid))
        elif args.process == "dead":
            dead = _dead_identity()
            data.update(pid=dead["pid"], process_started_at=dead["started_at"])
    with util.locked():
        launcher.save_receipt(identifier, data)
        runtime = state.runtime(node["id"])
        runtime.update(state="running", activation=identifier, launcher=args.launcher, outcome="pending",
                       retries_used=args.retries_used)
        state.save_runtime(node["id"], runtime)
        if args.heartbeat:
            util.atomic_write(state.root() / "heartbeats" / node["id"],
                              util.iso(util.parse_time(args.heartbeat)) + "\n")
    return identifier + "\n"


def activation(args) -> str:
    _guard()
    node = state.require_node(args.node)
    if getattr(args, "phase", None):
        return _phased(args, node)
    identifier = tracker.allocate("activation")
    pid, started = None, None
    if args.process == "live":
        process = subprocess.Popen(["sleep", "3600"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        pid, started = process.pid, util.process_start(process.pid)
    elif args.process == "dead":
        process = subprocess.Popen(["true"])
        process.wait()
        pid, started = process.pid, "exited"
    data = {"node": node["id"], "activation": identifier, "launcher": args.launcher, "pid": pid,
            "process_started_at": started, "started_at": util.iso(), "outcome": args.outcome,
            "retries_used": args.retries_used, "reason": "launch", "process": args.process,
            "programme": node.get("programme"), "project": node.get("project")}
    if args.process == "dead":
        data.update(exit_code=None, exited_at=util.iso())
    with util.locked():
        launcher.save_receipt(identifier, data)
        runtime = state.runtime(node["id"])
        runtime.update(state=args.state if args.state == "running" else "idle", activation=identifier,
                       launcher=args.launcher, outcome=args.outcome, retries_used=args.retries_used)
        state.save_runtime(node["id"], runtime)
        if args.state == "queued":
            state.wake(node["id"], "test")
        if args.heartbeat:
            util.atomic_write(state.root() / "heartbeats" / node["id"],
                              util.iso(util.parse_time(args.heartbeat)) + "\n")
        if args.outcome == "uncertain":
            # Uncertain as production derives it: spawned, its launcher and supervisor dead, no agent
            # evidence and no exit (no stored mirror).
            from . import lifecycle
            launcher_id, supervisor = _dead_identity(), _dead_identity()
            lifecycle.commit(node["id"], identifier, "reserved", launcher_pid=launcher_id["pid"],
                             launcher_started_at=launcher_id["started_at"])
            lifecycle.commit(node["id"], identifier, "spawned", supervisor_pid=supervisor["pid"],
                             supervisor_started_at=supervisor["started_at"])
    return identifier + "\n"


# ---------------------------------------------------------------- A11 test-mode verbs (amendment 25)

def _field(value: str):
    """A `--name value` field of `ygg test alert`: true and false as booleans, whole numbers as numbers."""
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        return value


def alert(kind: str, extra: list[str]) -> str:
    """`ygg test alert <kind> [--name value ...]` (driver.raise_alert): raise one alert of that kind with the
    given fields, classed by the alert-kind table like any other alert."""
    _guard()
    from . import alertkinds
    if not kind.startswith("ygg-"):
        raise util.usage("test alert: kind must start with ygg-")
    fields = {}
    items = list(extra)
    while items:
        name = items.pop(0)
        if not name.startswith("--") or not items:
            raise util.usage("test alert: fields are --name value pairs")
        fields[name[2:].replace("-", "_")] = _field(items.pop(0))
    entry = state.alert(kind, alertkinds.test_message(kind, fields), test=True, **fields)
    return entry["id"] + " " + kind + " (" + entry["class"] + ")\n"


def acceptance(project: str, package: str, sha: str) -> str:
    """`ygg test acceptance <project> <package> <sha>` (driver.seed_acceptance): record the package as accepted
    at that SHA by its paired senior user, running no check, the way a pre-A11 acceptance looks."""
    _guard()
    from . import commands
    util.check_id(project, "project id")
    util.check_record_id(package)
    with util.locked():
        tracker.ensure_writable()
        issue = tracker.read_issue(project, package)
        if issue is None or issue.get("kind") != "work-package":
            raise util.usage("unknown work package '" + package + "'")
        lane = issue.get("lane")
        seniors = commands.paired_seniors(lane, state.all_nodes())
        senior = seniors[0] if seniors else None
        issue.records.append(tracker.record("acceptance", senior, sha=sha, verdict="accept", caller=senior))
        issue.header["state"] = "accepted"
        tracker.owe(issue.header, (tracker.project(project) or {}).get("pm"), "acceptance:" + package)
        issue.save()
    return "acceptance seeded on " + package + " at " + sha + "\n"
