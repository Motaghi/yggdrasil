import json
import re

import pytest

from a1_observations import alerts, entry


@pytest.fixture
def linear_tree(empty):
    with empty.a5_linear():
        empty.core_tree()
        yield empty


def project_row(state):
    projects = [row for row in state.get("projects", []) if row["name"] == "alpha-p1"]
    assert len(projects) == 1
    return projects[0]


def package_row(state):
    project = project_row(state)
    packages = [row for row in state.get("issues", [])
                if row.get("projectId") == project["id"]
                and re.match(r"^WP-41(?:\s|$)", row["title"])]
    assert len(packages) == 1
    return packages[0]


def stage_two_package(driver):
    driver.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1",
               "--stage", "2", "--kind", "stage-plan", "--body", "Stage 2 plan")
    driver.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1",
               "--stage", "2", "--package", "WP-41", "--kind", "work-package",
               "--body", "Save module split")


def authorise_package(driver):
    driver.run("tracker", "write", "--as", "pm1su1", "--project", "alpha-p1",
               "--package", "WP-41", "--kind", "criteria", "--body", "WP-41")
    driver.run("wp", "authorise", "alpha-p1", "--package", "WP-41", "--lane", "pm1tl1")


def labelled(state, issue, name):
    return any(row["id"] in issue.get("labelIds", []) and row["name"] == name
               for row in state.get("labels", []))


@pytest.mark.ac("AC-08")
def test_linear_rehydrate_restores_full_inflight_state(linear_tree):
    linear_tree.at("2026-11-16T02:00:00+01:00")
    linear_tree.stage_executing(2)
    linear_tree.decision(number=3, scope="lane:pm1tl2")
    linear_tree.active_envelope()
    linear_tree.run("test", "set-next", "message", "10")
    linear_tree.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "note",
                    "--body", "API contract ready")
    linear_tree.set_load("0.95", now=linear_tree.env["YGG_NOW"])
    linear_tree.run("issue", "raise", "--from", "pm1tl1", "--reason", "fixture data missing")
    linear_tree.run("conductor", "tick")
    linear_tree.run("test", "set-next", "activation", "12")
    linear_tree.seed_activation("pm1su1", "--outcome", "uncertain")
    linear_tree.empty_state()
    linear_tree.run("rehydrate")
    status = linear_tree.status()
    assert isinstance(status, dict)
    assert {"alpha", "beta"} <= {item["id"] for item in status["programmes"]}
    parents = {"pm1": "alpha", "pm2": "beta", "pm3": "alpha", "pm1tl1": "pm1",
               "pm1tl2": "pm1", "pm1su1": "pm1", "pm1su2": "pm1", "pm1tl1w1": "pm1tl1",
               "pm1tl1w2": "pm1tl1", "pm1tl2w1": "pm1tl2", "pm1su1w1": "pm1su1",
               "pm1su1w2": "pm1su1", "pm2tl1": "pm2", "pm2su1": "pm2", "pm1rv1": "pm1"}
    nodes = [item for item in status["nodes"] if item["id"] != "l1"]
    assert len(nodes) == 15
    assert {node["id"] for node in nodes} == set(parents)
    for node in nodes:
        if node["id"] in {"pm1", "pm2", "pm3"}:
            assert node["parent"] == "l1"
            assert node["programme"] == parents[node["id"]]
        else:
            assert node["parent"] == parents[node["id"]]
    assert "D-0003" in status["decisions_open"]
    project = entry(status, "projects", "alpha-p1")
    assert project["envelope"]["until"] == "2026-11-16T07:00:00Z"
    assert project["stage"]["number"] == 2
    assert project["stage"]["state"] == "executing"
    assert any(item["node"] == "pm1tl1" and item["msg_id"] == "m-0010"
               for item in status["inbox_pending"])
    assert "pm1" in status["held_launches"]
    assert "act-0012" in status["uncertain_keys"]


@pytest.mark.ac("AC-27")
def test_linear_programme_and_project_create_link_and_validate_requests(empty):
    with empty.a5_linear():
        empty.domain_repositories()
        empty.run("programme", "create", "alpha", "--domain", "alpha", "--home", empty.repo_a,
                  "--kind", "git-repo")
        empty.run("project", "create", "alpha-p1", "--programme", "alpha", "--pm", "pm1")
        state = empty.a5_linear_state()
        initiatives = [row for row in state.get("initiatives", []) if row["name"] == "alpha"]
        assert len(initiatives) == 1
        project = project_row(state)
        assert any(row["initiativeId"] == initiatives[0]["id"] and row["projectId"] == project["id"]
                   for row in state.get("links", []))
        requests = empty.a5_linear_requests()
        for mutation in ("initiativeCreate", "projectCreate", "initiativeToProjectCreate"):
            assert any(mutation in record["body"].get("query", "") for record in requests)
        assert empty.a5_linear_live_schema_errors() == []


@pytest.mark.ac("AC-27")
def test_linear_stage_package_and_authorisation_attach_milestone_and_lane(linear_tree):
    linear_tree.stage_executing(2)
    stage_two_package(linear_tree)
    state = linear_tree.a5_linear_state()
    project = project_row(state)
    milestones = [row for row in state.get("milestones", [])
                  if row["projectId"] == project["id"] and row["name"] == "Stage 2"]
    assert len(milestones) == 1
    assert package_row(state)["projectMilestoneId"] == milestones[0]["id"]
    authorise_package(linear_tree)
    state = linear_tree.a5_linear_state()
    assert labelled(state, package_row(state), "lane:pm1tl1")


