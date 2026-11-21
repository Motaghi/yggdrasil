"""`ygg summary --since <span>` (A11 lane 4: AC-07, AC-44; release switch `summary`).

For each project with a change in the span: the packages that reached Done (`accepted`, `merged`) and Being
checked (`in-review`, `ready-for-test`), then what waits for the user (open decisions). A package counts as
changed when its newest time (created, a record, a comment or `updated_at`) falls in the span. With nothing in
the span: `No change in the last hour.` Titles go on one line, cut to 60 characters plus `...`.
"""

from __future__ import annotations

import re
from datetime import timedelta

from . import release, state, tracker, util

DONE = ("accepted", "merged")
CHECKING = ("in-review", "ready-for-test")
TITLE_LIMIT = 60
SPAN = re.compile(r"^(\d+)\s*([mhd])$")


def parse_span(text: str | None) -> timedelta:
    match = SPAN.match((text or "1h").strip().lower())
    if not match or int(match[1]) <= 0:
        raise util.usage("--since must be a span such as 30m, 1h or 2d, not '" + str(text) + "'")
    number, unit = int(match[1]), match[2]
    return timedelta(minutes=number) if unit == "m" else timedelta(hours=number) if unit == "h" else \
        timedelta(days=number)


def span_words(span: timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    if minutes == 60:
        return "the last hour"
    if minutes % (24 * 60) == 0:
        days = minutes // (24 * 60)
        return "the last day" if days == 1 else "the last " + str(days) + " days"
    if minutes % 60 == 0:
        return "the last " + str(minutes // 60) + " hours"
    return "the last minute" if minutes == 1 else "the last " + str(minutes) + " minutes"


def short_title(value) -> str:
    text = util.redact(str(value or "")).replace("\r\n", " ").replace("\n", " ")
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return text[:TITLE_LIMIT] + "..." if len(text) > TITLE_LIMIT else text


def _times(issue) -> list:
    found = []
    for value in [issue.get("created_at"), issue.get("updated_at"),
                  *(r.get("at") for r in issue.records), *(c.get("at") for c in issue.comments)]:
        if not value:
            continue
        try:
            found.append(util.parse_time(str(value)))
        except ValueError:
            continue
    return found


def _changed(issue, since) -> bool:
    return any(moment >= since for moment in _times(issue))


def _project_block(project: dict, since, words: str) -> list[str] | None:
    from . import decisions
    done, checking, waiting = [], [], 0
    changed = False
    for issue in tracker.issues(project["id"]):
        if issue.get("kind") == "work-package":
            if not _changed(issue, since):
                continue
            changed = True
            label = issue.id + (" " + short_title(issue.get("title")) if issue.get("title") else "")
            if issue.get("state") in DONE:
                done.append(label)
            elif issue.get("state") in CHECKING:
                checking.append(label)
        elif issue.get("kind") == "decision" and decisions.is_open(issue):
            waiting += 1
            changed = changed or _changed(issue, since)
    if not (changed or waiting) or not (done or checking or waiting):
        return None
    lines = [project["id"] + " in " + words + ":"]
    if done:
        lines.append("Done: " + "; ".join(done) + ".")
    if checking:
        lines.append("Being checked: " + "; ".join(checking) + ".")
    if waiting == 0:
        lines.append("Nothing needs you.")
    elif waiting == 1:
        lines.append("1 decision waits for you: run ygg pending.")
    else:
        lines.append(str(waiting) + " decisions wait for you: run ygg pending.")
    return lines


def build(since_text: str | None) -> str:
    release.refuse("summary")
    span = parse_span(since_text)
    since = util.now() - span
    words = span_words(span)
    scope = state.visible_programme()
    lines: list[str] = []
    for project in tracker.projects():
        if scope is not None and project.get("programme") != scope:
            continue
        if project.get("state") in ("initiating", "pid-ready", "pid-rejected"):
            continue
        block = _project_block(project, since, words)
        if block:
            lines.extend(block)
    if not lines:
        return "No change in " + words + ".\n"
    return "\n".join(lines) + "\n"
