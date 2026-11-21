"""Agile backlog ordering (Amendment A2.1)."""

from __future__ import annotations

from . import state, tracker, util


def order(project_id: str, order_text: str, rationale: str | None, as_node: str | None, theme: str | None) -> str:
    with util.locked():
        if not (rationale or "").strip():
            raise util.usage("backlog order needs a non-empty --rationale")
        from .commands import _project_for
        project = _project_for(project_id)
        caller = util.current_node()
        nodes = state.all_nodes()
        if theme is not None and caller is not None:
            raise util.refuse("user-only: --theme is set only by the user")
        if as_node is not None:
            state.check_identity(as_node, nodes)
            node = nodes.get(as_node)
            if not node or node.get("project") != project_id:
                raise util.refuse("writer: " + as_node + " is not a node of " + project_id)
            role = node["role"]
        elif caller is not None:
            raise util.refuse("identity: a managed session must name itself with --as")
        else:
            role = "user"
        if role not in ("su", "pm", "user"):
            raise util.refuse("writer: only the senior user, the PM or the user orders the backlog")
        text = tracker.read_document(project_id, "backlog")
        if not text:
            raise util.usage("project " + project_id + " has no backlog document")
        doc = tracker.parse_issue(project_id, "backlog", text)
        items = list(doc.header.get("items") or [])
        known = [str(i.get("id")) for i in items]
        wanted = [part.strip() for part in (order_text or "").split(",") if part.strip()]
        for identifier in wanted:
            if identifier not in known:
                raise util.usage("unknown backlog id '" + identifier + "'")
        for identifier in known:
            if identifier not in wanted:
                raise util.usage("backlog id '" + identifier + "' is missing from --order")
        status = "proposed" if role == "su" else "applied"
        actor = "user" if role == "user" else as_node
        with util.locked():
            tracker.ensure_writable()
            if status == "applied":
                position = {identifier: index for index, identifier in enumerate(wanted)}
                doc.header["items"] = sorted(items, key=lambda i: position[str(i.get("id"))])
            if theme is not None:
                doc.header["themes"] = list(doc.header.get("themes") or []) + [util.redact(theme)]
            doc.records.append({"kind": "backlog-order", "at": util.iso(), "actor": actor, "order": wanted,
                                "rationale": util.redact(rationale), "status": status,
                                "standard_version": util.STANDARD_VERSION})
            if status == "proposed":
                tracker.owe(doc.header, project["pm"], "backlog-proposal")
            from .commands import _render_doc
            tracker.write_text(tracker.document_path(project_id, "backlog"), _render_doc(doc))
        return "backlog order " + status + "\n"
