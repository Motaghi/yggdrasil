"""Ratatoskr: the message CLI with identity, topology and domain rules."""

from __future__ import annotations

import re

from . import state, tracker, util
from .util import YggError

# Message kinds that never wake a PM at once (batched checkpoints).
BATCHED_TO_PM = {"checkpoint"}
DISPATCH = "dispatch:"  # the reason prefix of a lane dispatch stamp on an authorised package


def _log(entry: dict) -> None:
    util.append_line(state.root() / "messages.jsonl", entry)


def topology_reason(sender: dict, recipient: dict) -> str | None:
    if sender.get("role") == "reviewer" or recipient.get("role") == "reviewer":
        return "topology"
    related = (
        recipient["id"] == sender.get("parent")
        or recipient.get("parent") == sender["id"]
        or (recipient.get("parent") is not None and recipient.get("parent") == sender.get("parent"))
    )
    if sender["id"] == recipient["id"]:
        related = False
    if not related:
        return "topology"
    if sender.get("domain") and recipient.get("domain") and sender["domain"] != recipient["domain"]:
        return "cross-domain"
    return None


def send(sender_id: str, recipient_id: str, kind: str, body: str, *, relay_of: str | None = None,
         internal: bool = False, origin: str | None = None, dispatch: str | None = None,
         urgent: bool = False) -> str:
    nodes = state.all_nodes()
    body = util.redact(body)
    if urgent and not internal:
        # AC-55: only the user or the PM sends urgent messages.
        if (state.get_node(sender_id, nodes) or {}).get("role") != "pm":
            raise util.refuse("only the user or the PM sends urgent messages")
    with util.locked():
        tracker.ensure_writable()
        if dispatch:
            sent = _dispatched(recipient_id).get(dispatch)
            if sent:
                return sent  # already delivered for this stamp: delivery is idempotent
            if _stamp_given(recipient_id, dispatch):
                # An activation was already given the stamp (consumed, or reserved while it runs): a late
                # message would only create launch work that is already settled. Checked under the lock.
                return ""
        msg_id = tracker.allocate("message")
        entry = {"ts": util.iso(), "from": sender_id, "to": recipient_id, "kind": kind, "msg_id": msg_id,
                 "body": body, "provenance": {"type": "agent", "origin": origin or sender_id}}
        if relay_of:
            entry["relay_of"] = relay_of
        if urgent:
            entry["urgent"] = True
        try:
            if not internal:
                try:
                    state.check_identity(sender_id, nodes)
                except YggError:
                    entry.update(outcome="refused", reason="identity")
                    raise
                sender = state.get_node(sender_id, nodes)
                recipient = state.get_node(recipient_id, nodes)
                if sender is None or recipient is None:
                    entry.update(outcome="refused", reason="unknown-node")
                    missing = sender_id if sender is None else recipient_id
                    raise util.usage("unknown node '" + missing + "'")
                reason = topology_reason(sender, recipient)
                if reason:
                    entry.update(outcome="refused", reason=reason)
                    raise util.refuse(reason + ": " + sender_id + " may not message " + recipient_id)
                if relay_of:
                    source_domain, origin_node = relay_source(relay_of, nodes)
                    if source_domain is None:
                        entry.update(outcome="refused", reason="unknown-relay")
                        raise util.usage("unknown message '" + relay_of + "' for --relay-of")
                    entry["provenance"] = {"type": "quoted-untrusted", "origin": origin_node}
                    target_domain = recipient.get("domain")
                    if target_domain and source_domain != target_domain:
                        if not relay_granted(source_domain, target_domain):
                            entry.update(outcome="refused", reason="relay-needs-user")
                            relay_decision(relay_of, source_domain, target_domain, recipient, sender_id)
                            raise util.refuse("relay-needs-user: relaying " + relay_of + " from " +
                                              source_domain + " to " + target_domain + " needs the user")
                        entry["cross_domain"] = True
        except YggError:
            _log(entry)
            raise
        deliver(recipient_id, msg_id, sender_id, kind, body, entry["provenance"], dispatch=dispatch, urgent=urgent)
        entry["outcome"] = "delivered"
        _log(entry)
    if urgent:
        from . import t3
        live = getattr(t3, "live_send", None)  # (A11 AC-36): an urgent message also reaches a live thread
        if live is not None:
            try:
                live(dict(entry, body=body))
            except YggError:
                pass
    # The inbox fact is the obligation itself: an unacknowledged message is owed work until a spawned
    # activation is given it (a report to a PM only once its batch is due). No separate wake is recorded.
    return msg_id


