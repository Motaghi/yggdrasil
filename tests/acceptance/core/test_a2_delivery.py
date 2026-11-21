import pytest

from a1_observations import alerts, entry, header, records


@pytest.mark.ac("AC-70")
def test_pid_without_delivery_settings_uses_core_agile_flow_defaults(tree):
    assert tree.run("pid", "approve", "alpha-p1").returncode == 0
    delivery = entry(tree.status(), "projects", "alpha-p1")["delivery"]
    assert delivery["approach"] == "agile"
    assert delivery["cadence"] == "flow"
    assert delivery["source"] == "core"


@pytest.mark.ac("AC-70")
def test_pid_without_delivery_approach_uses_profile_waterfall_default(tree):
    profile = tree.root / "profile-waterfall.yaml"
    tree.write_yaml(profile, {"name": "example", "delivery_approach_default": "waterfall"})
    tree.env["YGG_PROFILE"] = str(profile)
    assert tree.run("pid", "approve", "alpha-p1").returncode == 0
    delivery = entry(tree.status(), "projects", "alpha-p1")["delivery"]
    assert delivery["approach"] == "waterfall"
    assert delivery["cadence"] is None
    assert delivery["source"] == "profile"


@pytest.mark.ac("AC-70")
def test_custom_delivery_without_description_refuses_pid_approval(tree):
    tree.pid(delivery_approach="custom")
    result = tree.run("pid", "approve", "alpha-p1")
    assert result.returncode == 1
    assert "delivery_description" in result.stderr


@pytest.mark.ac("AC-71")
def test_timebox_cadence_without_hours_refuses_pid_approval(tree):
    tree.pid(cadence="timebox")
    result = tree.run("pid", "approve", "alpha-p1")
    assert result.returncode == 1
    assert "timebox_hours" in result.stderr


@pytest.mark.ac("AC-71")
def test_timebox_status_shows_first_eight_hour_timebox(tree):
    tree.at("2026-11-16T09:00:00Z")
    tree.ready(cadence="timebox", timebox_hours=8)
    tree.at("2026-11-16T12:00:00Z")
    delivery = entry(tree.status(), "projects", "alpha-p1")["delivery"]
    assert delivery["cadence"] == "timebox"
    assert delivery["timebox"]["number"] == 1
    assert delivery["timebox"]["planned_end"] == "2026-11-16T17:00:00Z"


@pytest.mark.ac("AC-71")
def test_two_ticks_deliver_one_timebox_end_and_one_senior_user_demo(tree):
    tree.at("2026-11-16T09:00:00Z")
    tree.ready(cadence="timebox", timebox_hours=8)
    tree.at("2026-11-16T17:00:01Z")
    tree.run("conductor", "tick")
    tree.run("conductor", "tick")
    pm_inbox = tree.run("inbox", "pm1").stdout
    su_inbox = tree.run("inbox", "pm1su1").stdout
    assert pm_inbox.count("timebox-end") == 1
    assert pm_inbox.count("stage 1 timebox 1 ended") == 1
    assert su_inbox.count("timebox-demo") == 1
    assert su_inbox.count("stage 1 timebox 1 ended") == 1


@pytest.mark.ac("AC-72")
def test_missing_business_case_refuses_pid_approval(tree):
    tree.pid(omit=("business_case",))
    result = tree.run("pid", "approve", "alpha-p1")
    assert result.returncode == 1
    assert "business_case" in result.stderr


@pytest.mark.ac("AC-72")
def test_skipped_business_case_without_reason_refuses_pid_approval(tree):
    tree.pid(business_case="skipped")
    result = tree.run("pid", "approve", "alpha-p1")
    assert result.returncode == 1
    assert "business_case_skip_reason" in result.stderr


@pytest.mark.ac("AC-72")
def test_skipped_business_case_with_reason_is_approved_and_visible(tree):
    tree.pid(business_case="skipped", business_case_skip_reason="internal tooling spike")
    assert tree.run("pid", "approve", "alpha-p1").returncode == 0
    assert entry(tree.status(), "projects", "alpha-p1")["delivery"]["business_case"] == "skipped"


