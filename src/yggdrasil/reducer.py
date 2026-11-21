"""The reducer: one pure fold of durable journal facts, and a view computed from that fold plus a fresh
evidence snapshot.

`fold(entries)` folds a programme's trusted journal entries. Its result is cached in memory per programme,
keyed by the number of entries plus a hash of the last one, so an unchanged journal is never folded twice.
`view(folds, evidence)` is pure as well: time, the safety-folder interlock markers and the platform pause are
read into the evidence snapshot on every read and never cached. `read()` is the shared entry point.

This runs alongside today's readers. `shadow_diff(status)` compares the reducer's projection with what
today's readers report, so the two can be checked against each other before any command switches over.
"""

from __future__ import annotations

import json

from . import util

_CACHE: dict = {}


def fold(entries: list[dict]) -> dict:
    """Fold one programme's trusted journal entries into the durable facts the view needs."""
    result = {"inbox": {}, "intents": [], "intent_ids": set(), "resumed": set(),
              "events": {}, "checkpoints": {}, "activations": {}, "envelopes": {}, "chain_resets": {}}
    for sequence, entry in enumerate(entries):
        kind = entry.get("kind")
        if kind == "resume":
            result["resumed"].update(str(i) for i in entry.get("clears") or [])
        if kind == "chain-reset":
            result["chain_resets"][entry.get("node")] = sequence
            continue
        if kind in ("pause", "stop") and entry.get("intent"):
            result["intent_ids"].add(str(entry["intent"]))  # journalled at all (persisting is idempotent)
        if kind == "activation":
            # Lifecycle facts are never cleared: the work an activation was given, and how it ended.
            record = result["activations"].setdefault(entry["key"], {"node": entry.get("node"), "phases": [],
                                                                     "work": {}, "terminal": None, "end": {},
                                                                     "sequence": sequence, "retries_used": 0})
            record["phases"].append(entry.get("phase"))
            if entry.get("phase") == "reserved":
                record["sequence"] = sequence
                record["retries_used"] = int(entry.get("retries_used") or 0)
                if isinstance(entry.get("work"), dict):
                    record["work"] = entry["work"]
                if isinstance(entry.get("packages"), list):
                    # The packages the work set is about: a retry of this activation continues them.
                    record["packages"] = [str(p) for p in entry["packages"]]
                if isinstance(entry.get("blocked"), list):
                    # The work-ahead selection the work set was made with (the packages blocked then), so the
                    # activation's context matches what it consumes, after a wipe too.
                    record["blocked"] = [str(p) for p in entry["blocked"]]
                    record["packages"] = record.get("packages") or []
            if entry.get("phase") in ("exited", "aborted", "released") and record["terminal"] is None:
                record["terminal"] = entry["phase"]
                record["end"] = {k: entry.get(k) for k in ("exit_code", "spawn_failed", "capacity", "released_as",
                                                           "backoff_until", "reset_kill", "interrupted",
                                                           "ambiguous", "crashed")}
            if entry.get("phase") == "resolved" and record.get("resolution") is None:
                record["resolution"] = entry.get("outcome")  # the user's call on an ambiguous exit
            continue
        if entry.get("cleared_at"):
            continue
        if kind == "inbox":
            result["inbox"][entry["msg_id"]] = {"node": entry.get("node"), "msg_id": entry["msg_id"],
                                                "kind": entry.get("message_kind"), "sender": entry.get("sender"),
                                                "at": entry.get("at"),
                                                "packages": _named_packages(entry.get("body"))}
            bound = result["activations"].get(str(entry.get("bound") or ""))
            if bound is not None and bound.get("node") == entry.get("node"):
                # AC-29: acked by its running activation, the message joins that activation's work.
                work = dict(bound.get("work") or {})
                messages = list(work.get("messages") or [])
                if entry["msg_id"] not in messages:
                    messages.append(entry["msg_id"])
                work["messages"] = messages
                bound["work"] = work
        elif kind == "event":
            result["events"][entry["event_id"]] = {"node": entry.get("node"), "id": entry["event_id"],
                                                   "reason": entry.get("reason"), "at": entry.get("at")}
        elif kind == "envelope-ended":
            result["envelopes"][entry["key"]] = {"key": entry["key"], "at": entry.get("at")}
        elif kind == "checkpoint":
            # A batch entry without a message id is legacy: it is owed until a PM activation clears it.
            report_id = entry.get("msg_id") or "legacy-" + str(entry.get("lane")) + "@" + str(entry.get("at"))
            result["checkpoints"][report_id] = {"pm": entry.get("pm"), "lane": entry.get("lane"), "msg_id": report_id,
                                                "at": entry.get("at"), "legacy": not entry.get("msg_id")}
        elif kind in ("pause", "stop"):
            result["intents"].append({"kind": kind, "scope": entry.get("scope"),
                                      "intent": str(entry["intent"]) if entry.get("intent") else None})
    return result


