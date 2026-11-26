"""Decisions: raise, provisional choice inside envelopes, user answers, blocking scope."""

from __future__ import annotations

from . import state, tracker, util

HARD_STOP_ACTIONS = {"push", "force", "delete", "rewrite-history", "send-external", "spend", "workflow-change"}


def actors() -> list[str]:
    from .commands import authority_actors
    return authority_actors()


def decided_record(issue) -> dict | None:
    allowed = actors()
    found = [r for r in issue.records if r.get("kind") == "decided" and r.get("actor") in allowed]
    return found[-1] if found else None


def is_provisional(issue) -> bool:
    return issue.get("chosen_by") == "envelope" and "provisional-decision" in issue.labels()


def is_open(issue) -> bool:
    if issue.get("kind") != "decision":
        return False
    if is_provisional(issue):
        return False
    return decided_record(issue) is None


def blocked_node(issue) -> str | None:
    scope = issue.get("scope") or {}
    if scope.get("type") == "lane":
        return scope.get("id")
    if scope.get("type") == "package":
        package = tracker.read_issue(issue.project, scope.get("id"))
        return package.get("lane") if package else None
    return issue.get("node")


def lineage(node_id: str, nodes: dict) -> list[str]:
    """The node and its ancestors up to (not including) L1."""
    chain, current = [], node_id
    while current and current in nodes and current not in chain:
        chain.append(current)
        current = nodes[current].get("parent")
    return chain


def blocking(node_id: str, nodes: dict, cache: dict | None = None) -> list[str]:
    """Open decisions that block this node: its own branch, or the branch of any ancestor."""
    node = nodes.get(node_id)
    if not node or not node.get("project"):
        return []
    if node.get("role") == "reviewer":
        return []  # a project reviewer checks finished work; the PM's open decisions don't hold reviews (A11)
    chain = set(lineage(node_id, nodes))
    found = []
    for issue in tracker.issues(node["project"]):
        if is_open(issue) and blocked_node(issue) in chain:
            found.append(issue.id)
    return found


def ignored_authority() -> list[dict]:
    allowed = actors()
    ignored = []
    for project in tracker.projects():
        for issue in tracker.issues(project["id"]):
            for record in issue.records:
                if record.get("kind") in ("decided", "approval", "envelope", "shape", "pid-approval",
                                          "relay-grant") and record.get("actor") not in allowed:
                    ignored.append({"record": issue.id, "actor": str(record.get("actor")),
                                    "reason": "actor not in authority allowlist"})
    import json
    root = tracker.root()
    for path in sorted(list(root.glob("projects/*/envelopes/EN-*.json")) +
                       list(root.glob("initiatives/*/envelopes/EN-*.json"))):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("actor") not in allowed:
            ignored.append({"record": str(data.get("id") or path.stem), "actor": str(data.get("actor")),
                            "reason": "actor not in authority allowlist"})
    return ignored


def _scope(value: str | None, project: str) -> dict:
    if not value:
        return {"type": "project", "id": project}
    kind, _, identifier = value.partition(":")
    if kind == "lane":
        util.check_id(identifier, "lane")
    elif kind == "package":
        util.check_record_id(identifier)
    else:
        raise util.usage("--scope must be lane:<node> or package:<id>")
    return {"type": kind, "id": identifier}


def raise_decision(args) -> str:
    from . import envelopes
    nodes = state.all_nodes()
    state.check_identity(args.sender, nodes)
    node = state.require_node(args.sender, nodes)
    if node["role"] in ("worker", "reviewer") or not node.get("project"):
        raise util.refuse("writer: " + args.sender + " cannot raise decisions")
    options = [util.redact(str(o)) for o in (args.option or [])]
    if len(options) < 2:
        raise util.usage("give at least two --option values")
    args.recommended = util.redact(str(args.recommended))
    args.undo = util.redact(args.undo) if args.undo else args.undo
    if args.recommended not in options:
        raise util.usage("--recommended must be one of the options")
    reversible = args.reversible == "yes"
    if reversible and not (args.undo or "").strip():
        raise util.usage("--reversible yes requires --undo <text>")
    if args.action and args.action not in HARD_STOP_ACTIONS:
        raise util.usage("unknown --action '" + args.action + "'")
    project = tracker.project(node["project"])
    scope = _scope(args.scope, project["id"])
    if scope["type"] == "lane":
        target = nodes.get(scope["id"])
        if not target or target.get("project") != project["id"] or target.get("role") not in ("tl", "su"):
            raise util.usage("unknown lane '" + scope["id"] + "' in project " + project["id"])
    elif scope["type"] == "package":
        package = tracker.read_issue(project["id"], scope["id"])
        if package is None or package.get("kind") != "work-package":
            raise util.usage("unknown work package '" + scope["id"] + "' in project " + project["id"])
    aligns = args.aligns == "yes"
    with util.locked():
        tracker.ensure_writable()
        identifier = tracker.allocate("decision")
        issue = tracker.new_issue(
            project["id"], identifier, "decision", ["decision"], args.sender,
            question=util.redact(args.question), options=options, recommended=args.recommended,
            chosen=None, chosen_by=None, reversible=reversible, undo=args.undo if reversible else None,
            aligns=aligns, scope=scope, action=args.action, target=util.redact(args.target or "") or None,
            provenance={"type": "agent", "origin": args.sender}, raised_with="decision-raise")
        provisional = (reversible and aligns and not args.action
                       and envelopes.effective_envelope(project) is not None)
        if provisional:
            issue.header["chosen"] = args.recommended
            issue.header["chosen_by"] = "envelope"
            issue.add_label("provisional-decision")
        issue.save()
        if provisional:
            envelopes.morning_go_item(project, "provisional-decision", identifier)
    if not provisional:
        state.alert("ygg-decision-waiting", "Decision " + identifier + " waits for you: " + util.redact(args.question),
                    project=project["id"], decision=identifier)  # A11 AC-06 row 3
    return identifier + (" provisional\n" if provisional else " pending\n")


def find(identifier: str):
    util.check_record_id(identifier, "decision")
    for project in tracker.projects():
        issue = tracker.read_issue(project["id"], identifier)
        if issue is not None:
            return issue
    raise util.usage("unknown decision '" + identifier + "'")


def decide(identifier: str, option: str, note: str | None) -> str:
    state.check_user_only("decide")
    note = util.redact(note) if note else note
    with util.locked():
        tracker.ensure_writable()
        issue = find(identifier)
        if issue.get("kind") != "decision":
            raise util.usage(identifier + " is not a decision")
        if option not in [str(o) for o in issue.get("options") or []]:
            raise util.usage("unknown option '" + option + "' for " + identifier)
        issue.header["chosen"] = option
        issue.header["chosen_by"] = "user"
        issue.add_label("decided")
        issue.records.append(tracker.record("decided", None, actor="user", option=option, note=note))
        text = "Decided: " + option + "." + (" Note: " + note if note else "")
        issue.comments.append(tracker.record("decided-comment", None, actor="user", body=text))
        tracker.owe(issue.header, issue.get("node"), "decided:" + identifier)
        issue.save()
    return identifier + " decided: " + option + "\n"
