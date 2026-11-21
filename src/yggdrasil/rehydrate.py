"""Rehydration documents (A1.4): at most the configured number of lines."""

from __future__ import annotations

import re

from . import config, state, tracker, util


def _one_line(text) -> str:
    return " ".join(str(text).replace("`", "'").split())[:200]


def _section(title: str, items: list[str], limit: int) -> list[str]:
    """A section whose items are fenced as untrusted data; the overflow line sits outside the fence."""
    shown = items[:limit]
    lines = ["## " + title, "```text"]
    lines += [_one_line(item) for item in shown] or ["none"]
    lines.append("```")
    if len(items) > limit:
        lines.append("and " + str(len(items) - limit) + " more in the tracker")
    lines.append("")
    return lines


def write_document(node_id: str) -> None:
    nodes = state.all_nodes()
    node = nodes.get(node_id)
    if not node or not node.get("project"):
        return
    project = node["project"]
    cap = int(config.get("rehydration_line_cap", 150))
    issues = tracker.issues(project)
    from . import decisions, workahead
    # Amendment A4: a team lead's document leaves out every later-stage package it may not work on now
    # (held or suspended), and every line or message that names one.
    blocked = workahead.blocked_packages(node_id, nodes)

    def names_blocked(text) -> bool:
        return bool(blocked) and any(p in blocked for p in workahead.PACKAGE_RE.findall(str(text or "")))

    open_decisions = [i.id + ": " + str(i.get("question")) for i in issues if decisions.is_open(i)
                      and (node["role"] == "pm" or i.get("node") == node_id)]
    next_actions = []
    for issue in issues:
        if issue.get("kind") != "work-package" or issue.id in blocked:
            continue
        if node["role"] == "pm" or issue.get("lane") == node_id:
            next_actions.append(issue.id + " (" + str(issue.get("state")) + "): " + str(issue.get("title")))
    inbox = [m["msg_id"] + " from " + m["from"] + ": " + m["body"].splitlines()[0][:100]
             for m in state.inbox(node_id) if not m.get("acked_at") and m.get("body")
             and not workahead.hidden(node_id, m, nodes)]
    recent = []
    for issue in issues[-50:]:
        if issue.id in blocked:
            continue
        for record in issue.records[-3:]:
            recent.append(issue.id + " " + str(record.get("kind")) + " by " + str(record.get("node") or record.get("actor")))
    open_decisions = [line for line in open_decisions if not names_blocked(line)]
    next_actions = [line for line in next_actions if not names_blocked(line)]
    recent = [line for line in recent if not names_blocked(line)]
    header = {"kind": "rehydrate", "node": node_id, "standard_version": util.STANDARD_VERSION,
              "actor": "ygg-app", "created_at": util.iso()}
    sections = [("Open decisions", open_decisions), ("Next actions", next_actions), ("Inbox", inbox),
                ("Recent records", recent)]
    budget = cap
    while True:
        lines = ["## Role", "- " + node["role"] + " " + node_id + " of project " + project, ""]
        for title, items in sections:
            lines += _section(title, items, budget)
        text = util.render_frontmatter(header, "\n".join(lines))
        if len(text.splitlines()) <= cap or budget == 0:
            break
        budget -= 1
    with util.locked():
        tracker.write_document(project, "rehydrate-" + node_id, text)


MSG_RE = re.compile(r"^m-[0-9]{4,9}$")
ACT_RE = re.compile(r"^act-[0-9]{4,9}$")


def _valid_node(value, nodes) -> bool:
    return isinstance(value, str) and util.ID_RE.match(value) is not None and value in nodes


def consumed_events() -> dict[str, set]:
    """node -> message ids given to activations that were not aborted (from receipts)."""
    found: dict[str, set] = {}
    base = state.root() / "activations"
    if not base.is_dir():
        return found
    for path in base.glob("act-*.json"):
        data = util.read_json(path, {}) or {}
        # Delivery needs an agent process that started: aborted and failed process creations consume nothing.
        if not isinstance(data, dict) or data.get("outcome") == "aborted" or not data.get("node") or \
                data.get("pid") is None or data.get("spawn_failed"):
            continue
        given = data.get("consumed") or {}
        events = found.setdefault(str(data["node"]), set())
        events.update(str(m) for m in given.get("messages") or [])
    return found


