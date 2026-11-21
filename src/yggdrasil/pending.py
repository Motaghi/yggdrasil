"""`ygg pending`: the user's decision queue."""

from __future__ import annotations

from . import decisions, tracker, util


def build() -> dict:
    items = []
    for project in tracker.projects():
        for issue in tracker.issues(project["id"]):
            if issue.get("kind") == "decision" and decisions.is_open(issue):
                scope = issue.get("scope") or {"type": "project", "id": project["id"]}
                items.append({
                    "id": issue.id, "kind": "relay" if issue.get("relay") else "decision",
                    "programme": project.get("programme"), "project": project["id"],
                    "raised_by": str(issue.get("node") or project["pm"]), "scope": scope,
                    "question": str(issue.get("question") or ""),
                    "options": [{"label": str(o)} for o in issue.get("options") or []],
                    "recommended": issue.get("recommended"), "reversible": issue.get("reversible"),
                    "undo": issue.get("undo"), "aligns": issue.get("aligns"), "action": issue.get("action"),
                    "raised_at": str(issue.get("created_at") or util.iso()),
                    "provenance": issue.get("provenance") or {"type": "agent", "origin": project["pm"]},
                    "relay": issue.get("relay"),
                })
        if project.get("state") == "pid-ready":
            items.append(_gate("pid:" + project["id"], "pid", project, "Approve the PID of " + project["id"] + "?"))
        for stage in tracker.milestones(project["id"]).get("stages") or []:
            if stage.get("state") == "awaiting-approval":
                items.append(_gate("stage:" + project["id"] + ":" + str(stage["number"]), "stage", project,
                                   "Approve stage " + str(stage["number"]) + " of " + project["id"] + "?"))
        for issue in tracker.issues(project["id"]):
            if issue.get("kind") == "exception" and issue.get("state", "open") == "open":
                item = _gate(issue.id, "exception", project, str(issue.get("reason") or ""))
                items.append(item)
    from . import state
    scope = state.visible_programme()
    if scope is not None:
        # A managed caller sees only its own programme's queue.
        items = [item for item in items if item.get("programme") == scope]
    return {"schema": "ygg.pending.v1", "decisions": items}


def _gate(identifier, kind, project, question):
    return {"id": identifier, "kind": kind, "programme": project.get("programme"), "project": project["id"],
            "raised_by": project["pm"], "scope": {"type": "project", "id": project["id"]},
            "question": question, "options": [{"label": "approve"}, {"label": "reject"}],
            "recommended": None, "reversible": None, "undo": None, "aligns": None, "action": None,
            "raised_at": util.iso(), "provenance": {"type": "agent", "origin": project["pm"]}, "relay": None}


def human(data: dict) -> str:
    if not data["decisions"]:
        return "Nothing waiting for you.\n"
    return "".join(item["id"] + ": " + item["question"] + "\n" for item in data["decisions"])
