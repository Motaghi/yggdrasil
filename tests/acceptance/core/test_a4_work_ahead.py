import pytest


SETUP_NOW = "2026-11-16T22:00:00+01:00"
TICK_NOW = "2026-11-16T23:00:00+01:00"
EVIDENCE = "touches only docs/help.md; no stage 1 or stage 2 file depends on it"


@pytest.fixture
def a4_tree(empty):
    config = empty.root / "a4-config.yaml"
    empty.write_yaml(config, {
        "tick_cap": "count",
        "max_launches_per_tick": 50,
        "settle_delay_seconds": 0,
    })
    empty.env["YGG_CONFIG"] = str(config)
    empty.env["YGG_NOW"] = SETUP_NOW
    empty.set_load("0.40", now=SETUP_NOW)
    empty.core_tree()
    return empty


def write_package(driver, package, stage, senior, body="Save module"):
    driver.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1",
               "--stage", str(stage), "--package", package,
               "--kind", "work-package", "--body", body)
    driver.run("tracker", "write", "--as", senior, "--project", "alpha-p1",
               "--package", package, "--kind", "criteria", "--body", package)


def work_ahead_record(driver, reason="stage 1 waits on testing"):
    return driver.run("wp", "work-ahead", "alpha-p1", "--package", "WP-41",
                      "--as", "pm1", "--reason", reason, "--evidence", EVIDENCE)


@pytest.fixture
def work_ahead_fixture(a4_tree):
    def build(*, through=8, omit=(), complete_current=True, reviewer_rejects=False):
        driver = a4_tree
        # A4.1 steps 1 to 4, in their signed order.
        driver.pid(work_ahead="conditional")
        driver.run("pid", "approve", "alpha-p1")
        driver.run("node", "add", "rv1", "--role", "reviewer", "--parent", "alpha")
        write_package(driver, "WP-40", 1, "pm1su2", "Save format")
        driver.run("wp", "authorise", "alpha-p1", "--package", "WP-40",
                   "--lane", "pm1tl2")
        if complete_current:
            driver.run("wp", "complete", "--from", "pm1tl2", "--package", "WP-40")
        write_package(driver, "WP-41", 2, "pm1su1")
        driver.run("wp", "authorise", "alpha-p1", "--package", "WP-41",
                   "--lane", "pm1tl1")
        if through >= 5 and 5 not in omit:
            driver.run("envelope", "grant", "alpha-p1",
                       "--until", "2026-11-17T07:00+01:00")
        if through >= 6 and 6 not in omit:
            work_ahead_record(driver)
        if through >= 7 and 7 not in omit:
            driver.run("wp", "work-ahead-review", "alpha-p1", "--package", "WP-41",
                       "--as", "pm1su1", "--verdict", "approve")
        if through >= 8 and 8 not in omit:
            args = ["wp", "work-ahead-review", "alpha-p1", "--package", "WP-41",
                    "--as", "rv1", "--verdict", "reject" if reviewer_rejects else "approve"]
            if reviewer_rejects:
                args += ["--note", "touches the save format"]
            driver.run(*args)
        return driver

    return build


def tick(driver):
    driver.env["YGG_NOW"] = TICK_NOW
    driver.set_load("0.40", now=TICK_NOW)
    driver.clear_calls()
    driver.run("conductor", "tick")


def lead_status(driver):
    nodes = (driver.status() or {}).get("nodes", [])
    return next((node for node in nodes if node.get("id") == "pm1tl1"), {})


def assert_held(driver, reason):
    assert driver.launches("pm1tl1") == []
    assert lead_status(driver).get("held_reason") == reason


@pytest.mark.ac("AC-80")
def test_default_work_ahead_allows_authorisation_but_holds_later_stage(a4_tree):
    a4_tree.run("pid", "approve", "alpha-p1")
    write_package(a4_tree, "WP-41", 2, "pm1su1")
    result = a4_tree.run("wp", "authorise", "alpha-p1", "--package", "WP-41",
                         "--lane", "pm1tl1")
    tick(a4_tree)
    assert result.returncode == 0
    assert_held(a4_tree, "work-ahead: pid off")