def _named_packages(body) -> list:
    """The packages a message body names (the same ids the work-ahead gate reads)."""
    import re
    found = []
    for match in re.findall(r"\bWP-[0-9]+\b", str(body or "")):
        if match not in found:
            found.append(match)
    return found


def fold_programme(programme: str | None) -> dict:
    """The programme's fold. It is reused only while the journal file's stat key (its location included, so
    two tracker roots never share one) and the authority actors are exactly what it was made from, and then
    without reading the journal at all; any other change, or a file that changed while it was read, folds
    the journal again. A journal that is missing before and after its read is stamped as missing."""
    import os
    from . import journal
    from .commands import authority_actors
    actors = frozenset(authority_actors())
    cached = _CACHE.get(programme)
    missing = ("missing", os.path.abspath(journal.path_for(programme)))
    now = journal.file_key(programme) or missing
    if cached and cached[1] == (now, actors):
        return cached[0]  # the very file the fold was made from, unchanged, under the same authority
    marker, _, raw = journal.keyed_snapshot(programme)
    if marker is None and not raw and journal.file_key(programme) is None:
        marker = missing  # still missing after the read: the empty fold is what the journal holds
    entries, _ = journal.trusted_entries(programme, raw)
    folded = fold(entries)
    _CACHE[programme] = (folded, (marker, actors) if marker else None)
    return folded


def folds() -> dict:
    from . import journal
    return {programme: fold_programme(programme) for programme in journal.all_programmes()}


def evidence() -> dict:
    """The live inputs, read fresh on every call: never cached."""
    from . import conductor, lifecycle, safety, state
    return {"now": util.iso(), "markers": safety.markers(), "platform_paused": state.platform_paused(),
            "held_keys": lifecycle.held_activations(), "launch_holds": conductor.launch_holds()}


def view(folded: dict, snapshot: dict) -> dict:
    """The projection today's status reports, computed from the fold and the evidence snapshot alone."""
    inbox, uncertain, held, paused_scopes, stop_scopes = [], [], [], set(), set()
    resumed = set().union(*(f["resumed"] for f in folded.values())) if folded else set()
    for programme_fold in folded.values():
        inbox.extend({"node": m["node"], "msg_id": m["msg_id"]} for m in programme_fold["inbox"].values())
        # A journalled intent is active until a committed resume lists its id (an entry without an id,
        # written before ids existed, until it is cleared).
        for item in programme_fold["intents"]:
            if item["intent"] not in resumed or item["intent"] is None:
                (paused_scopes if item["kind"] == "pause" else stop_scopes).add(item["scope"])
    uncertain.extend(k for k in snapshot.get("held_keys") or [] if k not in uncertain)
    # Holds are derived: a capacity backoff from its exit fact, held-load from the load sample and the
    # blocker order (due liveness retries included).
    held.extend(snapshot.get("launch_holds") or {})
    for marker in snapshot.get("markers") or []:
        (paused_scopes if marker.get("kind") == "pause" else stop_scopes).add(marker.get("scope"))
    return {
        "inbox_pending": sorted(inbox, key=lambda m: (str(m["node"]), m["msg_id"])),
        "uncertain_keys": sorted(uncertain),
        "held_launches": sorted(held),
        "paused": {"global": "*" in paused_scopes or bool(snapshot.get("platform_paused")),
                   "programmes": sorted(s for s in paused_scopes if s and s != "*")},
        "stops": sorted(s for s in stop_scopes if s),
    }


def read() -> dict:
    """The shared read: refresh the fold if the journal changed, take a fresh evidence snapshot."""
    return view(folds(), evidence())


def today(status: dict) -> dict:
    """The same projection from the production readers (status plus the active stop intents)."""
    from . import interlock
    stops = interlock.stop_scopes()
    return {
        "inbox_pending": sorted(({"node": m["node"], "msg_id": m["msg_id"]} for m in status.get("inbox_pending") or []),
                                key=lambda m: (str(m["node"]), m["msg_id"])),
        "uncertain_keys": sorted(status.get("uncertain_keys") or []),
        "held_launches": sorted(status.get("held_launches") or []),
        "paused": {"global": bool((status.get("paused") or {}).get("global")),
                   "programmes": sorted((status.get("paused") or {}).get("programmes") or [])},
        "stops": sorted(stops),
    }


