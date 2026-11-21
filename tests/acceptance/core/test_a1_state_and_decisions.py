import pytest

from a1_observations import alerts, entry, header, records
from observations import comments, has_label


@pytest.mark.ac("AC-08")
def test_files_rehydrate_restores_full_inflight_state(tree):
    tree.at("2026-11-16T02:00:00+01:00")
    tree.stage_executing(2)
    tree.decision(number=3, scope="lane:pm1tl2")
    tree.active_envelope()
    tree.run("test", "set-next", "message", "10")
    tree.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "note", "--body", "API contract ready")
    tree.set_load("0.95", now=tree.env["YGG_NOW"])
    tree.run("issue", "raise", "--from", "pm1tl1", "--reason", "fixture data missing")
    tree.run("conductor", "tick")
    tree.run("test", "set-next", "activation", "12")
    tree.seed_activation("pm1su1", "--outcome", "uncertain")
    tree.empty_state()
    tree.run("rehydrate")
    status = tree.status()
    assert isinstance(status, dict)
    assert {"alpha", "beta"} <= {item["id"] for item in status["programmes"]}
    parents = {"pm1": "alpha", "pm2": "beta", "pm3": "alpha", "pm1tl1": "pm1",
               "pm1tl2": "pm1", "pm1su1": "pm1", "pm1su2": "pm1", "pm1tl1w1": "pm1tl1",
               "pm1tl1w2": "pm1tl1", "pm1tl2w1": "pm1tl2", "pm1su1w1": "pm1su1",
               "pm1su1w2": "pm1su1", "pm2tl1": "pm2", "pm2su1": "pm2", "pm1rv1": "pm1"}
    nodes = [item for item in status["nodes"] if item["id"] != "l1"]
    assert len(nodes) == 15
    # PMs belong to the domain-neutral L1; programme membership is a separate field.
    for item in nodes:
        if item["id"] in {"pm1", "pm2", "pm3"}:
            assert item["parent"] == "l1"
            assert item["programme"] == parents[item["id"]]
        else:
            assert item["parent"] == parents[item["id"]]
    assert "D-0003" in status["decisions_open"]
    project = entry(status, "projects", "alpha-p1")
    assert project["envelope"]["until"] == "2026-11-16T07:00:00Z"
    assert project["stage"]["number"] == 2
    assert project["stage"]["state"] == "executing"
    assert any(item["node"] == "pm1tl1" and item["msg_id"] == "m-0010" for item in status["inbox_pending"])
    assert "pm1" in status["held_launches"]
    assert "act-0012" in status["uncertain_keys"]


@pytest.mark.ac("AC-09")
def test_lane_decision_blocks_own_branch_and_user_answer_releases_it(tree):
    tree.decision(number=4, scope="lane:pm1tl1")
    pending = tree.pending()
    decision = entry(pending, "decisions", "D-0004")
    assert decision["question"] == "Ship tutorial skip?"
    assert [option["label"] for option in decision["options"]] == ["A", "B"]
    assert decision["recommended"] == "A"
    assert decision["scope"] == {"type": "lane", "id": "pm1tl1"}
    status = tree.status()
    assert entry(status, "nodes", "pm1tl1")["state"] == "blocked"
    for identifier in ("pm1tl2", "pm2", "pm3"):
        assert entry(status, "nodes", identifier)["state"] != "blocked"
    tree.run("decide", "D-0004", "--option", "B", "--note", "keep it")
    text = tree.tracker_text("projects/alpha-p1/issues/D-0004.md")
    assert "decided" in header(text)["labels"]
    assert "Decided: B. Note: keep it" in comments(text)
    assert "D-0004" not in {item["id"] for item in tree.pending()["decisions"]}
    assert entry(tree.status(), "nodes", "pm1tl1")["state"] != "blocked"


