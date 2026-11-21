import json

import pytest

from a1_observations import alerts, entry, header, records, sections
from observations import comments


@pytest.mark.ac("AC-00")
def test_scripted_skeleton_reaches_stage_one_user_gate(empty):
    tree = empty
    tree.skeleton()
    status = tree.status()
    project = entry(status, "projects", "alpha-p1")
    assert project["state"] == "awaiting-approval"
    assert project["stage"]["number"] == 1
    assert tree.tracker_text("projects/alpha-p1/documents/pid.md")
    assert sections(tree.tracker_text("projects/alpha-p1/documents/route-map.md")) == [
        "Destination", "Frontier", "Blockers", "Decisions so far", "Fog", "Out of scope"]
    packages = [header(text) for _, text in tree.documents("projects/alpha-p1/issues")
                if header(text).get("kind") == "work-package"]
    assert len(packages) == 1
    assert packages[0]["lane"] == "pm1tl1"
    package = tree.tracker_text("projects/alpha-p1/issues/" + packages[0]["id"] + ".md")
    criteria = [record for record in records(package) if record["kind"] == "criteria"]
    assert any(record["node"] == "pm1su1" for record in criteria)
    steps = [call for call in tree.calls() if call.get("event") == "step"]
    criterion_steps = [index for index, call in enumerate(steps) if call["node"] == "pm1su1" and call["id"] == "criteria" and call["exit_code"] == 0]
    authorisations = [index for index, call in enumerate(steps) if call["node"] == "pm1" and call["id"] == "authorise" and call["exit_code"] == 0]
    assert criterion_steps and authorisations
    assert criterion_steps[0] < authorisations[0]
    assert any(record["kind"] == "checkpoint" for record in records(package, "Comments"))
    head = tree.ref("ygg/alpha-p1/lane/pm1tl1")
    assert any(record["kind"] == "acceptance" and record["sha"] == head for record in records(package))
    assert tree.tracker_text("projects/alpha-p1/documents/end-stage-1.md")
    assert len(alerts(tree, "ygg-pid-ready")) == 1
    assert len(alerts(tree, "ygg-stage-ready")) == 1
    assert all(node["state"] == "idle" for node in status["nodes"])


@pytest.mark.ac("AC-49")
def test_authorised_build_dispatch_waits_for_pid_approval(tree):
    tree.package(criteria=True, authorise=True)
    tree.run("conductor", "tick")
    assert not tree.launches("pm1tl1")
    tree.run("pid", "approve", "alpha-p1")
    tree.run("conductor", "tick")
    assert len(tree.launches("pm1tl1")) == 1


@pytest.mark.ac("AC-49")
def test_revoking_envelope_at_stage_three_holds_build_dispatch(tree):
    tree.stage_executing(3)
    tree.active_envelope()
    tree.package(criteria=True, authorise=True)
    tree.run("envelope", "revoke", "alpha-p1")
    tree.clear_calls()
    tree.run("conductor", "tick")
    project = entry(tree.status(), "projects", "alpha-p1")
    assert project["state"] == "awaiting-approval"
    for node in ("pm1tl1", "pm1tl2", "pm1tl1w1", "pm1tl1w2", "pm1tl2w1", "pm1su1w1", "pm1su1w2"):
        assert not tree.launches(node)


@pytest.mark.ac("AC-51")
def test_inbox_acknowledgement_removes_only_named_message(tree):
    tree.run("test", "set-next", "message", "10")
    for body in ("API contract ready", "fixture path changed"):
        tree.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "note", "--body", body)
    tree.run("inbox", "pm1tl1", "--ack", "m-0010")
    listing = tree.run("inbox", "pm1tl1").stdout
    assert "m-0011" in listing
    assert "m-0010" not in listing


@pytest.mark.ac("AC-51")
def test_midrun_message_rewakes_once_only_after_exit(tree):
    tree.ready()
    tree.fake_behavior(claude={"agents": {"pm1tl1": {"mode": "persist"}}})
    tree.run("launch", "pm1tl1")
    tree.clear_calls()
    tree.run("test", "set-next", "message", "12")
    tree.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "note", "--body", "API contract ready")
    tree.run("conductor", "tick")
    assert not tree.launches("pm1tl1")
    tree.exit_agent("pm1tl1")
    tree.run("conductor", "tick")
    assert len(tree.launches("pm1tl1")) == 1