def _programme_of(node_id: str):
    node = state.get_node(node_id) or {}
    return node.get("programme")


def _dispatched(recipient_id: str) -> dict:
    """dispatch stamp id -> message id, from every trusted inbox entry (acknowledged ones included)."""
    from . import journal
    trusted, _ = journal.trusted_entries(_programme_of(recipient_id))
    return {str(e["dispatch"]): str(e["msg_id"]) for e in trusted if e.get("kind") == "inbox" and e.get("dispatch")}


def _stamp_given(node_id: str, stamp_id: str) -> bool:
    from . import reducer
    activations: dict = {}
    for programme_fold in reducer.folds().values():
        activations.update(programme_fold["activations"])
    consumed, reserved = reducer.consumption(activations)
    return (node_id, "record:" + stamp_id) in consumed | reserved


def deliver_dispatches(project_id: str | None = None) -> None:
    """Send the work message of every lane dispatch stamp that has no delivered message yet (the stamp is
    written with the authorisation; this completes it now or after a crash)."""
    for project in tracker.projects():
        if project_id and project["id"] != project_id:
            continue
        for issue in tracker.issues(project["id"]):
            for stamp in issue.header.get("owed") or []:
                if not isinstance(stamp, dict) or not str(stamp.get("reason", "")).startswith(DISPATCH):
                    continue
                lane = str(stamp.get("node"))
                if stamp.get("id") in _dispatched(lane) or _stamp_given(lane, str(stamp["id"])):
                    continue
                title = issue.get("title") or issue.id
                send(project["pm"], lane, "work", "Work package " + issue.id + " authorised: " + str(title),
                     internal=True, dispatch=str(stamp["id"]))


def deliver(recipient_id: str, msg_id: str, sender_id: str, kind: str, body: str, provenance: dict,
            dispatch: str | None = None, urgent: bool = False) -> None:
    """Deliver a message. A checkpoint to a PM registers its batch entry in the same locked journal write as
    its inbox entry (A1.4), so no acknowledgement, tick or confirmation can run between the two."""
    from . import events, journal
    with util.locked():
        moment = util.iso()
        records = [("inbox", dict({"node": recipient_id, "msg_id": msg_id, "sender": sender_id,
                                   "message_kind": kind, "body": body, "provenance": provenance},
                                  **({"dispatch": dispatch} if dispatch else {}),
                                  **({"urgent": True} if urgent else {})))]
        batched = kind in BATCHED_TO_PM and (state.get_node(recipient_id) or {}).get("role") == "pm" \
            and sender_id != recipient_id
        if batched:
            records.append(("checkpoint", {"pm": recipient_id, "lane": sender_id, "msg_id": msg_id}))
        journal.add_many(_programme_of(recipient_id), records, at=moment)
        items = state.inbox(recipient_id)
        item = {"msg_id": msg_id, "from": sender_id, "kind": kind, "body": body,
                "ts": moment, "provenance": provenance, "acked_at": None}
        if urgent:
            item["urgent"] = True
        items.append(item)
        state.save_inbox(recipient_id, items)
        if batched:
            events.add_local_checkpoint(recipient_id, sender_id, msg_id, moment)