@pytest.mark.ac("AC-10")
def test_aligned_reversible_decision_records_provisional_choice_and_undo(tree):
    tree.active_envelope()
    tree.run("decision", "raise", "--from", "pm1", "--question", "Use placeholder art?",
             "--option", "A", "--option", "B", "--recommended", "A", "--reversible", "yes",
             "--undo", "swap art back in commit range", "--aligns", "yes")
    documents = tree.documents("projects/alpha-p1/issues")
    decisions = [header(text) for _, text in documents if "provisional-decision" in header(text).get("labels", [])]
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision["chosen"] == "A"
    assert decision["undo"] == "swap art back in commit range"
    status = tree.status()
    assert entry(status, "nodes", "pm1")["state"] != "blocked"
    assert any(item["kind"] == "provisional-decision" and item["ref"] == decision["id"]
               for item in tree.json_records("initiatives/alpha/morning-go"))


@pytest.mark.ac("AC-10")
def test_reversible_decision_without_undo_is_invalid_and_writes_no_record(tree):
    tree.active_envelope()
    before = tree.documents("projects/alpha-p1/issues")
    result = tree.run("decision", "raise", "--from", "pm1", "--question", "Use placeholder art?",
                      "--option", "A", "--option", "B", "--recommended", "A", "--reversible", "yes", "--aligns", "yes")
    assert result.returncode == 1
    assert "--undo" in result.stderr
    assert tree.documents("projects/alpha-p1/issues") == before


@pytest.mark.ac("AC-10")
def test_unaligned_decision_is_pending_and_blocks_pm(tree):
    tree.active_envelope()
    tree.run("decision", "raise", "--from", "pm1", "--question", "Use placeholder art?",
             "--option", "A", "--option", "B", "--recommended", "A", "--reversible", "yes",
             "--undo", "swap art back", "--aligns", "no")
    pending = tree.pending()
    assert isinstance(pending, dict)
    assert any(item["question"] == "Use placeholder art?" for item in pending["decisions"])
    assert entry(tree.status(), "nodes", "pm1")["state"] == "blocked"


@pytest.mark.ac("AC-11")
def test_stage_end_inside_time_envelope_self_approves_and_starts_next_stage(tree):
    tree.stage_executing(2)
    tree.active_envelope()
    tree.at("2026-11-16T03:00:00+01:00")
    tree.report(2)
    tree.run("stage", "end", "alpha-p1", "--within-tolerance", "yes", "--report", "end-stage-2.md")
    milestones = tree.tracker_json("projects/alpha-p1/milestones.json")
    assert isinstance(milestones, dict)
    assert entry(milestones, "stages", 2)["approved_by"] == "envelope"
    project = entry(tree.status(), "projects", "alpha-p1")
    assert project["stage"]["number"] == 3
    assert project["stage"]["state"] == "executing"
    assert any(item["kind"] == "stage-end" and item["ref"] == "stage-2"
               for item in tree.json_records("initiatives/alpha/morning-go"))


@pytest.mark.ac("AC-11")
@pytest.mark.parametrize("stage,through,time", [
    (2, False, "2026-11-16T09:00:00+01:00"),
    (3, True, "2026-11-16T03:00:00+01:00"),
], ids=["expired-time", "through-stage-boundary"])
def test_stage_end_waits_when_envelope_does_not_cover_boundary(tree, stage, through, time):
    tree.stage_executing(stage)
    tree.at("2026-11-16T02:00:00+01:00")
    if through:
        tree.run("envelope", "grant", "alpha-p1", "--through-stage", "3")
    else:
        tree.active_envelope()
    tree.at(time)
    tree.report(stage)
    before = len(alerts(tree, "ygg-stage-ready"))
    tree.run("stage", "end", "alpha-p1", "--within-tolerance", "yes", "--report", "end-stage-" + str(stage) + ".md")
    project = entry(tree.status(), "projects", "alpha-p1")
    assert project["state"] == "awaiting-approval"
    milestones = tree.tracker_json("projects/alpha-p1/milestones.json")
    assert isinstance(milestones, dict)
    assert entry(milestones, "stages", stage + 1)["state"] != "executing"
    if not through:
        assert len(alerts(tree, "ygg-stage-ready")) - before == 1