@pytest.mark.ac("AC-73")
def test_failed_business_justification_raises_exception_and_keeps_stage_executing(tree):
    tree.ready()
    tree.write(tree.cwd / "r.md", "# End-stage report\n")
    before = len(alerts(tree, "ygg-exception"))
    tree.run("stage", "end", "alpha-p1", "--within-tolerance", "yes", "--justified", "no", "--report", "r.md")
    assert entry(tree.status(), "projects", "alpha-p1")["state"] == "exception"
    milestones = tree.tracker_json("projects/alpha-p1/milestones.json")
    assert entry(milestones, "stages", 1)["state"] == "executing"
    exceptions = [header(text) for _, text in tree.documents("projects/alpha-p1/issues")
                  if "exception" in header(text).get("labels", [])]
    assert any(item["reason"] == "business justification no longer holds" for item in exceptions)
    assert len(alerts(tree, "ygg-exception")) - before == 1


@pytest.mark.ac("AC-73")
def test_confirmed_business_justification_is_recorded_at_stage_end(tree):
    tree.ready()
    tree.write(tree.cwd / "r.md", "# End-stage report\n")
    tree.run("stage", "end", "alpha-p1", "--within-tolerance", "yes", "--justified", "yes", "--report", "r.md")
    stage = entry(tree.tracker_json("projects/alpha-p1/milestones.json"), "stages", 1)
    assert stage["justified"] == "yes"
    assert stage["state"] == "awaiting-approval"


@pytest.mark.ac("AC-73")
def test_skipped_business_case_never_raises_justification_exception(tree):
    tree.ready(business_case="skipped", business_case_skip_reason="internal tooling spike")
    tree.write(tree.cwd / "r.md", "# End-stage report\n")
    tree.run("stage", "end", "alpha-p1", "--within-tolerance", "yes", "--justified", "no", "--report", "r.md")
    assert not [text for _, text in tree.documents("projects/alpha-p1/issues")
                if "exception" in header(text).get("labels", [])]
    stage = entry(tree.tracker_json("projects/alpha-p1/milestones.json"), "stages", 1)
    assert stage["justified"] == "not-applicable"
    assert stage["state"] == "awaiting-approval"


def agile_backlog(tree):
    tree.ready(delivery_approach="agile")
    body = ("---\nschema: ygg.backlog.v1\nthemes: []\nitems:\n"
            "  - id: B-0001\n    title: Save file format\n    priority: must\n    status: open\n"
            "  - id: B-0002\n    title: Save module\n    priority: must\n    status: open\n---\n")
    tree.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1", "--kind", "backlog", "--body", body)


@pytest.mark.ac("AC-74")
def test_senior_user_backlog_order_is_logged_as_proposal_and_preserves_order(tree):
    agile_backlog(tree)
    result = tree.run("backlog", "order", "alpha-p1", "--as", "pm1su1", "--order", "B-0002,B-0001",
                      "--rationale", "B-0002 unblocks testing", env={"YGG_NODE": "pm1su1"})
    assert result.returncode == 0
    assert entry(tree.status(), "projects", "alpha-p1")["delivery"]["backlog"]["order"] == ["B-0001", "B-0002"]
    orders = [record for record in records(tree.tracker_text("projects/alpha-p1/documents/backlog.md"))
              if record["kind"] == "backlog-order"]
    assert len(orders) == 1
    assert orders[0]["actor"] == "pm1su1"
    assert orders[0]["status"] == "proposed"
    assert orders[0]["rationale"] == "B-0002 unblocks testing"


@pytest.mark.ac("AC-74")
def test_backlog_order_without_rationale_is_refused(tree):
    agile_backlog(tree)
    result = tree.run("backlog", "order", "alpha-p1", "--order", "B-0002,B-0001")
    assert result.returncode == 1
    assert "--rationale" in result.stderr


@pytest.mark.ac("AC-74")
def test_user_backlog_order_is_applied_and_logged_with_user_actor(tree):
    agile_backlog(tree)
    result = tree.run("backlog", "order", "alpha-p1", "--order", "B-0002,B-0001", "--rationale", "user priority")
    assert result.returncode == 0
    assert entry(tree.status(), "projects", "alpha-p1")["delivery"]["backlog"]["order"] == ["B-0002", "B-0001"]
    orders = [record for record in records(tree.tracker_text("projects/alpha-p1/documents/backlog.md"))
              if record["kind"] == "backlog-order"]
    assert any(record["actor"] == "user" and record["status"] == "applied" for record in orders)
