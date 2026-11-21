"""A reviewer is told the literal verdict path, not only $YGG_REVIEW_DIR: Claude Code's Write tool doesn't expand
environment variables and the reviewer's lane can't print them (pilot 2 run 2, act-0019)."""
from __future__ import annotations

from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


def test_the_review_section_names_the_literal_verdict_file(world, monkeypatch):
    from yggdrasil import launcher, permissions
    activation = "act-0042"
    record = {"cwd": "", "review": {"id": "rv-0001", "package": "WP-1", "project": "demo", "base": None,
                                    "head": "abc1234"}}
    monkeypatch.setattr(launcher, "receipt", lambda _activation: record)
    text = launcher.review_section({"role": "reviewer"}, activation, {})
    assert str(permissions.review_dir(activation) / "verdict.json") in text
    assert "$YGG_REVIEW_DIR/verdict.json" in text


def test_the_review_assignment_names_the_literal_verdict_file(world, monkeypatch):
    from yggdrasil import permissions, review
    activation = "act-0042"
    assigned = {"id": "rv-0001", "package": "WP-1", "lane": "tl1", "round": 1, "base": "a1", "head": "b2",
                "project": "demo"}
    monkeypatch.setattr(review, "assigned_to", lambda _node, _activation=None: assigned)
    monkeypatch.setattr(review.tracker, "read_issue", lambda _project, _package: None)
    text = review._assignment_text({"id": "pm1rv1"}, activation)
    assert str(permissions.review_dir(activation) / "verdict.json") in text
    assert "$YGG_REVIEW_DIR/verdict.json" in text