@pytest.mark.ac("AC-13")
@pytest.mark.parametrize("node,action,target,all_grants", [
    ("pm1tl1", "delete", "assets/legacy", False),
    ("pm1", "send-external", "forum post", False),
    ("pm1tl1", "spend", "USD 40 ads", True),
], ids=["delete", "send-external", "spend-with-every-grant"])
def test_hardstops_remain_denied_inside_envelopes(tree, node, action, target, all_grants):
    if all_grants:
        tree.write_yaml(tree.tracker / "initiatives/alpha/documents/autonomy.yaml", {
            "schema": "ygg.autonomy.v1", "actor": "user",
            "standing_grants": ["land-lane", "push-lane", "merge-main-local", "self-approve-stage", "provisional-decision"],
            "hard_stops": ["push-outside-ygg", "force", "delete", "rewrite-history", "send-external", "spend", "workflow-change"],
        })
        tree.at("2026-11-16T02:00:00+01:00")
        tree.run("envelope", "grant", "alpha-p1", "--until", "2026-11-16T08:00+01:00", "--allow-local-merge")
    else:
        tree.active_envelope()
    before = len(alerts(tree, "ygg-blocker"))
    result = tree.run("hardstop", "check", "--node", node, "--action", action, "--target", target)
    assert result.returncode == 4
    if action == "delete":
        assert len(alerts(tree, "ygg-blocker")) - before == 1


@pytest.mark.ac("AC-15")
def test_dead_node_relaunches_before_retry_limit_without_alert(tree):
    tree.ready()
    tree.at("2026-11-16T10:25:00+01:00")
    tree.seed_activation("pm1tl1", "--state", "running", "--process", "dead", "--heartbeat",
                         "2026-11-16T10:00:00+01:00", "--retries-used", "0")
    tree.clear_calls()
    tree.run("heimdall", "tick")
    assert len(tree.launches("pm1tl1")) == 1
    assert not alerts(tree, "ygg-stall")


@pytest.mark.ac("AC-15")
def test_retry_exhaustion_marks_stalled_and_alerts_once(tree):
    tree.ready()
    tree.at("2026-11-16T10:25:00+01:00")
    tree.seed_activation("pm1tl1", "--state", "running", "--process", "dead", "--heartbeat",
                         "2026-11-16T10:00:00+01:00", "--retries-used", "2")
    tree.run("heimdall", "tick")
    assert entry(tree.status(), "nodes", "pm1tl1")["state"] == "stalled"
    lines = alerts(tree, "ygg-stall")
    assert len(lines) == 1
    assert "pm1tl1" in str(lines[0])


@pytest.mark.ac("AC-15")
def test_silent_live_process_is_suspect_and_is_not_relaunched(tree):
    tree.ready()
    tree.at("2026-11-16T10:05:00+01:00")
    tree.seed_activation("pm1tl1", "--state", "running", "--process", "live", "--heartbeat", "2026-11-16T09:00:00+01:00")
    tree.clear_calls()
    tree.run("heimdall", "tick")
    assert entry(tree.status(), "nodes", "pm1tl1")["state"] == "stalled-suspect"
    assert not tree.launches("pm1tl1")
    lines = alerts(tree, "ygg-stall")
    assert len(lines) == 1
    assert "pm1tl1" in str(lines[0])


@pytest.mark.ac("AC-16")
def test_admission_uses_load_and_freshness_without_session_cap(tree):
    tree.set_load("0.95")
    assert tree.run("heimdall", "admit").returncode == 6
    for number in range(40):
        node = "pm1tl1w" + str(number + 3)
        tree.run("node", "add", node, "--role", "worker", "--parent", "pm1tl1")
        tree.seed_activation(node, "--state", "running", "--process", "none")
    tree.set_load("0.40")
    assert tree.run("heimdall", "admit").returncode == 0
    tree.set_load("0.40", age_seconds=600)
    assert tree.run("heimdall", "admit").returncode == 6
