"""`ygg status` and `ygg pending` views."""

from __future__ import annotations

import re

from . import config, state, tracker, util
from .commands import WIP_STATES

PROJECT_LABELS = {
    "plain": {"initiating": "being planned", "pid-ready": "project plan ready for you",
              "pid-rejected": "project plan rejected", "executing": "in progress",
              "awaiting-approval": "waiting for your approval", "exception": "problem needing your call",
              "ready-to-merge": "ready to go into main", "closed": "closed", "stage": "phase"},
    "prince2": {"initiating": "initiating", "pid-ready": "PID ready", "pid-rejected": "PID rejected",
                "executing": "executing", "awaiting-approval": "awaiting approval", "exception": "exception",
                "ready-to-merge": "ready to merge", "closed": "closed", "stage": "stage"},
}


def stage_view(project_id: str):
    stages = tracker.milestones(project_id).get("stages") or []
    chosen = None
    for stage in stages:
        if stage.get("state") in ("executing", "awaiting-approval"):
            chosen = stage
            break
    if chosen is None:
        started = [s for s in stages if s.get("state") != "planned"]
        chosen = started[-1] if started else None
    if chosen is None:
        return None
    return {"number": int(chosen["number"]), "state": chosen["state"], "approved_by": chosen.get("approved_by")}


def envelope_view(envelope):
    if not envelope:
        return None
    keys = ("id", "scope", "until", "through_stage", "granted_at", "allow_local_merge", "grants",
            "ref_snapshot", "state")
    return {key: envelope.get(key) for key in keys if key in envelope or key != "grants"}


def route_map(project_id: str):
    text = tracker.read_document(project_id, "route-map")
    if not text:
        return None, []
    header, body = util.split_frontmatter(text)
    header = header or {}
    stage = stage_view(project_id)
    forecast = None
    for item in header.get("forecast_end") or []:
        if isinstance(item, dict) and stage and item.get("stage") == stage["number"]:
            value = item.get("date")
            forecast = util.iso(util.parse_time(str(value))) if value else None
    return {"forecast_end": forecast}, util.bullets(body, "Blockers")


def wip_limit(project_id: str) -> int | None:
    """The WIP limit through the layered configuration (preset, user config, profile, programme, PID)."""
    value = config.project_get("wip_limit", project_id)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def timebox_hours(project_id: str) -> int | None:
    """The timebox length through the same layers as the cadence (programme config, PID, file layers)."""
    value = config.project_get("timebox_hours", project_id)
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 336 else None


def effective_delivery(project_id: str) -> tuple[str, str, str | None]:
    """(approach, source, cadence) through the layered configuration (A3): programme config, PID and the
    file layers for `delivery_approach` and `cadence`, then the profile and core defaults.

    `source` names the layer that supplied the applicable delivery settings (ruling 75, A1.8, AC-90): the
    approach, and the cadence only while the approach is agile (it applies to nothing else). `programme`
    when the programme layer supplies either, else `pid` when the PID does, else `profile` when the profile
    does (key or default), else `core`.
    """
    layered = config.explain_all(project_id)
    profile = config.profile() or {}

    def kind(label) -> str:
        return {"pid": "pid", "programme": "programme", "profile": "profile"}.get(str(label).split(":", 1)[0], "core")

    approach, label = layered.get("delivery_approach", (None, ""))
    if approach:
        suppliers = [kind(label)]
    elif profile.get("delivery_approach_default"):
        approach, suppliers = profile["delivery_approach_default"], ["profile"]
    else:
        approach, suppliers = config.get("delivery_approach_default", "agile"), ["core"]
    cadence = None
    if approach == "agile":
        for key in ("cadence", "agile_cadence"):
            value, origin = layered.get(key, (None, ""))
            if value:
                cadence = value
                suppliers.append(kind(origin))
                break
        else:
            if profile.get("agile_cadence_default"):
                cadence = profile["agile_cadence_default"]
                suppliers.append("profile")
            else:
                cadence = config.get("agile_cadence_default", "flow")
    source = next((s for s in ("programme", "pid", "profile") if s in suppliers), "core")
    # Shown in status: a value under a secret-looking key is hidden, as in config explain.
    return str(util.redact_profile(approach)), source, util.redact_profile(cadence)