@pytest.mark.ac("AC-27")
def test_linear_worker_task_is_subissue_of_work_package(linear_tree):
    linear_tree.stage_executing(2)
    stage_two_package(linear_tree)
    authorise_package(linear_tree)
    linear_tree.run("tracker", "write", "--as", "pm1tl1", "--project", "alpha-p1",
                    "--package", "WP-41", "--worker", "pm1tl1w2", "--kind", "worker-task",
                    "--body", "split save module")
    state = linear_tree.a5_linear_state()
    package = package_row(state)
    assert any(row.get("parentId") == package["id"] and row["title"] == "split save module"
               for row in state.get("issues", []))


@pytest.mark.ac("AC-28")
def test_linear_highlight_is_project_update_with_health(linear_tree):
    linear_tree.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1",
                    "--kind", "highlight", "--body", "health: atRisk; stage 2 slipping 1 day")
    state = linear_tree.a5_linear_state()
    project = project_row(state)
    assert any(row.get("projectId") == project["id"] and row["health"] == "atRisk"
               for row in state.get("updates", []))


@pytest.mark.ac("AC-28")
def test_linear_checkpoint_is_comment_on_work_package(linear_tree):
    linear_tree.stage_executing(2)
    stage_two_package(linear_tree)
    authorise_package(linear_tree)
    linear_tree.run("tracker", "write", "--as", "pm1tl1", "--project", "alpha-p1",
                    "--package", "WP-41", "--kind", "checkpoint", "--body", "WP-41 60 percent done")
    state = linear_tree.a5_linear_state()
    package = package_row(state)
    assert any(row.get("issueId") == package["id"]
               and "WP-41 60 percent done" in row["body"]
               for row in state.get("comments", []))


@pytest.mark.ac("AC-28")
def test_linear_exception_issue_has_plan_and_one_alert(linear_tree):
    linear_tree.stage_executing(2)
    linear_tree.run("exception", "raise", "--from", "pm1",
                    "--reason", "save format change breaks stage plan", "--plan", "add one day to stage 2")
    state = linear_tree.a5_linear_state()
    project = project_row(state)
    assert any(row.get("projectId") == project["id"] and labelled(state, row, "exception")
               and "add one day to stage 2" in (row.get("description") or "")
               for row in state.get("issues", []))
    assert len(alerts(linear_tree, "ygg-exception")) == 1


@pytest.mark.ac("AC-62")
def test_linear_rehydrate_reads_all_120_work_packages(linear_tree):
    linear_tree.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1",
                    "--stage", "1", "--kind", "stage-plan", "--body", "Stage 1 plan")
    for number in range(1, 121):
        linear_tree.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1", "--stage", "1",
                        "--package", "WP-" + str(number), "--kind", "work-package", "--body", "Save module split")
    linear_tree.empty_state()
    linear_tree.run("rehydrate")
    status = linear_tree.status()
    assert isinstance(status, dict)
    packages = [item for item in status["work_packages"] if item["project"] == "alpha-p1"]
    assert len(packages) == 120
    assert len({item["id"] for item in packages}) == 120


@pytest.mark.ac("AC-62")
def test_linear_retry_after_waits_two_seconds_and_retries_once(linear_tree):
    linear_tree.a5_linear_clear_requests()
    linear_tree.a5_linear_control("fail-next", {"status": 429, "headers": {"Retry-After": "2"}, "body": {}})
    result = linear_tree.run("rehydrate")
    requests = linear_tree.a5_linear_requests()
    assert len(requests) >= 2
    first = requests[0]
    retries = [row for row in requests if row["path"] == first["path"] and row["body"] == first["body"]]
    assert first["response"]["status"] == 429
    assert len(retries) == 2
    assert retries[1]["received_at"] - first["received_at"] >= 2
    assert result.returncode == 0


@pytest.mark.ac("AC-62")
def test_linear_outage_holds_conductor_without_launch(linear_tree):
    linear_tree.ready()
    linear_tree.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "start WP-3")
    linear_tree.clear_calls()
    linear_tree.a5_linear_refuse_connections()
    result = linear_tree.run("conductor", "tick")
    assert result.returncode == 8
    assert linear_tree.launches() == []


@pytest.mark.ac("AC-64")
def test_linear_renewal_failure_exits_eight_twice_and_alerts_once(linear_tree):
    linear_tree.ready()
    linear_tree.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "start WP-3")
    linear_tree.a5_linear_expire_token()
    linear_tree.a5_linear_control("token-endpoint", {"status": 401, "body": {}})
    linear_tree.clear_calls()
    first = linear_tree.run("conductor", "tick")
    second = linear_tree.run("conductor", "tick")
    assert first.returncode == 8
    assert second.returncode == 8
    assert linear_tree.launches() == []
    assert len(alerts(linear_tree, "ygg-tracker-auth")) == 1


@pytest.mark.ac("AC-64")
def test_linear_headless_environment_excludes_tracker_credentials(linear_tree):
    linear_tree.ready()
    credentials = linear_tree.a5_linear_credentials()
    linear_tree.clear_calls()
    linear_tree.run("launch", "pm1tl1")
    launches = linear_tree.launches("pm1tl1")
    assert launches
    for launch in launches:
        environment = json.dumps(launch["env"])
        assert credentials["access_token"] not in environment
        assert credentials["client_secret"] not in environment
        assert str(linear_tree.linear_dir / "token-store.json") not in environment
