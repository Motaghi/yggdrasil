import re

import pytest

from a1_observations import alerts, entry, header, records, sections
from observations import comments, labels


@pytest.mark.ac("AC-31")
def test_project_refusals_follow_programme_and_cannot_drop_inherited_refusal(tree):
    programme = "---\nschema: ygg.northstar.v1\nscope: programme\nactor: user\n---\n## Refusals\n- No paid shortcuts\n"
    tree.write(tree.tracker / "initiatives/alpha/documents/northstar.md", programme)
    tree.run("northstar", "add-refusal", "alpha-p1", "--text", "No new currencies this stage")
    text = tree.run("northstar", "resolve", "alpha-p1").stdout
    assert "No paid shortcuts" in text
    assert "No new currencies this stage" in text
    assert text.index("No paid shortcuts") < text.index("No new currencies this stage")
    project = "---\nschema: ygg.northstar.v1\nscope: project\nactor: user\n---\n## Refusals\n- No new currencies this stage\n"
    tree.write(tree.tracker / "projects/alpha-p1/documents/northstar.md", project)
    text = tree.run("northstar", "resolve", "alpha-p1").stdout
    refusals = re.findall(r"(?m)^- (.+)$", text)
    assert refusals[0:1] == ["No paid shortcuts"]


@pytest.mark.ac("AC-33")
@pytest.mark.parametrize("profile", [False, True], ids=["state-default", "profile-folder"])
def test_digest_lists_projects_frontiers_and_one_pending_decision(tree, profile):
    tree.decision(number=4)
    tree.route_map(frontier=["WP-41"])
    tree.route_map("beta-p1", "pm2", frontier=["WP-42"])
    destination = tree.state / "digests/2026-11-16.md"
    if profile:
        tree.example_profile(digest_dir=str(tree.root / "ygg-digests"), digest_filename="{date}-digest.md")
        destination = tree.root / "ygg-digests/2026-11-16-digest.md"
    tree.run("digest", "--date", "2026-11-16")
    text = tree.read(destination)
    assert "alpha-p1" in sections(text)
    assert "beta-p1" in sections(text)
    assert "Decisions waiting: 1" in text
    if not profile:
        for project, frontier in (("alpha-p1", "WP-41"), ("beta-p1", "WP-42")):
            match = re.search(r"(?ms)^## " + project + r"\s*\n(.*?)(?=^## |\Z)", text)
            assert match is not None
            assert "Frontier:" in match[1]
            assert frontier in match[1]


@pytest.mark.ac("AC-36")
@pytest.mark.parametrize("scope", ["tree", "history", "secret-history"])
def test_public_checkout_and_diffs_have_zero_leak_matches(empty, scope):
    assert empty.leak_matches(scope) == []


@pytest.mark.ac("AC-37")
def test_public_package_has_docs_demo_tracker_and_runnable_quickstart(empty):
    core = empty
    for name in ("LICENSE", "README.md", "examples/demo-programme"):
        assert core.exists(core.core_path(name))
    readme = core.read(core.core_path("README.md"))
    quickstart = re.search(r"(?ms)^## Quickstart\s*\n(.*?)(?=^## |\Z)", readme)
    assert quickstart is not None
    assert "--tracker files" in quickstart[1] or "YGG_TRACKER=files" in quickstart[1]
    core.copy_demo()
    assert core.run("rehydrate").returncode == 0
    status = core.status()
    assert isinstance(status, dict)
    assert any(item["id"] == "demo" for item in status["programmes"])
    assert any(item["programme"] == "demo" for item in status["projects"])
    assert any(item["role"] == "pm" and item["programme"] == "demo" for item in status["nodes"])
    assert core.quickstart().returncode == 0


def standard(version=1):
    base = {
        "work_package": {"label_prefix": "lane:", "states": ["planned", "authorised", "ready-for-test", "accepted", "rejected", "review-exhausted", "suspended", "merged"]},
        "worker_task": {"parent_field": "parent"},
        "decision": {"labels": ["decision", "provisional-decision", "decided"]},
        "stage": {"states": ["planned", "executing", "awaiting-approval", "approved", "rejected"]},
        "report": {"kinds": ["highlight", "checkpoint", "end-stage", "end-project", "lessons"]},
        "document": {"kinds": ["brief", "pid", "stage-plan", "product-description", "route-map", "northstar", "journal", "rehydrate"]},
    }
    result = {"schema": "ygg.standard.v1", "current": version, "versions": {"1": base}}
    if version == 2:
        result["versions"]["2"] = dict(base, work_package=dict(base["work_package"], label_prefix="ygg-lane:"))
    return result