def shadow_diff(status: dict) -> dict:
    """Fields where the reducer and today's readers disagree (empty when they agree)."""
    mine, theirs = read(), today(status)
    return {key: {"reducer": mine[key], "today": theirs[key]} for key in mine if mine[key] != theirs[key]}


def shadow_log(status: dict) -> None:
    """Test mode only: append any disagreement to `YGG_SHADOW_LOG`. A diagnostic, not a store write."""
    target = util.env("YGG_SHADOW_LOG")
    if not target or not util.test_mode() or util.env("YGG_NODE") or util.env("YGG_ACTIVATION"):
        return
    try:
        diff = shadow_diff(status)
    except util.YggError as error:
        diff = {"error": error.message}
    if diff:
        with open(target, "a") as handle:
            handle.write(json.dumps({"at": util.iso(), "state_root": str(util.state_root()), "diff": diff}) + "\n")



# ---------------------------------------------------------------- owed work (level-triggered wakes)

RELAUNCH_KINDS = ("capacity", "retry")
WORK_KEYS = {"messages": "message", "events": "event", "checkpoints": "checkpoint", "envelopes": "envelope",
             "records": "record", "capacity": "capacity", "retries": "retry"}
# An ended envelope owes one reviewer launch per programme: any reviewer given it settles it for all.
SHARED_KINDS = ("envelope",)


def _relaunches(programme_fold: dict, limit: int) -> list[dict]:
    """Relaunches owed from exit facts alone (no stored hold). Every capacity exit owes one launch of its
    node, not before its `backoff_until`, carrying the activation's retry count. An attempt that exited
    with a failure (not capacity, not a failed process creation, not a reset kill) owes a liveness retry
    while its chain has retries left and no `chain-reset` came after it, until a later attempt that ran
    was given that retry (see `_supersedes`)."""
    found = []
    ran: dict = {}
    for key, record in programme_fold["activations"].items():
        node = record.get("node")
        end = record.get("end") or {}
        # An activation that ran nothing (aborted, or its process was never created) is not the chain's
        # latest attempt: a retry it was given stays owed from the activation before it.
        ran_nothing = record.get("terminal") == "aborted" or end.get("released_as") == "aborted" or \
            bool(end.get("spawn_failed")) or bool(end.get("interrupted")) or \
            (bool(end.get("ambiguous")) and record.get("resolution") != "failed")
        # A stopped attempt is not the latest one either: the retry it was given, with its count, stays owed.
        if record.get("terminal") is None and "spawned" not in record.get("phases", []):
            ran_nothing = True  # a reservation still being started
        if node and not ran_nothing:
            ran.setdefault(node, []).append((key, record))
        if record.get("terminal") == "exited" and end.get("capacity"):
            reset = programme_fold["chain_resets"].get(node, -1) > record["sequence"]
            found.append({"node": node, "kind": "capacity", "id": key, "reason": "capacity",
                          "retries_used": 0 if reset else record.get("retries_used", 0),
                          "backoff_until": end.get("backoff_until"), "at": end.get("backoff_until")})
    for node, attempts in ran.items():
        attempts.sort(key=lambda pair: pair[1]["sequence"])
        # One pass from the latest attempt back: an attempt's retry is settled when a later attempt that
        # ran recorded no retry list, or was given that retry (the same rule as `_supersedes`).
        settled, blanket, given = [False] * len(attempts), False, set()
        for index in range(len(attempts) - 1, -1, -1):
            key, record = attempts[index]
            settled[index] = blanket or key in given
            work = record.get("work")
            if not isinstance(work, dict) or "retries" not in work:
                blanket = True
            else:
                given.update(str(i) for i in work.get("retries") or [])
        for index, (key, record) in enumerate(attempts):
            if not settled[index]:
                found += _retry_owed(programme_fold, node, key, record, limit)
    return found


def _supersedes(later: dict, key: str) -> bool:
    """Whether a later attempt that ran settles an earlier one's retry. It does when it was given that retry,
    and when its reservation records no retry list at all (facts from before work sets). An attempt whose
    work set left the retry out (another activation held it then) does not, so the retry and its count stay
    owed until the activation holding it settles (I4)."""
    work = later.get("work")
    if not isinstance(work, dict) or "retries" not in work:
        return True
    return key in [str(i) for i in work.get("retries") or []]