def inbox(node_id: str, ack: str | None = None) -> str:
    util.check_id(node_id, "node id", allow_reserved=True)
    caller = util.current_node()
    if ack is not None and caller is not None and caller != node_id:
        # AC-88 row 2: only the recipient or the user acks.
        raise util.refuse("identity: only " + node_id + " acknowledges its own messages")
    if caller is not None and caller != node_id:
        nodes = state.all_nodes()
        if (nodes.get(node_id) or {}).get("parent") != caller:
            raise util.refuse("identity: " + caller + " may not read the inbox of " + node_id)
    if ack is not None and not re.fullmatch(r"m-[0-9]{4,9}", ack):
        raise util.usage("invalid message id '" + ack + "'")
    if ack:
        with util.locked():
            items = state.inbox(node_id)
            found = False
            for item in items:
                if item["msg_id"] == ack and not item.get("acked_at"):
                    item["acked_at"] = util.iso()
                    if util.env("YGG_VIA_RELAY"):
                        item["read_via_relay"] = util.env("YGG_VIA_RELAY")
                    bound_to = _running_activation(node_id)
                    if bound_to is not None:
                        item["bound"] = bound_to  # back to unread if that activation doesn't consume it
                    found = True
            if not found:
                raise util.usage("no unacknowledged message " + ack + " for " + node_id)
            from . import events, journal
            programme = _programme_of(node_id)
            bound = _running_activation(node_id)
            if bound is not None:
                # AC-29: an ack by the running activation adds the message to that activation's work, which
                # follows the consumption rules (exit 0 consumes it; a signal, a failure or an uncertain exit
                # consumes nothing).
                journal.replace(programme, "inbox", {"node": node_id, "msg_id": ack},
                                **_bound_entry(programme, node_id, ack, bound))
                from . import launcher
                given = dict((launcher.receipt(bound) or {}).get("consumed") or {})
                given["messages"] = list(given.get("messages") or []) + [ack]
                launcher.update_receipt(bound, consumed=given)
                state.save_inbox(node_id, items)
                events.drop_checkpoint(node_id, ack)
                return "acknowledged " + ack + "\n"

            def covered(e: dict) -> bool:
                # One durable write: the inbox entry and the checkpoint's batch entry. Owed work and holds
                # are derived, so acknowledging the message is all it takes to stop owing it.
                if e.get("kind") == "checkpoint":
                    return e.get("pm") == node_id and e.get("msg_id") == ack
                return e.get("node") == node_id and e.get("kind") == "inbox" and e.get("msg_id") == ack

            journal.clear_where(programme, covered)
            state.save_inbox(node_id, items)
            events.drop_checkpoint(node_id, ack)
        return "acknowledged " + ack + "\n"
    lines = []
    from . import workahead
    # Unread messages, and (v1.1.1, CB-04 and CB-17) the ones a Codex agent read through the relay, marked so.
    listed = [item for item in state.inbox(node_id) if not item.get("acked_at") or item.get("read_via_relay")]
    listed.sort(key=lambda item: 0 if item.get("urgent") else 1)  # urgent first (AC-52), stable otherwise
    for item in listed:
        if workahead.hidden(node_id, item):
            continue  # refers to a blocked later-stage package: not shown until it may start
        kind = item["kind"] + (", urgent" if item.get("urgent") else "") + (", read" if item.get("acked_at") else "")
        lines.append(item["msg_id"] + " from " + item["from"] + " [" + kind + "]\n"
                     + "```text\n" + item["body"] + "\n```")
    return ("\n".join(lines) + "\n") if lines else ""


def seed_inbox(node_id: str, count: int) -> str:
    """`ygg test seed-inbox <node> --acked <n>` (A11 AC-53 row 2, amendment 25): n acknowledged messages written to
    the node's local inbox store in one call. Test mode only, never from a managed session."""
    if not util.test_mode() or state.managed_session():
        raise util.refuse("test-mode: fixture commands need YGG_TEST_MODE=1 and the user's own session")
    util.check_id(node_id, "node id")
    state.require_node(node_id)
    if count < 0 or count > 1000000:
        raise util.usage("--acked must be between 0 and 1000000")
    moment = util.iso()
    with util.locked():
        items = state.inbox(node_id)
        start = 900000000
        for number in range(count):
            items.append({"msg_id": "m-" + str(start + number), "from": "user", "kind": "work", "body": "seeded",
                          "ts": moment, "provenance": {"type": "agent", "origin": "user"}, "acked_at": moment})
        state.save_inbox(node_id, items)
    return "seeded " + str(count) + " acknowledged messages for " + node_id + "\n"


