import pytest

from observations import has_label

def node(status, node_id):
    assert isinstance(status, dict)
    return next(item for item in status["nodes"] if item["id"] == node_id)


def project(status, project_id):
    assert isinstance(status, dict)
    return next(item for item in status["projects"] if item["id"] == project_id)


@pytest.mark.ac("AC-12")
def test_out_of_tolerance_boundary_waits_inside_envelope(tree):
    tree.stage_executing(2)
    tree.active_envelope()
    tree.at("2026-11-16T03:00:00+01:00")
    tree.write(tree.cwd / "end-stage-2.md", "# End-stage report\n")
    tree.run("stage", "end", "alpha-p1", "--within-tolerance", "no", "--report", "end-stage-2.md")
    assert project(tree.status(), "alpha-p1")["state"] == "awaiting-approval"
    assert has_label(tree.documents("projects/alpha-p1/issues"), "exception")
    assert len([line for line in tree.state_lines("alerts.jsonl") if line["kind"] == "ygg-exception"]) == 1


@pytest.mark.ac("AC-17")
@pytest.mark.parametrize("reason,forecast,kind", [
    ("Boat worker refused", False, "ygg-blocker"),
    ("CI minutes run out tomorrow", True, "ygg-blocker-forecast"),
], ids=["blocker", "forecast"])
def test_blocker_alert_kind_once(tree, reason, forecast, kind):
    before = tree.state_lines("alerts.jsonl")
    args = ["blocker", "raise", "--from", "pm1tl1", "--reason", reason]
    if forecast:
        args.append("--forecast")
    tree.run(*args)
    added = tree.state_lines("alerts.jsonl")[len(before):]
    assert len(added) == 1
    assert added[0]["kind"] == kind


@pytest.mark.ac("AC-17")
def test_pid_ready_alert_once(tree):
    tree.run("tracker", "write", "--as", "pm2", "--project", "beta-p1", "--kind", "pid", "--body", "PID v1")
    before = tree.state_lines("alerts.jsonl")
    tree.run("tracker", "write", "--as", "pm2", "--project", "beta-p1", "--kind", "pid-ready", "--body", "PID v1")
    added = tree.state_lines("alerts.jsonl")[len(before):]
    assert len(added) == 1
    assert added[0]["kind"] == "ygg-pid-ready"


@pytest.mark.ac("AC-34")
def test_issue_wakes_idle_pm_immediately(quiet_tree):
    tree = quiet_tree
    tree.at("2026-11-16T10:00:00+01:00")
    tree.run("issue", "raise", "--from", "pm1tl1", "--reason", "fixture data missing")
    tree.at("2026-11-16T10:01:00+01:00")
    tree.run("conductor", "tick")
    assert len(tree.launches("pm1")) == 1


@pytest.mark.ac("AC-34")
def test_checkpoint_waits_for_other_active_lane_or_four_hours(tree):
    tree.package("WP-41", "pm1tl1", criteria=True, authorise=True)
    tree.package("WP-42", "pm1tl2", criteria=True, authorise=True)
    tree.quiesce()
    tree.clear_calls()
    tree.at("2026-11-16T10:00:00+01:00")
    tree.run("tracker", "write", "--as", "pm1tl1", "--project", "alpha-p1", "--package", "WP-41",
             "--kind", "checkpoint", "--body", "WP-41 60 percent done")
    tree.at("2026-11-16T10:05:00+01:00")
    tree.run("conductor", "tick")
    assert not tree.launches("pm1")
    tree.at("2026-11-16T14:00:00+01:00")
    tree.run("conductor", "tick")
    assert len(tree.launches("pm1")) == 1


@pytest.mark.ac("AC-34")
def test_checkpoint_of_only_active_lane_wakes_pm(tree):
    tree.package(criteria=True, authorise=True)
    tree.quiesce()
    tree.clear_calls()
    tree.at("2026-11-16T10:00:00+01:00")
    tree.run("tracker", "write", "--as", "pm1tl1", "--project", "alpha-p1", "--package", "WP-41",
             "--kind", "checkpoint", "--body", "WP-41 60 percent done")
    tree.at("2026-11-16T10:01:00+01:00")
    tree.run("conductor", "tick")
    assert len(tree.launches("pm1")) == 1