@pytest.mark.ac("AC-80")
def test_pm_work_ahead_request_is_refused_when_pid_omits_setting(a4_tree):
    a4_tree.run("pid", "approve", "alpha-p1")
    write_package(a4_tree, "WP-41", 2, "pm1su1")
    result = a4_tree.run("wp", "work-ahead", "alpha-p1", "--package", "WP-41",
                         "--as", "pm1", "--reason", "r", "--evidence", "e")
    assert result.returncode == 3
    assert "REFUSED: work-ahead-off" in result.stderr


@pytest.mark.ac("AC-80")
def test_default_work_ahead_still_launches_executing_stage_lead(a4_tree):
    a4_tree.run("pid", "approve", "alpha-p1")
    write_package(a4_tree, "WP-41", 1, "pm1su1")
    a4_tree.run("wp", "authorise", "alpha-p1", "--package", "WP-41",
                "--lane", "pm1tl1")
    tick(a4_tree)
    assert len(a4_tree.launches("pm1tl1")) == 1


@pytest.mark.ac("AC-81")
def test_conditional_work_ahead_launches_lead_without_held_reason(work_ahead_fixture):
    driver = work_ahead_fixture()
    tick(driver)
    assert len(driver.launches("pm1tl1")) == 1
    node = lead_status(driver)
    assert "held_reason" in node
    assert node["held_reason"] is None


@pytest.mark.ac("AC-81")
def test_work_ahead_launch_appears_in_pending_morning_go(work_ahead_fixture):
    driver = work_ahead_fixture()
    tick(driver)
    queue = (driver.status() or {}).get("morning_go_queue", [])
    assert any(item.get("kind") == "work-ahead" and item.get("ref") == "WP-41"
               and item.get("status") == "pending-user" for item in queue)


@pytest.mark.ac("AC-81")
def test_work_ahead_launch_appears_in_daily_digest(work_ahead_fixture):
    driver = work_ahead_fixture()
    tick(driver)
    driver.run("digest", "--date", "2026-11-16")
    assert "Work ahead: WP-41 (stage 2)" in driver.state_text("digests/2026-11-16.md")


@pytest.mark.ac("AC-82")
def test_work_ahead_without_envelope_holds_lead(work_ahead_fixture):
    driver = work_ahead_fixture(omit=(5,))
    tick(driver)
    assert_held(driver, "work-ahead: no envelope")


@pytest.mark.ac("AC-82")
def test_work_ahead_with_authorised_current_stage_work_holds_lead(work_ahead_fixture):
    driver = work_ahead_fixture(complete_current=False)
    tick(driver)
    assert_held(driver, "work-ahead: current-stage work available")


@pytest.mark.ac("AC-82")
def test_work_ahead_without_pm_record_holds_lead(work_ahead_fixture):
    driver = work_ahead_fixture(through=5)
    tick(driver)
    assert_held(driver, "work-ahead: no record")


@pytest.mark.ac("AC-83")
def test_work_ahead_without_reviewer_approval_holds_lead(work_ahead_fixture):
    driver = work_ahead_fixture(omit=(8,))
    tick(driver)
    assert_held(driver, "work-ahead: awaiting reviewer approval")


@pytest.mark.ac("AC-83")
def test_work_ahead_without_senior_user_approval_holds_lead(work_ahead_fixture):
    driver = work_ahead_fixture(omit=(7,))
    tick(driver)
    assert_held(driver, "work-ahead: awaiting su approval")


@pytest.mark.ac("AC-83")
def test_new_work_ahead_record_requires_both_approvals_again(work_ahead_fixture):
    driver = work_ahead_fixture()
    work_ahead_record(driver, reason="second reason")
    tick(driver)
    assert_held(driver, "work-ahead: awaiting su and reviewer approval")


@pytest.mark.ac("AC-84")
def test_team_lead_cannot_write_work_ahead_record(work_ahead_fixture):
    driver = work_ahead_fixture(through=5)
    result = driver.run("wp", "work-ahead", "alpha-p1", "--package", "WP-41",
                        "--as", "pm1tl1", "--reason", "r", "--evidence", "e")
    assert result.returncode == 3
    assert "REFUSED: pm-only" in result.stderr


