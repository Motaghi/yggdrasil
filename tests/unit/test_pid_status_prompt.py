"""Once the user approved the PID, the PM's prompt says so, so a fresh PM session doesn't write the PID again and
mark it ready, which sent an approved project back to waiting for approval on every wake (pilot 2 run 4)."""
from __future__ import annotations

from yggdrasil import commands, review

APPROVED = {"schema": "ygg.pid-approved.v1", "project": "demo", "approved_at": "2026-11-21T07:39:39Z", "actor": "user"}


def _setup(monkeypatch, approved, state):
    monkeypatch.setattr(commands, "approved_pid", lambda project: approved)
    monkeypatch.setattr(review.tracker, "project", lambda project: {"id": project, "state": state})


def test_an_approved_project_tells_the_pm_planning_is_done(monkeypatch):
    _setup(monkeypatch, APPROVED, "executing")
    text = review.pid_status_text("demo")
    assert text.startswith("## Plan status")
    assert "The user approved the PID of demo at 2026-11-21T07:39:39Z." in text
    assert "don't write the PID again and don't mark it ready again" in text


def test_nothing_is_said_while_the_pid_waits_or_was_refused(monkeypatch):
    for state in ("initiating", "pid-ready", "pid-rejected"):
        _setup(monkeypatch, APPROVED, state)
        assert review.pid_status_text("demo") == ""
    _setup(monkeypatch, None, "executing")
    assert review.pid_status_text("demo") == ""


def test_the_rulings_section_gives_the_plan_status_to_the_pm_only(monkeypatch):
    _setup(monkeypatch, APPROVED, "executing")
    monkeypatch.setattr(review, "rulings", lambda project=None: [])
    monkeypatch.setattr(review, "open_findings_text", lambda project, lane=None: "")
    for role, shown in (("pm", True), ("tl", False), ("worker", False), ("su", False)):
        text = review.rulings_section({"id": "pm1", "role": role, "project": "demo"}, "act-0001", {})
        assert ("## Plan status" in text) == shown