@pytest.mark.ac("AC-35")
def test_conductor_holds_and_releases_on_load(quiet_tree):
    tree = quiet_tree
    tree.run("issue", "raise", "--from", "pm1tl1", "--reason", "fixture data missing")
    tree.set_load("0.95")
    tree.run("conductor", "tick")
    assert not tree.launches()
    status = tree.status()
    assert isinstance(status, dict)
    assert "pm1" in status["held_launches"]
    tree.set_load("0.40")
    tree.run("conductor", "tick")
    assert len(tree.launches("pm1")) == 1


@pytest.mark.ac("AC-35")
def test_conductor_reads_load_again_between_launches(quiet_tree):
    tree = quiet_tree
    tree.run("issue", "raise", "--from", "pm1tl1", "--reason", "fixture data missing")
    tree.run("send", "--from", "pm1", "--to", "pm3", "--kind", "note", "--body", "shared asset pipeline")
    tree.set_load("0.40")
    tree.fake_behavior(load_after_launch="0.95")
    tree.run("conductor", "tick")
    launches = tree.launches()
    assert len(launches) == 1
    status = tree.status()
    assert isinstance(status, dict)
    launched = launches[0]["env"].get("YGG_NODE")
    assert launched in {"pm1", "pm3"}
    assert {"pm1", "pm3"} - {launched} <= set(status["held_launches"])


@pytest.mark.ac("AC-47")
@pytest.mark.parametrize("action,target", [
    ("push", "main"), ("delete", "assets/legacy"), ("spend", "USD 40 ads"),
], ids=["push", "delete", "spend"])
def test_hardstop_decision_never_provisional(tree, action, target):
    tree.active_envelope()
    tree.run("decision", "raise", "--from", "pm1", "--question", "Push to main now?", "--option", "A", "--option", "B",
             "--recommended", "A", "--reversible", "yes", "--undo", "revert merge", "--aligns", "yes",
             "--action", action, "--target", target)
    assert not has_label(tree.documents("projects/alpha-p1/issues"), "provisional-decision")
    if action == "push":
        pending = tree.pending()
        assert isinstance(pending, dict)
        assert any(item["question"] == "Push to main now?" for item in pending["decisions"])
    assert node(tree.status(), "pm1")["state"] == "blocked"


@pytest.mark.ac("AC-66")
def test_reviewer_cannot_review_other_programme(tree):
    tree.run("node", "add", "rv1", "--role", "reviewer", "--parent", "alpha")
    result = tree.run("morning-go", "build", "--programme", "beta", "--reviewer-node", "rv1")
    assert result.returncode == 3
    assert "REFUSED: cross-domain" in result.stderr


@pytest.mark.ac("AC-66")
def test_reviewer_cannot_write_checkpoint(tree):
    tree.run("node", "add", "rv1", "--role", "reviewer", "--parent", "alpha")
    result = tree.run("tracker", "write", "--as", "rv1", "--project", "alpha-p1", "--kind", "checkpoint", "--body", "looks fine")
    assert result.returncode == 3


@pytest.mark.ac("AC-66")
def test_reviewer_uses_pair_model(tree):
    tree.run("node", "add", "rv1", "--role", "reviewer", "--parent", "alpha")
    resolved = tree.command_json("resolve-model", "rv1")
    assert isinstance(resolved, dict)
    assert resolved["model"] == "claude-opus-5-5"
    assert resolved["effort"] == "high"


@pytest.mark.ac("AC-69")
def test_programme_envelope_refuses_stage_limit(tree):
    result = tree.run("envelope", "grant", "alpha", "--through-stage", "3")
    assert result.returncode == 1
    assert "programme" in result.stderr.lower()
    assert "--until" in result.stderr
    assert "only" in result.stderr.lower()


@pytest.mark.ac("AC-69")
def test_expired_envelope_queues_and_blocks_decision(tree):
    tree.active_envelope()
    tree.at("2026-11-16T08:00:01+01:00")
    tree.run("decision", "raise", "--from", "pm1", "--question", "Rename module?", "--option", "A", "--option", "B",
             "--recommended", "A", "--reversible", "yes", "--undo", "rename back", "--aligns", "yes")
    assert not has_label(tree.documents("projects/alpha-p1/issues"), "provisional-decision")
    pending = tree.pending()
    assert isinstance(pending, dict)
    assert any(item["question"] == "Rename module?" for item in pending["decisions"])
    assert node(tree.status(), "pm1")["state"] == "blocked"