def unbind_unconsumed() -> None:
    """A message acked by a running activation that then ended without consuming it (a failure, a crash, a
    stop) is unread again, so the next run is given it and status shows it owed (A11 AC-29, A11.1 AC-02)."""
    from . import reducer
    activations: dict = {}
    for programme_fold in reducer.folds().values():
        activations.update(programme_fold["activations"])
    consumed, _ = reducer.consumption(activations)
    base = state.root() / "inboxes"
    if not base.is_dir():
        return
    for path in sorted(base.glob("*.json")):
        node_id = path.stem
        items = state.inbox(node_id)
        changed = False
        for item in items:
            bound = item.get("bound")
            if not bound or not item.get("acked_at"):
                continue
            record = activations.get(str(bound))
            if record is None or record.get("terminal") is None:
                continue
            if (node_id, "message:" + str(item["msg_id"])) in consumed:
                item.pop("bound", None)
                changed = True
                continue
            item["acked_at"] = None
            item.pop("bound", None)
            item.pop("read_via_relay", None)
            changed = True
        if changed:
            with util.locked():
                state.save_inbox(node_id, items)


def _running_activation(node_id: str) -> str | None:
    """The activation acking, when it is the node's running one (YGG_ACTIVATION names it)."""
    activation = util.env("YGG_ACTIVATION")
    if not activation or util.current_node() != node_id:
        return None
    runtime = state.runtime(node_id)
    if runtime.get("activation") != activation:
        return None
    from . import launcher
    data = launcher.receipt(activation) or {}
    if data.get("exited_at") is not None:
        return None
    return activation


def _bound_entry(programme, node_id: str, msg_id: str, activation: str) -> dict:
    from . import journal
    trusted, _ = journal.trusted_entries(programme)
    found = next((dict(e) for e in reversed(trusted) if e.get("kind") == "inbox" and e.get("node") == node_id
                  and e.get("msg_id") == msg_id and not e.get("cleared_at")), None)
    fields = {k: v for k, v in (found or {"node": node_id, "msg_id": msg_id}).items()
              if k not in ("kind", "at", "actor", "cleared_at")}
    fields["bound"] = activation
    return fields


def relay_source(msg_id: str, nodes: dict):
    if not re.fullmatch(r"m-[0-9]{4,9}", msg_id or ""):
        return None, None
    for line in util.read_lines(state.root() / "messages.jsonl"):
        if line.get("msg_id") == msg_id and line.get("outcome") == "delivered":
            origin = (line.get("provenance") or {}).get("origin") or line.get("from")
            node = nodes.get(origin) or nodes.get(line.get("from")) or {}
            return node.get("domain"), origin
    return None, None


def relay_grants() -> list[dict]:
    data = util.read_json(tracker.root() / "_ygg" / "relay-grants.json", {}) or {}
    return list(data.get("grants") or [])


def relay_granted(source: str, target: str) -> bool:
    from .commands import authority_actors
    allowed = authority_actors()
    return any(g.get("from_domain") == source and g.get("to_domain") == target and g.get("actor") in allowed
               for g in relay_grants())


def relay_decision(msg_id: str, source: str, target: str, recipient: dict, sender_id: str) -> None:
    project = recipient.get("project")
    if not project:
        return
    for issue in tracker.issues(project):
        relay = issue.get("relay") or {}
        if issue.get("kind") == "decision" and relay.get("msg_id") == msg_id and not issue.get("chosen"):
            return
    identifier = tracker.allocate("decision")
    issue = tracker.new_issue(project, identifier, "decision", ["decision", "relay"], sender_id,
                              question="Relay " + msg_id + " from " + source + " to " + target + "?",
                              options=["relay", "hold"], recommended=None, chosen=None, chosen_by=None,
                              reversible=None, undo=None, aligns=None, scope={"type": "project", "id": project},
                              action=None, target=None, provenance={"type": "agent", "origin": sender_id},
                              relay={"msg_id": msg_id, "from_domain": source, "to_domain": target})
    issue.save()


def relay_grant(source: str, target: str, revoke: bool = False) -> str:
    state.check_user_only("relay revoke" if revoke else "relay grant")
    util.check_id(source, "domain")
    util.check_id(target, "domain")
    with util.locked():
        tracker.ensure_writable()
        grants = [g for g in relay_grants() if not (g.get("from_domain") == source and g.get("to_domain") == target)]
        if not revoke:
            grants.append({"from_domain": source, "to_domain": target, "actor": "user", "at": util.iso()})
        tracker.write_json(tracker.root() / "_ygg" / "relay-grants.json", {"grants": grants})
    return ("relay revoked " if revoke else "relay granted ") + source + " -> " + target + "\n"