def replay_journal() -> int:
    """Restore in-flight state from trusted journal entries; return the number of entries skipped.

    Idempotent, so `ygg rehydrate` and every conductor tick use it: an entry only restores what local
    state is missing (a message id not in the inbox, a runtime file that does not exist, a pause or stop
    marker that is absent, a checkpoint not yet batched). A missing wake is repaired for any outstanding
    entry (an unacknowledged message, an uncleared hold, an ended envelope) unless the node has launched
    since. An uncertain entry is cleared only by its own activation's receipt; a runtime restored from
    another entry never counts as reconciliation. Owed work (wakes) is not restored: it is derived from the
    journal facts on every read.
    """
    from . import journal
    nodes = state.all_nodes()
    nodes_with_l1 = dict(nodes, l1={"id": "l1"})
    skipped = 0
    with util.locked():
        restored_messages: dict[str, list] = {}
        checkpoints = util.read_json(state.root() / "checkpoints.json", {}) or {}
        checkpoints_changed = False

        consumed = consumed_events()

        def was_consumed(node_id: str, event: str) -> bool:
            """Whether an activation that was not aborted was given this message or wake (its receipt says so)."""
            return event in consumed.get(node_id, set())

        programme_entries = {p: journal.trusted_entries(p) for p in journal.all_programmes()}
        for programme in journal.all_programmes():
            trusted, rejected = programme_entries[programme]
            skipped += rejected
            for entry in trusted:
                kind = entry.get("kind")
                if entry.get("cleared_at") or kind == "timebox-signal":
                    continue
                if kind in ("event", "envelope-ended", "held-launch", "wake", "uncertain", "pause", "stop"):
                    continue  # owed work, holds and interlocks are derived from facts on every read (a legacy
                    # hold entry restores nothing)
                if kind == "inbox":
                    if not (_valid_node(entry.get("node"), nodes_with_l1) and
                            _valid_node(entry.get("sender"), nodes_with_l1) and
                            MSG_RE.match(str(entry.get("msg_id") or ""))):
                        skipped += 1
                        continue
                    existing = next((m for m in state.inbox(entry["node"]) if m.get("msg_id") == entry["msg_id"]),
                                    None)
                    if existing is not None:
                        continue
                    provenance = entry.get("provenance") if entry.get("actor") == "ygg-app" else None
                    if not isinstance(provenance, dict) or provenance.get("type") not in ("agent", "quoted-untrusted"):
                        provenance = {"type": "quoted-untrusted", "origin": entry["sender"]}
                    restored_messages.setdefault(entry["node"], []).append({
                        "msg_id": entry["msg_id"], "from": entry["sender"],
                        "kind": str(entry.get("message_kind") or "note"),
                        "body": util.redact(str(entry.get("body") or "")),
                        "ts": entry.get("at"), "provenance": provenance, "acked_at": None})
                elif kind == "checkpoint":
                    if not (_valid_node(entry.get("pm"), nodes) and _valid_node(entry.get("lane"), nodes)):
                        skipped += 1
                        continue
                    # A batch entry is restored from the journal only; one a PM activation was given is
                    # consumed (its confirmed receipt clears it) and never comes back.
                    item = {"lane": entry["lane"], "at": entry.get("at")}
                    if MSG_RE.match(str(entry.get("msg_id") or "")):
                        item["msg_id"] = entry["msg_id"]
                        if was_consumed(entry["pm"], entry["msg_id"]) or \
                                any(i.get("msg_id") == entry["msg_id"] for i in checkpoints.get(entry["pm"], [])):
                            continue
                    if item not in checkpoints.get(entry["pm"], []):
                        checkpoints.setdefault(entry["pm"], []).append(item)
                        checkpoints_changed = True
                elif kind in ("safety-root-established", "resume", "activation", "chain-reset", "t3-close", "t3-project"):
                    pass  # read by the safety folder and the reducer; nothing to restore locally
                else:
                    skipped += 1
        for node_id, items in restored_messages.items():
            existing = {m["msg_id"]: m for m in state.inbox(node_id)}
            for item in items:
                existing.setdefault(item["msg_id"], item)
            state.save_inbox(node_id, sorted(existing.values(), key=lambda m: m["msg_id"]))
        if checkpoints_changed:
            util.write_json(state.root() / "checkpoints.json", checkpoints)
    marker = state.root() / "replay-skipped.json"
    if skipped and util.read_json(marker, {}).get("count") != skipped:
        state.alert("ygg-rehydrate-skipped", "Journal replay skipped " + str(skipped) + " invalid entries",
                    count=skipped)
    if skipped or marker.exists():
        util.write_json(marker, {"count": skipped})
    return skipped


def rehydrate_all() -> str:
    from . import interlock, upgrade
    interlock.complete_stops()  # pending stops first, locally: nothing below may delay a signal
    tracker.pull()  # then a remote tracker's local cache is rebuilt from the tracker alone
    upgrade.run()  # legacy records into their current form, replaced stores deleted, settled stamps compacted
    interlock.reconcile(local=False)  # persist offline intents, finish interrupted resumes
    from . import conductor
    conductor.recover_records()
    skipped = replay_journal()
    count = 0
    for node_id, node in state.all_nodes().items():
        if node.get("project"):
            write_document(node_id)
            count += 1
    return "rehydrated " + str(count) + " node(s); skipped " + str(skipped) + " invalid journal entries\n"
