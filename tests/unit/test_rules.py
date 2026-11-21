import pytest

from yggdrasil import util
from yggdrasil.commands import validate_pid
from yggdrasil.ratatoskr import topology_reason


def node(identifier, parent, domain="alpha", role="tl"):
    return {"id": identifier, "parent": parent, "domain": domain, "role": role}


@pytest.mark.parametrize("sender,recipient,expected", [
    (node("pm1tl1", "pm1"), node("pm1tl2", "pm1"), None),
    (node("pm1", "l1", role="pm"), node("pm1tl1", "pm1"), None),
    (node("pm1tl1", "pm1"), node("pm1", "l1", role="pm"), None),
    (node("pm1tl1", "pm1"), node("pm1tl2w1", "pm1tl2", role="worker"), "topology"),
    (node("pm1", "l1", role="pm"), node("pm2", "l1", domain="beta", role="pm"), "cross-domain"),
    (node("pm1", "l1", role="pm"), node("pm3", "l1", role="pm"), None),
    (node("rv1", "alpha", role="reviewer"), node("pm1", "l1", role="pm"), "topology"),
])
def test_topology(sender, recipient, expected):
    assert topology_reason(sender, recipient) == expected


def pid(**changes):
    data = {"schema": "ygg.pid.v1", "project": "alpha-p1", "programme": "alpha", "repos": [],
            "stages": [{"number": 1, "planned_end": "2026-11-16T17:00:00Z"}], "tolerances": {"time": "P1D"},
            "business_case": {"why": "w", "benefit": "b", "cost": {}, "risks": [], "stop_if": "s"}}
    data.update(changes)
    return data


@pytest.mark.parametrize("changes,field", [
    ({"business_case": {"why": "w"}}, "business_case.benefit"),
    ({"business_case": "skipped"}, "business_case_skip_reason"),
    ({"delivery_approach": "custom"}, "delivery_description"),
    ({"cadence": "timebox", "timebox_hours": 0}, "timebox_hours"),
    ({"tolerances": {}}, "tolerances.time"),
    ({"draft": True}, "draft"),
])
def test_pid_validation_names_the_field(changes, field):
    with pytest.raises(util.YggError) as error:
        validate_pid(pid(**changes))
    assert error.value.code == 1
    assert field in error.value.message


def test_valid_pid_passes():
    validate_pid(pid())


def test_redaction_and_ids():
    secret = "sk-" + "ant-" + "api03-" + "x" * 12
    assert util.redact("key " + secret + " end") == "key [REDACTED] end"
    with pytest.raises(util.YggError):
        util.check_id("../../etc")
    with pytest.raises(util.YggError):
        util.check_stage("1/../../x")
    assert util.check_stage("3") == 3


def test_node_add_caller_rules(tmp_path, monkeypatch):
    from yggdrasil import commands
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "tracker"))
    monkeypatch.setenv("YGG_TRACKER", "files")
    monkeypatch.delenv("YGG_NODE", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    commands.programme_create("alpha", "alpha", str(home), "vault-docs")
    commands.project_create("alpha-p1", "alpha", "pm1")
    commands.project_create("alpha-p2", "alpha", "pm2")
    monkeypatch.setenv("YGG_NODE", "pm1")
    commands.node_add("pm1tl1", "tl", "pm1")
    with pytest.raises(util.YggError) as error:
        commands.node_add("pm1su1", "su", "pm1", "pm1tl1")
    assert "user-only" in error.value.message
    monkeypatch.delenv("YGG_NODE")
    commands.node_add("pm1su1", "su", "pm1", "pm1tl1")
    monkeypatch.setenv("YGG_NODE", "pm1")
    with pytest.raises(util.YggError) as error:
        commands.node_add("pm2tl1", "tl", "pm2")
    assert error.value.code == 3
    with pytest.raises(util.YggError) as error:
        commands.node_add("rv1", "reviewer", "alpha")
    assert "user-only" in error.value.message
    with pytest.raises(util.YggError):
        commands.project_create("alpha-p3", "alpha", "pm3")
    monkeypatch.setenv("YGG_NODE", "pm1tl1")
    commands.node_add("pm1tl1w1", "worker", "pm1tl1")
    with pytest.raises(util.YggError):
        commands.node_add("pm1su1w1", "worker", "pm1su1")
    monkeypatch.setenv("YGG_NODE", "l1")
    commands.node_add("pm2tl1", "tl", "pm2")


def test_reworked_stage_is_never_self_approved(tmp_path, monkeypatch):
    import yaml
    from yggdrasil import commands, envelopes, tracker
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "tracker"))
    monkeypatch.setenv("YGG_TRACKER", "files")
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.setenv("YGG_NOW", "2026-11-16T02:00:00Z")
    monkeypatch.delenv("YGG_NODE", raising=False)
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    commands.programme_create("alpha", "alpha", str(home), "vault-docs")
    commands.project_create("alpha-p1", "alpha", "pm1")
    header = pid()
    header["stages"] = [{"number": 1, "planned_end": "2026-11-17T17:00:00Z"},
                        {"number": 2, "planned_end": "2026-11-18T17:00:00Z"}]
    commands.tracker_write("pm1", "alpha-p1", "pid", "---\n" + yaml.safe_dump(header) + "---\n# PID\n")
    commands.pid_approve("alpha-p1")
    (tmp_path / "r.md").write_text("# report\n")
    commands.stage_end("alpha-p1", "yes", "r.md")
    commands.stage_reject("alpha-p1", "rework")
    envelopes.grant("alpha-p1", "2026-11-16T08:00:00Z", None, False)
    commands.stage_end("alpha-p1", "yes", "r.md")
    stages = tracker.milestones("alpha-p1")["stages"]
    assert stages[0]["state"] == "awaiting-approval"
    assert stages[0]["approved_by"] is None
    assert stages[1]["state"] == "planned"
