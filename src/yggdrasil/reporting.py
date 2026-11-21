"""Daily digest and North Star refusals."""

from __future__ import annotations

import re
from pathlib import Path

from . import config, state, tracker, util


def digest(date: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date or ""):
        raise util.usage("--date must be yyyy-mm-dd")
    from . import pending
    waiting = len(pending.build()["decisions"])
    profile = config.profile() or {}
    lines = ["# Yggdrasil digest " + date, "", "Decisions waiting: " + str(waiting), ""]
    index_path = state.root() / "digests" / "index.json"
    index = util.read_json(index_path, {}) or {}
    since = max((at for day, at in index.items() if day < date), default=None)
    for project in tracker.projects():
        lines.append("## " + project["id"])
        lines.append("State: " + str(project.get("state")))
        text = tracker.read_document(project["id"], "route-map")
        _, body = util.split_frontmatter(text) if text else (None, "")
        frontier = util.bullets(body or "", "Frontier")
        lines.append("Frontier:")
        lines += ["- " + item for item in frontier] or ["- none"]
        lines += work_ahead_lines(project, since)
        lines.append("")
    header = {"date": date, "status": "open", "type": "digest",
              "description": "Yggdrasil daily digest for " + date}
    text = util.render_frontmatter(header, "\n".join(lines))
    if profile.get("digest_dir"):
        name = str(profile.get("digest_filename") or "{date}.md").replace("{date}", date)
        if "/" in name or name.startswith("."):
            raise util.usage("digest_filename must be a plain file name")
        path = Path(str(profile["digest_dir"])) / name
    else:
        path = state.root() / "digests" / (date + ".md")
    util.atomic_write(path, text)
    index[date] = util.iso()
    util.write_json(index_path, index)
    return "digest written to " + str(path) + "\n"


def work_ahead_lines(project: dict, since: str | None) -> list[str]:
    """One line per work-ahead launch since the previous digest (Amendment A4)."""
    from . import morning_go
    lines = []
    for item in morning_go.items(project["programme"]):
        if item.get("kind") != "work-ahead" or item.get("project") != project["id"]:
            continue
        launches = item.get("launches") or [{"started_at": item.get("started_at") or item.get("created_at")}]
        for launch in launches:
            started = launch.get("started_at")
            if since and started and util.parse_time(str(started)) <= util.parse_time(str(since)):
                continue
            lines.append("Work ahead: " + str(item.get("ref")) + " (stage " + str(item.get("stage")) + ") started " +
                         str(started))
    return lines


def _refusals(text: str) -> list[str]:
    _, body = util.split_frontmatter(text) if text else (None, "")
    return util.bullets(body or "", "Refusals")


def _counts(text: str) -> bool:
    from .commands import authority_actors
    header, _ = util.split_frontmatter(text) if text else (None, "")
    return bool(header) and header.get("actor") in set(authority_actors()) | {"ygg-app"}


def northstar_resolve(project_id: str) -> str:
    from .commands import _project_for, pid_document
    project = _project_for(project_id)
    programme_text = util.read_text(tracker.root() / "initiatives" / project["programme"] / "documents" /
                                    "northstar.md")
    result = list(_refusals(programme_text)) if _counts(programme_text) else []
    project_text = tracker.read_document(project_id, "northstar")
    for item in _refusals(project_text):
        if item not in result:
            result.append(item)
    if tracker.read_document(project_id, "pid"):
        for item in pid_document(project_id).header.get("refusals") or []:
            if str(item) not in result:
                result.append(str(item))
    return "## Refusals\n" + "".join("- " + item + "\n" for item in result)


def northstar_add_refusal(project_id: str, text: str) -> str:
    from .commands import _project_for
    project = _project_for(project_id)
    caller = util.current_node()
    if caller is not None and caller != project["pm"]:
        raise util.refuse("writer: only the PM or the user adds project refusals")
    refusal = util.redact(text or "").strip().replace("\n", " ")
    if not refusal:
        raise util.usage("--text must not be empty")
    with util.locked():
        tracker.ensure_writable()
        current = tracker.read_document(project_id, "northstar")
        items = _refusals(current)
        if refusal not in items:
            items.append(refusal)
        header = {"schema": "ygg.northstar.v1", "scope": "project", "project": project_id,
                  "actor": "user" if caller is None else "ygg-app", "standard_version": util.STANDARD_VERSION}
        body = "## Refusals\n" + "".join("- " + item + "\n" for item in items)
        tracker.write_document(project_id, "northstar", util.render_frontmatter(header, body))
    return "refusal added to " + project_id + "\n"
