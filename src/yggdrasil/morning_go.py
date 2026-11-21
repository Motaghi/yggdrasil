"""Morning go: build the reviewer's document from tracker records, and the user's ok or not-ok."""

from __future__ import annotations

import re

from . import state, tracker, util


def item_path(programme: str, identifier: str):
    return tracker.root() / "initiatives" / programme / "morning-go" / (identifier + ".json")


def items(programme: str) -> list[dict]:
    base = tracker.root() / "initiatives" / programme / "morning-go"
    found = []
    if base.is_dir():
        for path in sorted(base.glob("MG-*.json")):
            data = util.read_json(path)
            if isinstance(data, dict):
                found.append(data)
    return found


def newest_report(project_id: str) -> tuple[str | None, dict | None]:
    base = tracker.project_dir(project_id) / "documents"
    reports = sorted(base.glob("envelope-report-*.md")) if base.is_dir() else []
    if not reports:
        return None, None
    path = reports[-1]
    header, _ = util.split_frontmatter(util.read_text(path))
    return path.stem, header or {}


def build(programme: str, reviewer: str) -> str:
    util.check_id(programme, "programme id")
    nodes = state.all_nodes()
    node = state.require_node(reviewer, nodes)
    caller = util.current_node()
    if caller is not None and caller != reviewer:
        raise util.refuse("identity: " + caller + " may not build the morning go as " + reviewer)
    target = tracker.programme(programme)
    if target is None:
        raise util.usage("unknown programme '" + programme + "'")
    if node["role"] != "reviewer":
        raise util.refuse("reviewer did the work: " + reviewer + " is a " + node["role"] +
                          " node and cannot write the morning go")
    if node.get("programme") != programme:
        if node.get("domain") != target.get("domain"):
            raise util.refuse("cross-domain: reviewer " + reviewer + " belongs to " + str(node.get("programme")))
        raise util.refuse("reviewer: " + reviewer + " reviews programme " + str(node.get("programme")) + " only")
    pending = [i for i in items(programme) if i.get("status") == "pending-user"]
    from . import workahead
    for item in pending:
        if item.get("kind") == "work-ahead":
            # Taken now: the lane branch from the envelope snapshot to its head at build time (A4.1).
            item["commit_ranges"], item["flags"] = workahead.lane_ranges(item)
            with util.locked():
                tracker.write_json(item_path(programme, item["id"]), item)
    lines = ["# Morning go for " + programme, "", "## Items", ""]
    for item in pending:
        ranges = ", ".join(c["branch"] + " " + c["range"] for c in item.get("commit_ranges") or []) or "none"
        lines.append("- " + item["id"] + " " + item["kind"] + " " + str(item.get("ref")) + " (" +
                     str(item.get("project")) + "): " + ranges)
        for flag in item.get("flags") or []:
            lines.append("  - FLAG: " + flag)
    lines += ["", "## PM input", ""]
    for project in sorted({i.get("project") for i in pending if i.get("project")}):
        name, report = newest_report(project)
        if report is None:
            lines.append("Discrepancy: no PM envelope report")
            continue
        refs = set()
        for entry in report.get("items") or []:
            if isinstance(entry, dict):
                refs.add(str(entry.get("ref")))
                lines.append("- " + str(entry.get("ref")) + ": " + util.redact(str(entry.get("summary") or "")))
        for item in pending:
            if item.get("project") == project and str(item.get("ref")) not in refs:
                lines.append("Discrepancy: " + str(item.get("ref")) + " missing from PM report")
    if not pending:
        lines.append("No items.")
    header = {"kind": "morning-go", "programme": programme, "reviewer": reviewer,
              "items": [i["id"] for i in pending], "standard_version": util.STANDARD_VERSION,
              "actor": "ygg-app", "node": reviewer, "created_at": util.iso()}
    with util.locked():
        tracker.ensure_writable()
        path = tracker.root() / "initiatives" / programme / "documents" / ("morning-go-" + util.compact() + ".md")
        tracker.write_text(path, util.render_frontmatter(header, util.escape_sections("\n".join(lines) + "\n")))
    return "morning go written with " + str(len(pending)) + " item(s)\n"


def decide(identifier: str, verdict: str) -> str:
    with util.locked():
        state.check_user_only("morning-go decide")
        if not re.fullmatch(r"MG-[0-9]{4,9}", identifier or ""):
            raise util.usage("invalid morning-go id '" + str(identifier) + "'")
        if verdict not in ("ok", "not-ok"):
            raise util.usage("verdict must be ok or not-ok")
        found = None
        for programme in tracker.programmes():
            path = item_path(programme["id"], identifier)
            if path.exists():
                found = (path, util.read_json(path))
        if not found or not isinstance(found[1], dict):
            raise util.usage("unknown morning-go item '" + identifier + "'")
        path, item = found
        flags = item.get("flags") or []
        output = []
        with util.locked():
            tracker.ensure_writable()
            item["status"] = "accepted" if verdict == "ok" else "rejected"
            item["decided_at"] = util.iso()
            item["decided_by"] = "user"
            tracker.write_json(path, item)
            if verdict == "not-ok" and item.get("project"):
                project = item["project"]
                rework = tracker.allocate("issue")
                record = tracker.new_issue(project, rework, "rework", ["rework"], None, actor="user",
                                           ref=item.get("ref"), morning_go=identifier,
                                           commit_ranges=item.get("commit_ranges") or [])
                record.save()
                suspended = []
                for issue in tracker.issues(project):
                    target = item.get("kind") == "work-ahead" and issue.id == item.get("ref")
                    if issue.get("kind") == "work-package" and (target or item.get("ref") in (issue.get("depends_on") or [])):
                        issue.header["state"] = "suspended"
                        issue.save()
                        suspended.append(issue.id)
                output.append("Rework " + rework + " recorded; suspended: " + (", ".join(suspended) or "none"))
                output.append("Revert plan (a hard stop: it runs only when you say so):")
                for commit in item.get("commit_ranges") or []:
                    output.append("  git -C " + commit["repo"] + " revert --no-edit " + commit["range"] +
                                  "   # " + commit["branch"])
        if flags:
            output.append("Flags on " + identifier + ": " + "; ".join(flags))
        output.insert(0, identifier + " " + item["status"])
        return "\n".join(output) + "\n"