@pytest.mark.ac("AC-52")
def test_stop_interrupts_running_session_writes_checkpoint_and_resume_relaunches(tree):
    tree.ready()
    tree.fake_behavior(claude={"agents": {"pm1tl1": {"mode": "persist"}}})
    tree.run("launch", "pm1tl1")
    tree.run("stop", "alpha")
    assert any(call.get("event") == "interrupt" and call["node"] == "pm1tl1" for call in tree.calls())
    assert entry(tree.status(), "nodes", "pm1tl1")["state"] == "stopped"
    updates = [header(text) for _, text in tree.documents("projects/alpha-p1/updates")]
    assert any(record.get("kind") == "checkpoint" and record.get("node") == "pm1tl1" for record in updates)
    tree.clear_calls()
    tree.run("resume", "alpha")
    result = tree.run("launch", "pm1tl1")
    assert result.returncode == 0
    assert len(tree.launches("pm1tl1")) == 1


@pytest.mark.ac("AC-52")
def test_programme_pause_denies_next_managed_tool_call(tree):
    tree.run("pause", "alpha")
    result = tree.hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, node="pm1tl1")
    assert result.returncode == 2
    assert "paused" in result.stderr


@pytest.mark.ac("AC-59")
def test_scripted_pm_exit_writes_short_rehydration_document(tree):
    tree.crowded_pm()
    status = tree.status()
    assert isinstance(status, dict)
    assert len(status["decisions_open"]) == 12
    completed = [item for item in status["work_packages"] if item["state"] in {"ready-for-test", "accepted", "merged"}]
    assert len(completed) == 40
    text = tree.tracker_text("projects/alpha-p1/documents/rehydrate-pm1.md")
    assert "Open decisions" in sections(text)
    assert "Next actions" in sections(text)
    assert len(text.splitlines()) <= 150


@pytest.mark.ac("AC-59")
def test_readonly_tracker_send_fails_before_inbox_delivery(tree):
    tree.readonly(tree.tracker)
    result = tree.run("send", "--from", "pm1tl1", "--to", "pm1", "--kind", "checkpoint", "--body", "WP-41 done")
    assert result.returncode == 8
    assert "WP-41 done" not in tree.run("inbox", "pm1").stdout


@pytest.mark.ac("AC-59")
def test_concurrent_checkpoint_writes_preserve_both_comments(tree):
    tree.package()
    command = ("tracker", "write", "--as", "pm1tl1", "--project", "alpha-p1", "--package", "WP-41", "--kind", "checkpoint", "--body")
    tree.concurrent((*command, "first"), (*command, "second"))
    body = comments(tree.tracker_text("projects/alpha-p1/issues/WP-41.md"))
    assert body is not None
    assert "first" in body
    assert "second" in body


@pytest.mark.ac("AC-60")
def test_capacity_failure_is_held_and_requeued_without_stall_alert(tree):
    tree.fake_behavior(claude={"agents": {"pm1": {"mode": "capacity"}}})
    tree.run("conductor", "tick")
    status = tree.status()
    assert entry(status, "nodes", "pm1")["state"] == "held-capacity"
    assert "pm1" in status["held_launches"]
    assert not alerts(tree, "ygg-stall")


@pytest.mark.ac("AC-60")
def test_simultaneous_stalls_produce_one_alert_listing_all_five_nodes(tree):
    tree.ready()
    tree.ready("beta-p1")
    tree.at("2026-11-16T10:25:00+01:00")
    nodes = ("pm1tl1", "pm1tl2", "pm1su1", "pm1su2", "pm2tl1")
    for node in nodes:
        tree.seed_activation(node, "--state", "running", "--process", "dead", "--heartbeat", "2026-11-16T10:00:00+01:00", "--retries-used", "2")
    before = len(alerts(tree, "ygg-stall"))
    tree.run("heimdall", "tick")
    added = alerts(tree, "ygg-stall")[before:]
    assert len(added) == 1
    for node in nodes:
        assert node in str(added[0])


