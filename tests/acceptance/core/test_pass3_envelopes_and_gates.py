import re

import pytest

from a1_observations import entry, header
from observations import has_label


def ended_envelope(tree, *, pm_report=False):
    tree.at("2026-11-16T02:00:00+01:00")
    tree.ready()
    tree.git_labels()
    tree.branch("ygg/alpha-p1/lane/pm1tl1")
    tree.branch("ygg/alpha-p1/lane/pm1tl2")
    tree.active_envelope()
    tree.report(1)
    tree.run("stage", "end", "alpha-p1", "--within-tolerance", "yes",
             "--report", "end-stage-1.md")
    tree.run("decision", "raise", "--from", "pm1", "--question", "Use placeholder art?",
             "--option", "A", "--option", "B", "--recommended", "A", "--reversible", "yes",
             "--undo", "swap art back in commit range", "--aligns", "yes")
    tree.at("2026-11-16T09:00:00+01:00")
    if pm_report:
        body = ("---\nenvelope: EN-0001\nitems:\n"
                "  - ref: stage-1\n    summary: self-approved stage end\n---\n")
        tree.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1",
                 "--kind", "envelope-report", "--body", body)


@pytest.mark.ac("AC-14")
def test_morning_go_refuses_pm_who_did_the_work(tree):
    ended_envelope(tree)
    result = tree.run("morning-go", "build", "--programme", "alpha", "--reviewer-node", "pm1")
    assert result.returncode == 3
    assert "REFUSED: reviewer did the work" in result.stderr


@pytest.mark.ac("AC-14")
def test_reviewer_morning_go_lists_records_and_reports_missing_pm_decision(tree):
    ended_envelope(tree, pm_report=True)
    tree.run("node", "add", "rv1", "--role", "reviewer", "--parent", "alpha")
    result = tree.run("morning-go", "build", "--programme", "alpha", "--reviewer-node", "rv1")
    assert result.returncode == 0
    items = tree.json_records("initiatives/alpha/morning-go")
    assert len(items) == 2
    for item in items:
        assert item["status"] == "pending-user"
        assert item["commit_ranges"]
        for commit in item["commit_ranges"]:
            assert re.fullmatch(r"[0-9a-f]{40}\.\.[0-9a-f]{40}", commit["range"])
    assert {item["ref"] for item in items} == {"stage-1", "D-0001"}
    documents = [text for _, text in tree.documents("")
                 if "## PM input" in text
                 and "Discrepancy: D-0001 missing from PM report" in text]
    assert documents
    pm_inputs = [match[1] for text in documents
                 if (match := re.search(r"(?ms)^## PM input\s*\n(.*?)(?=^## |\Z)", text))]
    assert any("stage-1" in text and "self-approved stage end" in text for text in pm_inputs)


@pytest.mark.ac("AC-14")
def test_morning_go_rejection_suspends_dependent_package_without_changing_branches(tree):
    ended_envelope(tree, pm_report=True)
    tree.run("node", "add", "rv1", "--role", "reviewer", "--parent", "alpha")
    tree.run("morning-go", "build", "--programme", "alpha", "--reviewer-node", "rv1")
    tree.package("WP-7", criteria=True, authorise=True)
    tree.set_header(tree.tracker / "projects/alpha-p1/issues/WP-7.md", depends_on=["D-0001"])
    item_path = tree.tracker / "initiatives/alpha/morning-go/MG-0002.json"
    item = tree.json_file(item_path)
    assert isinstance(item, dict)
    assert item["ref"] == "D-0001"
    ranges = [commit["range"] for commit in item["commit_ranges"]]
    assert ranges
    before = tree.refs()
    result = tree.run("morning-go", "decide", "MG-0002", "not-ok")
    assert tree.json_file(item_path)["status"] == "rejected"
    assert has_label(tree.documents("projects/alpha-p1/issues"), "rework")
    assert header(tree.tracker_text("projects/alpha-p1/issues/WP-7.md"))["state"] == "suspended"
    assert "revert" in result.stdout.lower()
    for commit_range in ranges:
        assert commit_range in result.stdout
    assert tree.refs() == before


@pytest.mark.ac("AC-53")
def test_user_approves_exception_closes_record_and_queues_pm(tree):
    tree.ready()
    tree.run("exception", "raise", "--from", "pm1", "--reason", "r", "--plan", "p")
    tree.run("exception", "decide", "E-0001", "--plan", "approve")
    assert header(tree.tracker_text("projects/alpha-p1/issues/E-0001.md")).get("state") == "closed"
    status = tree.status()
    assert entry(status, "projects", "alpha-p1")["state"] == "executing"
    assert entry(status, "nodes", "pm1")["state"] == "queued"


@pytest.mark.ac("AC-53")
def test_user_rejects_stage_two_and_delivers_rescope_note_without_starting_stage_three(tree):
    tree.stage_executing(2)
    tree.report(2)
    tree.run("stage", "end", "alpha-p1", "--within-tolerance", "yes", "--report", "end-stage-2.md")
    tree.run("stage", "reject", "alpha-p1", "--note", "rescope stage 3")
    milestones = tree.tracker_json("projects/alpha-p1/milestones.json")
    assert entry(milestones, "stages", 3)["state"] != "executing"
    assert entry(milestones, "stages", 2)["state"] == "rejected"
    assert "rescope stage 3" in tree.run("inbox", "pm1").stdout


@pytest.mark.ac("AC-53")
def test_user_prematurely_closes_project_and_conductor_launches_no_project_node(tree):
    tree.ready()
    tree.run("project", "close", "alpha-p1", "--premature", "--note", "stop work")
    assert entry(tree.status(), "projects", "alpha-p1")["state"] == "closed"
    tree.clear_calls()
    tree.run("conductor", "tick")
    assert not [call for call in tree.launches() if call["env"].get("YGG_PROJECT") == "alpha-p1"]