@pytest.mark.ac("AC-84")
def test_team_lead_cannot_review_work_ahead_record(work_ahead_fixture):
    driver = work_ahead_fixture(through=6)
    result = driver.run("wp", "work-ahead-review", "alpha-p1", "--package", "WP-41",
                        "--as", "pm1tl1", "--verdict", "approve")
    assert result.returncode == 3
    assert "REFUSED: work-ahead-role" in result.stderr


@pytest.mark.ac("AC-84")
def test_reviewer_rejection_holds_work_ahead_lead(work_ahead_fixture):
    driver = work_ahead_fixture(reviewer_rejects=True)
    tick(driver)
    assert_held(driver, "work-ahead: rejected by reviewer")


@pytest.mark.ac("AC-85")
def test_pm_pid_edit_does_not_change_approved_work_ahead_setting(a4_tree):
    a4_tree.run("pid", "approve", "alpha-p1")
    approved_pid = a4_tree.tracker_text("projects/alpha-p1/documents/pid.md")
    body = approved_pid.replace("---\n", "---\nwork_ahead: conditional\n", 1)
    a4_tree.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1",
                "--kind", "pid", "--body", body)
    result = a4_tree.command_json("config", "explain", "work_ahead",
                                 "--project", "alpha-p1", "--json")
    assert (result or {}).get("value") == "off"


@pytest.mark.ac("AC-85")
def test_managed_pm_cannot_change_approved_work_ahead_setting(a4_tree):
    a4_tree.run("pid", "approve", "alpha-p1")
    result = a4_tree.run("pid", "work-ahead", "alpha-p1", "--set", "conditional",
                         "--reason", "stage 1 waits on a supplier", env={"YGG_NODE": "pm1"})
    assert result.returncode == 3
    assert "REFUSED: user-only" in result.stderr


@pytest.mark.ac("AC-85")
def test_user_can_change_approved_work_ahead_setting(a4_tree):
    a4_tree.run("pid", "approve", "alpha-p1")
    result = a4_tree.run("pid", "work-ahead", "alpha-p1", "--set", "conditional",
                         "--reason", "stage 1 waits on a supplier")
    explained = a4_tree.command_json("config", "explain", "work_ahead",
                                    "--project", "alpha-p1", "--json")
    assert result.returncode == 0
    assert (explained or {}).get("value") == "conditional"
    assert explained["source"] == "pid:alpha-p1"


@pytest.mark.ac("AC-86")
def test_rehydrate_restores_approved_work_ahead_and_launches_lead(work_ahead_fixture):
    driver = work_ahead_fixture()
    driver.empty_state()
    driver.run("rehydrate")
    tick(driver)
    assert len(driver.launches("pm1tl1")) == 1


@pytest.mark.ac("AC-86")
def test_rehydrate_preserves_missing_work_ahead_reviewer_approval(work_ahead_fixture):
    driver = work_ahead_fixture(omit=(8,))
    driver.empty_state()
    driver.run("rehydrate")
    tick(driver)
    assert_held(driver, "work-ahead: awaiting reviewer approval")


@pytest.mark.ac("AC-86")
def test_rehydrate_restores_user_work_ahead_setting(a4_tree):
    a4_tree.run("pid", "approve", "alpha-p1")
    a4_tree.run("pid", "work-ahead", "alpha-p1", "--set", "conditional", "--reason", "r")
    a4_tree.empty_state()
    a4_tree.run("rehydrate")
    result = a4_tree.command_json("config", "explain", "work_ahead",
                                 "--project", "alpha-p1", "--json")
    assert (result or {}).get("value") == "conditional"


@pytest.mark.ac("AC-87")
def test_user_config_cannot_set_work_ahead(empty):
    config = empty.cwd / "yggdrasil.yaml"
    empty.write_yaml(config, {"schema": "ygg.config.v1", "work_ahead": "conditional"})
    empty.env["YGG_CONFIG"] = str(config)
    result = empty.run("config", "validate")
    assert result.returncode == 1
    assert "work_ahead can be set only in a PID" in result.stderr


@pytest.mark.ac("AC-87")
def test_pid_approval_refuses_unknown_work_ahead_setting(a4_tree):
    a4_tree.pid(work_ahead="sometimes")
    result = a4_tree.run("pid", "approve", "alpha-p1")
    assert result.returncode == 1
    assert "work_ahead" in result.stderr