@pytest.mark.ac("AC-38")
def test_standard_versions_control_labels_and_rehydrate_mixed_packages(tree):
    path = tree.root / "tracker-standard.yaml"
    tree.env["YGG_STANDARD_FILE"] = str(path)
    tree.write_yaml(path, standard())
    tree.package(criteria=True, authorise=True)
    first = tree.tracker_text("projects/alpha-p1/issues/WP-41.md")
    assert "lane:pm1tl1" in labels(first)
    assert header(first)["standard_version"] == 1
    tree.write_yaml(path, standard(2))
    tree.package("WP-42", "pm1tl2", criteria=True, authorise=True)
    second = tree.tracker_text("projects/alpha-p1/issues/WP-42.md")
    assert "ygg-lane:pm1tl2" in labels(second)
    assert header(second)["standard_version"] == 2
    assert tree.tracker_text("projects/alpha-p1/issues/WP-41.md") == first
    tree.empty_state()
    tree.run("rehydrate")
    status = tree.status()
    assert entry(status, "work_packages", "WP-41")["lane"] == "pm1tl1"
    assert entry(status, "work_packages", "WP-42")["lane"] == "pm1tl2"


@pytest.mark.ac("AC-39")
def test_cross_domain_relay_requires_grant_and_retains_source_provenance(tree):
    tree.run("test", "set-next", "message", "7")
    tree.run("send", "--from", "pm1", "--to", "l1", "--kind", "note", "--body", "fyi")
    command = ("send", "--from", "l1", "--to", "pm2", "--kind", "note", "--body", "fyi", "--relay-of", "m-0007")
    result = tree.run(*command)
    assert result.returncode == 3
    assert "REFUSED: relay-needs-user" in result.stderr
    pending = tree.pending()
    assert isinstance(pending, dict)
    assert any(item["kind"] == "relay" and item["relay"] == {
        "msg_id": "m-0007", "from_domain": "alpha", "to_domain": "beta"} for item in pending["decisions"])
    tree.run("relay", "grant", "--from-domain", "alpha", "--to-domain", "beta")
    result = tree.run(*command)
    assert result.returncode == 0
    assert "fyi" in tree.run("inbox", "pm2").stdout
    lines = tree.state_lines("messages.jsonl")
    assert lines[-1]["relay_of"] == "m-0007"
    assert lines[-1]["cross_domain"] is True


@pytest.mark.ac("AC-41")
def test_managed_pm_cannot_grant_envelope(tree):
    result = tree.run("envelope", "grant", "alpha-p1", "--until", "2026-11-16T08:00+01:00", env={"YGG_NODE": "pm1"})
    assert result.returncode == 3
    assert "REFUSED: user-only" in result.stderr
    assert entry(tree.status(), "projects", "alpha-p1")["envelope"] is None


@pytest.mark.ac("AC-41")
def test_stage_approval_is_denied_to_pm_and_allowed_to_l1(tree):
    tree.ready()
    tree.report(1)
    tree.run("stage", "end", "alpha-p1", "--within-tolerance", "yes", "--report", "end-stage-1.md")
    result = tree.run("stage", "approve", "alpha-p1", env={"YGG_NODE": "pm1"})
    assert result.returncode == 3
    assert entry(tree.status(), "projects", "alpha-p1")["state"] == "awaiting-approval"
    result = tree.run("stage", "approve", "alpha-p1", env={"YGG_NODE": "l1"})
    assert result.returncode == 0
    stage = entry(tree.status(), "projects", "alpha-p1")["stage"]
    assert stage["number"] == 2
    assert stage["state"] == "executing"


@pytest.mark.ac("AC-42")
def test_rehydrate_counts_decided_record_only_when_actor_is_allowed(tree):
    tree.decision(number=3)
    tree.append(tree.tracker / "projects/alpha-p1/issues/D-0003.md", '\n## Records\n\n```yaml\nkind: decided\nat: 2026-11-16T09:00:00Z\nactor: someone-else\nstandard_version: 1\noption: B\nnote: keep it\n```\n')
    tree.run("rehydrate")
    status = tree.status()
    assert isinstance(status, dict)
    assert "D-0003" in status["decisions_open"]
    assert any(item["record"] == "D-0003" and item["actor"] == "someone-else" for item in status["ignored_authority"])
    config = tree.root / "config.yaml"
    tree.write_yaml(config, {"authority": {"actors": ["someone-else"]}})
    tree.env["YGG_CONFIG"] = str(config)
    tree.run("rehydrate")
    assert "D-0003" not in tree.status()["decisions_open"]