def delivery_view(project_id: str, header: dict | None):
    header = header or {}
    approach, source, cadence = effective_delivery(project_id)
    packages = [i for i in tracker.issues(project_id) if i.get("kind") == "work-package"]
    wip = None
    if approach == "agile" and cadence == "flow":
        limit = wip_limit(project_id)
        from . import workahead
        wip = {"limit": limit,
               "in_progress": sum(1 for p in packages if p.get("state") in WIP_STATES
                                  and workahead.wip_counts(project_id, p))}
    timebox = None
    hours = timebox_hours(project_id)
    if cadence == "timebox" and hours is not None:
        stages = tracker.milestones(project_id).get("stages") or []
        for stage in stages:
            if stage.get("state") == "executing" and stage.get("started_at"):
                start = util.parse_time(stage["started_at"])
                elapsed = (util.now() - start).total_seconds() / 3600
                number = int(elapsed // hours) + 1 if elapsed >= 0 else 1
                from datetime import timedelta
                timebox = {"stage": int(stage["number"]), "number": number,
                           "planned_end": util.iso(start + timedelta(hours=hours * number))}
    backlog = None
    text = tracker.read_document(project_id, "backlog")
    if text:
        backlog_header, _ = util.split_frontmatter(text)
        items = (backlog_header or {}).get("items") or []
        backlog = {"order": [i.get("id") for i in items],
                   "open": sum(1 for i in items if i.get("status") == "open"),
                   "must_open": sum(1 for i in items if i.get("status") == "open" and i.get("priority") == "must")}
    case = header.get("business_case")
    justified = None
    for stage in tracker.milestones(project_id).get("stages") or []:
        if stage.get("justified"):
            justified = stage["justified"]
    return {"approach": approach, "source": source, "cadence": cadence,
            "description": header.get("delivery_description"), "timebox": timebox, "wip": wip,
            "backlog": backlog, "business_case": "skipped" if case == "skipped" else "present",
            "justified_last": justified}


def build_status() -> dict:
    from . import envelopes
    nodes = state.all_nodes()
    scope = state.visible_programme(nodes)

    def visible(programme_id) -> bool:
        return scope is None or programme_id == scope

    from . import reducer
    owed_now = reducer.owed_all()
    unsettled: dict = {}  # project -> issue writes whose outcome is unknown (an owed launch may wait on one)
    if tracker.selected() == "linear":
        from .linear import adapter
        for key in adapter.pending_issues():
            unsettled.setdefault(key.partition("/")[0], []).append(key)
    wakes = state.wakes()
    programmes = []
    for programme in tracker.programmes():
        if not visible(programme["id"]):
            continue
        programmes.append({"id": programme["id"], "domain": programme.get("domain"),
                           "home": tracker.programme_home(programme), "kind": programme.get("kind"),
                           "envelope": envelope_view(envelopes.programme_envelope(programme["id"]))})
    projects = []
    packages = []
    decisions_open = []
    blocked_map: dict[str, list] = {}
    for project in tracker.projects():
        if not visible(project.get("programme")):
            continue
        pid_text = tracker.read_document(project["id"], "pid")
        pid_header = util.split_frontmatter(pid_text)[0] if pid_text else None
        route, blockers = route_map(project["id"])
        projects.append({
            "id": project["id"], "programme": project.get("programme"), "pm": project["pm"],
            "shape": project.get("shape", "standard"), "state": project.get("state", "initiating"),
            "hold": project.get("hold"), "stage": stage_view(project["id"]),
            "health": project.get("health", "onTrack"), "blockers": blockers, "route_map": route,
            "envelope": envelope_view(envelopes.active_envelope(project) or envelopes.last_envelope(project)),
            "delivery": delivery_view(project["id"], pid_header),
        })
        for issue in tracker.issues(project["id"]):
            if issue.get("kind") == "work-package":
                from . import review
                packages.append({"id": issue.id, "project": project["id"], "lane": issue.get("lane"),
                                 "state": issue.get("state", "planned"),
                                 "standard_version": int(issue.get("standard_version", 1)),
                                 "held_reason": review.held_reason(project["id"], issue.id)})
            elif issue.get("kind") == "decision":
                from . import decisions
                if decisions.is_open(issue):
                    decisions_open.append(issue.id)
                    target = decisions.blocked_node(issue)
                    if target:
                        blocked_map.setdefault(target, []).append(issue.id)
    node_views = []
    held = []
    from . import lifecycle
    # Derived from lifecycle facts and fresh evidence alone, never from a stored mirror: uncertain
    # activations, and those whose evidence is unknown right now (a visible hold, never stored).
    # A managed caller sees only its own programme's nodes (A9 scoped reads).
    uncertain = lifecycle.held_activations([n for n, node in nodes.items() if visible(node.get("programme"))])
    from . import conductor
    # Derived on every read: a capacity backoff from its exit fact, held-load and queued from the current
    # load sample and the blocker order (due liveness retries included). Nothing about a hold is stored.
    hold_reasons: dict = {}
    launch_states = conductor.launch_states(hold_reasons)
    from . import interlock
    paused_view = interlock.paused_view()
    stop_scopes = set(interlock.stop_scopes())
    held_stops = set(lifecycle.held_stop_nodes())
    for node in nodes.values():
        if not visible(node.get("programme")):
            continue
        runtime = state.runtime(node["id"])
        node_state = runtime.get("state", "idle")
        if node_state in ("held-load", "held-capacity"):
            node_state = "idle"  # a stored hold state is never trusted: holds are derived below
        if stop_scopes & {"*", node["id"], node.get("programme")}:
            node_state = "stopped"  # an active stop intent covers it (it survives a wipe of the state root)
        elif node_state == "stopped":
            node_state = "idle"  # no stop intent is active any more
        if node_state in ("idle", "running") and node["id"] in launch_states:
            node_state = launch_states[node["id"]]  # running here: a due liveness retry of a dead activation
        elif node_state == "idle" and node["id"] in wakes:
            node_state = "queued"
        # A pause holds a node that is not running: it shows as blocked by the pause, never as queued. (The
        # node state values are fixed by the status schema, so "paused" is the `paused` field.)
        paused_here = node_state in ("idle", "queued", "held-load", "held-capacity") and \
            (paused_view["global"] or node.get("programme") in paused_view["programmes"])
        if paused_here:
            node_state = "blocked"
        # A held stop (an ambiguous exit nobody settled) blocks the node until the user releases it.
        held_stop = node["id"] in held_stops
        if held_stop and node_state in ("idle", "queued", "held-load", "held-capacity"):
            node_state = "blocked"
        # Owed work whose launch would be refused for its agent permissions is held, visibly (A7 I4).
        permission_hold = None
        if node_state == "queued":
            from . import permissions
            permission_hold = permissions.hold_reason(node)
            if permission_hold:
                node_state = "blocked"
        if node_state in ("held-load", "held-capacity"):
            held.append(node["id"])
        activation = None
        if runtime.get("activation"):
            outcome = "uncertain" if runtime["activation"] in uncertain else "confirmed"
            activation = {"id": runtime["activation"], "launcher": runtime.get("launcher", "headless"),
                          "outcome": outcome}
        # Amendment A4: a team lead whose owed work is all withheld by the work-ahead gate stays queued
        # with the reason of its lowest-numbered held package (derived on every read, never stored).
        waiting = None
        if node_state == "queued" and node.get("role") == "tl":
            from . import workahead
            waiting = workahead.held_reason(node["id"], owed_now.get(node["id"], []), nodes)
        if node_state == "queued" and waiting is None:
            from . import t3
            waiting = t3.reuse_hold(node["id"])  # a visible hold: its T3 thread can't take the work yet (I4)
        if node_state == "queued" and waiting is None:
            from . import launcher
            # A visible hold (I4): a senior user owing only a retry whose package left testing (lane 3's predicate,
            # the same as the launch refusal's).
            waiting = launcher.retry_hold_reason(node, nodes, owed_now.get(node["id"], []))
            if waiting:
                held.append(node["id"])
        if node_state in ("idle", "queued") and waiting is None and unsettled.get(node.get("project")):
            # A visible hold (I4): work for this node may sit in a Linear write that is not settled yet.
            waiting = "the Linear write to " + ", ".join(unsettled[node["project"]]) + " has an unknown " \
                "outcome; the next conductor tick settles it from Linear"
        if node_state not in ("stopped", "stalled") and _chain_stalled(node["id"], runtime):
            node_state = "stalled"  # A11.1 AC-01, AC-02: derived from the failure chain on every read
        display = _display(node_state, runtime, activation)
        node_views.append({"id": node["id"], "role": node["role"], "parent": node.get("parent"),
                           "programme": node.get("programme"), "project": node.get("project"),
                           "pairs_with": node.get("pairs_with") or [], "state": node_state,
                           "blocked_by": list(runtime.get("blocked_by") or []) +
                           (["pause:" + ("*" if paused_view["global"] else str(node.get("programme")))]
                            if paused_here else []) + (["uncertain-activation"] if held_stop else []) +
                           (["agent-permissions"] if permission_hold else []),
                           "paused": bool(paused_here),
                           "held_reason": permission_hold or (hold_reasons.get(node["id"]) if node_state in
                                                              ("held-load", "held-capacity") else waiting) or
                           _activation_hold(activation) or _stop_hold(node_state, runtime),
                           "activation": activation, "display": display})
    from . import decisions
    shown_nodes = {view["id"] for view in node_views}
    for node_view in node_views:
        blocked = [d for ancestor in decisions.lineage(node_view["id"], nodes) for d in blocked_map.get(ancestor, [])]
        if blocked:
            node_view["blocked_by"] = sorted(set(node_view["blocked_by"]) | set(blocked))
            if node_view["state"] in ("idle", "queued"):
                node_view["state"] = "blocked"
    result = {
        "schema": "ygg.status.v1",
        "generated_at": util.iso(),
        "clock_source": util.clock_source(),
        "paused": {"global": paused_view["global"],
                   "programmes": [name for name in paused_view["programmes"] if visible(name)]},
        "programmes": programmes,
        "projects": projects,
        "nodes": node_views,
        "decisions_open": decisions_open,
        "morning_go_queue": [item for item in envelopes.morning_go_queue() if visible(item.get("programme"))],
        "inbox_pending": [item for item in state.all_inbox_pending() if scope is None or item["node"] in shown_nodes],
        "held_launches": held,
        "uncertain_keys": uncertain,
        # Cross-domain relay grants and authority anomalies are the user's business only.
        "relay_grants": [{"from_domain": g["from_domain"], "to_domain": g["to_domain"]}
                         for g in (util.read_json(tracker.root() / "_ygg" / "relay-grants.json", {}) or {})
                         .get("grants", [])] if scope is None else [],
        "ignored_authority": decisions.ignored_authority() if scope is None else [],
        "work_packages": packages,
    }
    from . import reducer
    reducer.shadow_log(result)  # test-mode diagnostic: the reducer checked against today's readers
    return result


def _project_suffix(project: dict) -> str:
    """A11 lane 2 project words: ready to close, reopened stages, the last approval waiting on merges, and
    ready to merge (AC-14, AC-37, AC-62, AC-65)."""
    from . import stageclose
    words = stageclose.status_suffix(project["id"])
    try:
        from . import mergecheck  # lane 4 (A11 AC-27, AC-83)
        found = mergecheck.status_text(project["id"])
        if found:
            words += ", " + found
    except (ImportError, AttributeError):
        pass
    stages = tracker.milestones(project["id"]).get("stages") or []
    for stage in stages:
        if stage.get("state") == "executing" and stage.get("reopened"):
            words += ", stage " + str(stage["number"]) + " reopened (" + ", ".join(stage["reopened"]) + ")"
    if project["state"] == "ready-to-merge":
        words += ", ready to merge, not yet in main"
    elif project["state"] == "executing" and stages and all(s.get("state") == "approved" for s in stages):
        words += ", all stages approved; waiting for every package to be merged and checked"
    return words


def _activation_hold(activation: dict | None) -> str | None:
    """A held activation says so (A11 AC-01 row 3: held, not signalled), except inside its confirm window, where it is
    still starting (AC-40 row 1)."""
    if not activation or activation.get("outcome") != "uncertain" or _inside_confirm_window(activation["id"]):
        return None
    return ("activation " + activation["id"] + " is held: its process can't be confirmed, so nothing restarts or "
            "signals it until you check it")


def _stop_hold(node_state: str, runtime: dict) -> str | None:
    """A stop that couldn't signal its node's process, because the live pid can't be identified as the agent, holds
    the activation: status says so rather than showing a quiet stop (A11 AC-01 row 3)."""
    if node_state != "stopped" or not runtime.get("activation"):
        return None
    from . import launcher, lifecycle
    record = launcher.receipt(runtime["activation"]) or {}
    pid = record.get("pid")
    if record.get("exited_at") is not None or not isinstance(pid, int) or isinstance(pid, bool) or \
            not util.pid_alive(pid):
        return None
    try:
        if lifecycle.signal_identity(pid, runtime["activation"], record):
            return None
    except (util.YggError, OSError, ValueError):
        pass
    return ("activation " + runtime["activation"] + " is held: pid " + str(pid) + " can't be identified as its agent, "
            "so the stop didn't signal it")


def _chain_stalled(node_id: str, runtime: dict) -> bool:
    from . import launcher, retries
    if retries.stalled(node_id) is None:
        return False
    if runtime.get("activation"):
        data = launcher.receipt(runtime["activation"]) or {}
        if data.get("exited_at") is None and data.get("outcome") not in ("aborted",):
            return False  # a run in progress (a stop's resume never starts a stalled node, AC-02 row 3)
    return True


def _display(node_state: str, runtime: dict, activation: dict | None) -> str | None:
    """The status word of a node with a live or held activation (AC-02, AC-40): `starting` inside its confirm
    window, `busy (running <program>)` inside an allowed tool call, else its state. Display only."""
    if node_state in ("stalled-suspect", "stalled"):
        return node_state
    if node_state != "running" or not activation:
        return None
    if runtime.get("outcome") == "pending" or _inside_confirm_window(activation["id"]):
        return "starting"
    if activation.get("outcome") == "uncertain":
        return "running (uncertain)"
    from . import busy
    record = busy.current(activation["id"])
    return busy.word(record) if record else "running"


def _inside_confirm_window(activation: str) -> bool:
    """A launch still inside its confirm window, judged on YGG_NOW from its reservation (AC-40 row 1): the
    first window is `launch_confirm_seconds` plus 10 s; a recorded second window (AC-22) extends it."""
    from . import launcher
    data = launcher.receipt(activation) or {}
    if data.get("exited_at") is not None or data.get("pid") is not None and data.get("outcome") == "confirmed":
        return False
    try:
        reserved = util.parse_time(str(data.get("reserved_at")))
    except (TypeError, ValueError):
        return False
    window = float(config.get("launch_confirm_seconds", 5)) + 10.0
    if data.get("confirm_window_seconds"):
        window = max(window, float(data["confirm_window_seconds"]))
    age = (util.now() - reserved).total_seconds()
    return 0 <= age < window


_LOAD = re.compile(r"^load ([0-9.]+) above ([0-9.]+)$")


def _waiting(reason: str) -> str:
    match = _LOAD.match(reason)
    if match:
        return ("waiting for the computer to be less busy (other programs are using it; load " + match[1] +
                ", limit " + match[2] + ")")
    return reason


def human(status: dict) -> str:
    vocabulary = config.get("vocabulary", "plain")
    labels = PROJECT_LABELS.get(vocabulary, PROJECT_LABELS["plain"])
    lines = []
    paused = status.get("paused") or {}
    programmes = sorted(paused.get("programmes") or [])
    named = ("programme " if len(programmes) == 1 else "programmes ") + ", ".join(programmes)
    if paused.get("global"):
        lines.append("Paused: everything" + (", and " + named if programmes else "") + ".")
    elif programmes:
        lines.append("Paused: " + named + ".")
    if not status["projects"]:
        lines.append("No projects yet.")
    for project in status["projects"]:
        line = project["id"] + ": " + labels.get(project["state"], project["state"])
        if project["stage"]:
            line += ", " + labels["stage"] + " " + str(project["stage"]["number"])
        line += _project_suffix(project)
        if project["state"] != "closed" and (status["paused"]["global"] or
                                             project.get("programme") in status["paused"]["programmes"]):
            line += ", paused"
        lines.append(line)
        if project["blockers"]:
            lines.append("  blockers: " + "; ".join(project["blockers"]))
    if status["decisions_open"]:
        lines.append("Decisions waiting: " + ", ".join(status["decisions_open"]))
    for item in status["morning_go_queue"]:
        if item.get("status") == "pending-user":
            line = "Morning go " + item["id"] + ": " + item["kind"] + " " + str(item.get("ref"))
            if item.get("flags"):
                line += " FLAGGED: " + "; ".join(item["flags"])
            lines.append(line)
    for node in status["nodes"]:
        if node.get("held_reason"):
            if _LOAD.match(node["held_reason"]):
                lines.append(node["id"] + ": " + _waiting(node["held_reason"]))
            else:
                lines.append(node["id"] + " waiting: " + node["held_reason"])
        elif node.get("display"):
            lines.append(node["id"] + ": " + node["display"])
    lines += _a11_lines(status)
    return "\n".join(lines) + "\n"


def _a11_lines(status: dict) -> list[str]:
    """Package review lines and user rulings (A11 lane 2)."""
    from . import cancel, review
    lines = review.status_lines(status) + cancel.status_lines({p["id"] for p in status.get("projects") or []})
    try:
        from . import alertdelivery  # lane 4 (A11 AC-87 row 2): undelivered user alerts
        lines += list(alertdelivery.status_lines() or [])
    except (ImportError, AttributeError):
        pass
    return lines