def _retry_owed(programme_fold: dict, node: str, key: str, record: dict, limit: int) -> list[dict]:
    """The liveness retry an attempt that ran owes: when it failed, its chain has retries left and no
    `chain-reset` came after it."""
    end = record.get("end") or {}
    if record.get("terminal") != "exited" or end.get("exit_code") in (0, None) or end.get("capacity") or \
            end.get("spawn_failed") or end.get("reset_kill") or end.get("interrupted") or end.get("ambiguous"):
        # A stop or reset is not a failure: its work is owed again, not retried. A stopped run the user released
        # (A11 AC-72 row 3: `--as failed` means done, `--as aborted` means offer the work again) owes no retry.
        return []
    if programme_fold["chain_resets"].get(node, -1) > record["sequence"]:
        return []
    if int(record.get("retries_used") or 0) >= limit:
        return []
    return [{"node": node, "kind": "retry", "id": key, "reason": "retry",
             "retries_used": int(record.get("retries_used") or 0) + 1, "at": None}]


def work_set(items: list[dict], unread: list[str]) -> dict:
    """The work an activation is given at reservation: every unread message and each owed item."""
    work = {"messages": list(unread) + [i["id"] for i in items if i["kind"] == "message" and i["id"] not in unread]}
    for key, kind in WORK_KEYS.items():
        if key != "messages":
            work[key] = [i["id"] for i in items if i["kind"] == kind]
    reasons = [str(i["reason"]) for i in items if i["kind"] == "record" and i.get("reason")]
    if reasons:
        work["reasons"] = reasons  # what each record stamp is about, for the receipt (never an owed key)
    return work


def consumption(activations: dict) -> tuple[set, set]:
    """(consumed, reserved) work-item keys. An item is consumed once an activation given it was spawned and
    did not end aborted, as a failed process creation, as a capacity exit, interrupted by a signal (a stop or
    a reset kill: an interruption never consumes, I4), or released as aborted; it is
    reserved while the activation given it has not ended. Keys are `(node, kind:id)`: consumption is scoped
    to the node the activation ran for (`*` for a shared kind)."""
    consumed, reserved = set(), set()
    for record in activations.values():
        node = record.get("node")
        items = set()
        for kind, ids in (record.get("work") or {}).items():
            singular = WORK_KEYS.get(kind, kind)
            for i in ids or []:
                items.add(("*" if singular in SHARED_KINDS else node, singular + ":" + str(i)))
        if not items:
            continue
        terminal, end = record.get("terminal"), record.get("end") or {}
        if terminal is None:
            reserved |= items
            continue
        if end.get("ambiguous"):
            # Stopped after the signal reached it, without dying of it: finished or cut short. Held until
            # the user settles it (failed: done; aborted: offered again); never replayed on its own.
            if record.get("resolution") == "failed":
                consumed |= items
            elif record.get("resolution") != "aborted":
                reserved |= items
            continue
        if "spawned" not in record.get("phases", []) or terminal == "aborted" or end.get("spawn_failed") or \
                end.get("capacity") or end.get("interrupted") or end.get("reset_kill") or \
                (terminal == "released" and end.get("released_as") == "aborted"):
            continue
        if terminal == "exited" and end.get("exit_code") not in (0, None):
            continue  # A11 AC-29, A11.1 AC-02: a failure consumes nothing; its work goes to the next run
        consumed |= items
    return consumed, reserved