@pytest.mark.ac("AC-42")
def test_pm_tracker_write_cannot_create_authority_envelope(tree):
    result = tree.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1", "--kind", "envelope", "--body", "until 2026-11-20")
    assert result.returncode == 3
    assert "REFUSED: authority kind" in result.stderr
    assert entry(tree.status(), "projects", "alpha-p1")["envelope"] is None


@pytest.mark.ac("AC-50")
def test_work_package_authorisation_requires_criteria_and_delivers_package(tree):
    tree.package()
    command = ("wp", "authorise", "alpha-p1", "--package", "WP-41", "--lane", "pm1tl1")
    result = tree.run(*command)
    assert result.returncode == 3
    assert "REFUSED: no criteria" in result.stderr
    tree.run("tracker", "write", "--as", "pm1su1", "--project", "alpha-p1", "--package", "WP-41", "--kind", "criteria", "--body", "WP-41")
    assert tree.run(*command).returncode == 0
    assert "WP-41" in tree.run("inbox", "pm1tl1").stdout


@pytest.mark.ac("AC-50")
def test_review_exhaustion_raises_issue_and_prevents_acceptance(tree):
    tree.package(criteria=True, authorise=True)
    for number in (1, 2, 3, 4):
        tree.run("review", "record", "--from", "pm1tl1", "--package", "WP-41", "--round", str(number), "--verdict", "changes")
    tree.run("review", "record", "--from", "pm1tl1", "--package", "WP-41", "--round", "5", "--verdict", "changes")
    assert header(tree.tracker_text("projects/alpha-p1/issues/WP-41.md")).get("state") == "review-exhausted"
    issues = [header(text) for _, text in tree.documents("projects/alpha-p1/issues")]
    assert any(issue.get("kind") == "issue" and issue.get("to") == "pm1" and issue.get("package") == "WP-41" for issue in issues)
    tree.git_labels()
    result = tree.run("acceptance", "record", "--from", "pm1su1", "--package", "WP-41", "--sha", tree.sha("b2c3d4e"), "--verdict", "accept")
    assert result.returncode == 3


@pytest.mark.ac("AC-55")
def test_route_map_sections_blockers_and_forecast_drift_are_observable(tree):
    tree.stage_executing(2)
    tree.route_map(blockers=["Boat runner offline"], forecast="2026-11-19T17:00:00Z")
    text = tree.tracker_text("projects/alpha-p1/documents/route-map.md")
    assert sections(text) == ["Destination", "Frontier", "Blockers", "Decisions so far", "Fog", "Out of scope"]
    assert "Boat runner offline" in entry(tree.status(), "projects", "alpha-p1")["blockers"]
    before = len(alerts(tree, "ygg-blocker-forecast"))
    tree.run("conductor", "tick")
    assert any("exception-forecast" in labels(text) for _, text in tree.documents("projects/alpha-p1/issues"))
    assert len(alerts(tree, "ygg-blocker-forecast")) - before == 1
    assert entry(tree.status(), "nodes", "pm1")["state"] == "queued"


@pytest.mark.ac("AC-56")
def test_collapsed_shape_requires_user_record_before_workers_under_pm(tree):
    result = tree.run("node", "add", "pm3w1", "--role", "worker", "--parent", "pm3")
    assert result.returncode == 3
    assert "REFUSED: topology" in result.stderr
    result = tree.run("project", "shape", "alpha-p2", "--collapsed", env={"YGG_NODE": "pm3"})
    assert result.returncode == 3
    assert "REFUSED: user-only" in result.stderr
    tree.run("project", "shape", "alpha-p2", "--collapsed", env={"YGG_NODE": "l1"})
    result = tree.run("node", "add", "pm3w1", "--role", "worker", "--parent", "pm3")
    assert result.returncode == 0
    pid = tree.tracker_text("projects/alpha-p2/documents/pid.md")
    assert any(record["kind"] == "shape" and record["shape"] == "collapsed" and record["actor"] == "user" for record in records(pid))


@pytest.mark.ac("AC-58")
@pytest.mark.parametrize("name", ["status", "pending"])
def test_json_output_validates_against_signed_schema(tree, name):
    tree.decision()
    tree.active_envelope()
    data = tree.command_json(name, "--json")
    assert isinstance(data, dict)
    assert tree.schema_errors(name, data) == []