@pytest.mark.ac("AC-60")
def test_second_conductor_refuses_held_lock(tree):
    with tree.conductor():
        result = tree.run("conductor", "run")
    assert result.returncode == 5
    assert "conductor" in result.stderr.lower()
    assert "lock" in result.stderr.lower()


@pytest.mark.ac("AC-65")
def test_vault_docs_launch_uses_staging_without_git_branches(empty):
    tree = empty
    home = tree.make_cwd("docs-g")
    tree.run("programme", "create", "gamma", "--domain", "gamma", "--home", home, "--kind", "vault-docs")
    tree.run("project", "create", "gamma-p1", "--programme", "gamma", "--pm", "pm4")
    tree.run("node", "add", "pm4tl1", "--role", "tl", "--parent", "pm4")
    tree.pid("gamma-p1", "pm4", "gamma", False)
    tree.run("pid", "approve", "gamma-p1")
    tree.run("agents", "allow-minimal", "--yes")
    tree.run("launch", "pm4tl1")
    calls = tree.launches("pm4tl1")
    assert len(calls) == 1
    assert tree.same_path(calls[0]["cwd"], home / ".ygg-staging/gamma-p1/pm4tl1")
    assert not tree.git_metadata(home)


@pytest.mark.ac("AC-65")
def test_shared_tree_launch_uses_worktree_and_preserves_home_main(empty):
    tree = empty
    home = tree.root / "shared-d"
    tree.repository(home)
    tree.run("programme", "create", "delta", "--domain", "delta", "--home", home, "--kind", "shared-tree")
    tree.run("project", "create", "delta-p1", "--programme", "delta", "--pm", "pm5")
    tree.run("node", "add", "pm5tl1", "--role", "tl", "--parent", "pm5")
    tree.pid("delta-p1", "pm5", "delta", home)
    tree.run("pid", "approve", "delta-p1")
    tree.run("agents", "allow-minimal", "--yes")
    tree.run("launch", "pm5tl1")
    calls = tree.launches("pm5tl1")
    assert len(calls) == 1
    assert tree.beneath(calls[0]["cwd"], home / ".ygg-worktrees")
    assert tree.git(calls[0]["cwd"], "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    assert tree.git(home, "branch", "--show-current").stdout.strip() == "main"


@pytest.mark.ac("AC-65")
def test_programme_without_kind_cannot_launch(empty):
    tree = empty
    home = tree.make_cwd("epsilon")
    tree.write(tree.tracker / "initiatives/epsilon/initiative.json", json.dumps({
        "schema": "ygg.initiative.v1", "id": "epsilon", "domain": "epsilon", "home": str(home),
        "standard_version": 1, "actor": "user", "created_at": "2026-11-16T09:00:00Z"}))
    tree.run("project", "create", "epsilon-p1", "--programme", "epsilon", "--pm", "pm6")
    tree.run("node", "add", "pm6tl1", "--role", "tl", "--parent", "pm6")
    result = tree.run("launch", "pm6tl1")
    assert result.returncode == 3
    assert "kind" in result.stderr.lower()


@pytest.mark.ac("AC-67")
def test_authorisation_completion_and_failed_ci_wake_the_named_nodes(tree):
    tree.ready()
    tree.package(criteria=True)
    tree.quiesce()
    tree.clear_calls()
    tree.run("wp", "authorise", "alpha-p1", "--package", "WP-41", "--lane", "pm1tl1")
    tree.run("conductor", "tick")
    assert tree.launches("pm1tl1")
    assert tree.launches("pm1su1")
    tree.quiesce()
    tree.clear_calls()
    tree.run("wp", "complete", "--from", "pm1tl1", "--package", "WP-41")
    tree.complete_package_review()
    tree.run("conductor", "tick")
    assert len(tree.launches("pm1")) == 1
    tree.quiesce()
    tree.clear_calls()
    tree.run("event", "ci", "--repo", tree.repo_a, "--branch", "ygg/alpha-p1/lane/pm1tl1", "--status", "failed")
    tree.run("conductor", "tick")
    assert tree.launches("pm1tl1")
    assert tree.launches("pm1su1")