def owed_all(now=None) -> dict:
    """node -> its owed work items, oldest first, derived from journal facts alone (no queued wakes):
    unacknowledged messages, uncleared events, and due checkpoint reports, minus what an activation was
    given (consumed, or reserved while it runs). Nodes of closed projects owe nothing."""
    from datetime import timedelta
    from . import config, state, tracker
    from .commands import WIP_STATES
    nodes = state.all_nodes()
    moment = now or util.now()
    hours = float(config.get("checkpoint_batch_hours", 4))
    limit = int(config.get("retry_limit", 2))
    folded = folds()
    activations: dict = {}
    for programme_fold in folded.values():
        activations.update(programme_fold["activations"])
    consumed, reserved = consumption(activations)
    closed = {p["id"] for p in tracker.projects() if p.get("state") == "closed"}
    cancelled = {p["id"]: {i.id for i in tracker.issues(p["id"]) if i.get("kind") == "work-package"
                           and i.get("state") == "cancelled"} for p in tracker.projects()}
    owed: dict = {}

    def add(node_id, item, packages=None):
        node = nodes.get(node_id)
        if node is None and node_id != "l1":
            return
        if node and node.get("project") in closed:
            return
        gone = cancelled.get((node or {}).get("project")) or set()
        if packages and gone and all(p in gone for p in packages):
            return  # A11.1 AC-06: an item whose packages are all cancelled is settled
        key = ("*" if item["kind"] in SHARED_KINDS else node_id, item["kind"] + ":" + item["id"])
        if key in consumed or key in reserved:
            return
        owed.setdefault(node_id, []).append(item)

    for programme, programme_fold in folded.items():
        for envelope in programme_fold["envelopes"].values():
            for node_id, node in nodes.items():
                if node.get("role") == "reviewer" and node.get("programme") == programme and not node.get("project"):
                    add(node_id, {"kind": "envelope", "id": envelope["key"], "reason": "envelope-ended:" + envelope["key"],
                                  "at": envelope.get("at")})
        for message in programme_fold["inbox"].values():
            node = nodes.get(message["node"]) or {}
            if message.get("kind") == "checkpoint" and node.get("role") == "pm" and message.get("sender") != message["node"]:
                continue  # a report to a PM owes a launch only when its batch is due (below)
            if message.get("kind") == "ruling":
                continue  # a ruling is launch context, never owed work (AC-29, AC-89)
            add(message["node"], {"kind": "message", "id": message["msg_id"], "reason": "message:" + message["msg_id"],
                                  "at": message.get("at")}, message.get("packages"))
        for event in programme_fold["events"].values():
            add(event["node"], {"kind": "event", "id": event["id"], "reason": state.decode_reason(str(event["reason"])),
                                "at": event.get("at")})
        for item in _relaunches(programme_fold, limit):
            # A retry or capacity relaunch continues its activation's packages (A11.1 AC-06: settled with them).
            record = programme_fold["activations"].get(str(item.get("id"))) or {}
            add(item.pop("node"), item, record.get("packages"))
    # Obligations stamped on tracker records, written in the same write as the record change.
    for project in tracker.projects():
        stamped = list(project.get("owed") or []) + list(tracker.milestones(project["id"]).get("owed") or [])
        for issue in tracker.issues(project["id"]):
            # A stamp on a work package is about that package (the work-ahead gate reads it).
            about = {"package": issue.id} if issue.get("kind") == "work-package" else {}
            stamped += [dict(s, **about) if isinstance(s, dict) else s for s in issue.header.get("owed") or []]
        backlog = tracker.read_document(project["id"], "backlog")
        if backlog:
            stamped += list(tracker.parse_issue(project["id"], "backlog", backlog).header.get("owed") or [])
        for stamp in stamped:
            if isinstance(stamp, dict) and stamp.get("id") and stamp.get("node"):
                add(str(stamp["node"]), dict({"kind": "record", "id": str(stamp["id"]), "reason": str(stamp.get("reason")),
                                              "at": stamp.get("at")},
                                             **({"package": str(stamp["package"])} if stamp.get("package") else {}),
                                             **({"review": str(stamp["review"])} if stamp.get("review") else {})),
                    [str(stamp["package"])] if stamp.get("package") else None)
    # Checkpoint batches: each report keeps its own deadline; a complete batch is due at once.
    reports: dict = {}
    for programme_fold in folded.values():
        for report in programme_fold["checkpoints"].values():
            # A report is settled once given to a PM activation, as a due report or as an inbox message.
            keys = {(report["pm"], "checkpoint:" + report["msg_id"]), (report["pm"], "message:" + report["msg_id"])}
            if keys & (consumed | reserved):
                continue
            if not report.get("legacy") and programme_fold["inbox"].get(report["msg_id"]) is None:
                continue  # acknowledged: the report owes nothing
            reports.setdefault(report["pm"], []).append(report)
    for project in tracker.projects():
        pm = project["pm"]
        items = reports.get(pm) or []
        if not items:
            continue
        active = {i.get("lane") for i in tracker.issues(project["id"])
                  if i.get("kind") == "work-package" and i.get("state") in WIP_STATES and i.get("lane")}
        complete = active <= {r["lane"] for r in items}
        for report in items:
            try:
                due = complete or moment >= util.parse_time(str(report["at"])) + timedelta(hours=hours)
            except ValueError:
                due = True
            if due:
                add(pm, {"kind": "checkpoint", "id": report["msg_id"], "reason": "checkpoints:" + report["msg_id"],
                         "at": report.get("at")})
        reports.pop(pm, None)
    for items in owed.values():
        items.sort(key=lambda i: str(i.get("at") or ""))
    return owed


def owed(node_id: str) -> list[dict]:
    return owed_all().get(node_id, [])
