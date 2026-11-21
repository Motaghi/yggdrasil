import pytest


@pytest.mark.ac("AC-01")
@pytest.mark.parametrize("sender,recipient,kind,body", [
    ("pm1tl1", "pm1tl2", "note", "API contract ready"),
    ("pm1su1w1", "pm1su1w2", "note", "fixture path changed"),
    ("pm1", "pm1su2", "work", "write criteria for WP-3"),
], ids=["lead-sibling", "worker-sibling", "parent-child"])
def test_allowed_delivery(tree, sender, recipient, kind, body):
    result = tree.run("send", "--from", sender, "--to", recipient, "--kind", kind, "--body", body)
    assert result.returncode == 0
    assert body in tree.run("inbox", recipient).stdout


@pytest.mark.ac("AC-02")
@pytest.mark.parametrize("sender,recipient", [
    ("pm1su1w1", "pm1tl1"), ("pm1tl1", "l1"), ("pm1tl1w1", "pm1su1w1"),
], ids=["worker-other-lead", "lead-root", "unrelated-workers"])
def test_topology_refusal(tree, sender, recipient):
    result = tree.run("send", "--from", sender, "--to", recipient, "--kind", "note", "--body", "hi")
    assert result.returncode == 3
    assert "REFUSED: topology" in result.stderr
    if sender == "pm1su1w1":
        assert "hi" not in tree.run("inbox", recipient).stdout


@pytest.mark.ac("AC-03")
def test_lead_reaches_own_worker(tree):
    result = tree.run("send", "--from", "pm1tl1", "--to", "pm1tl1w2", "--kind", "work", "--body", "rename module")
    assert result.returncode == 0
    assert "rename module" in tree.run("inbox", "pm1tl1w2").stdout


@pytest.mark.ac("AC-03")
@pytest.mark.parametrize("sender,recipient,kind,body", [
    ("pm1tl1", "pm1tl2w1", "work", "help me out"),
    ("pm1su1", "pm1tl1w1", "note", "test this"),
], ids=["other-lead-worker", "senior-user-lead-worker"])
def test_other_workers_refused(tree, sender, recipient, kind, body):
    result = tree.run("send", "--from", sender, "--to", recipient, "--kind", kind, "--body", body)
    assert result.returncode == 3
    assert "REFUSED: topology" in result.stderr
    if sender == "pm1tl1":
        assert body not in tree.run("inbox", recipient).stdout


@pytest.mark.ac("AC-04")
def test_same_domain_pms_deliver(tree):
    result = tree.run("send", "--from", "pm1", "--to", "pm3", "--kind", "note", "--body", "shared asset pipeline")
    assert result.returncode == 0
    assert "shared asset pipeline" in tree.run("inbox", "pm3").stdout


@pytest.mark.ac("AC-04")
def test_cross_domain_pms_refused(tree):
    result = tree.run("send", "--from", "pm1", "--to", "pm2", "--kind", "note", "--body", "share balance sheet")
    assert result.returncode == 3
    assert "REFUSED: cross-domain" in result.stderr


@pytest.mark.ac("AC-05")
def test_each_send_attempt_logged_once(tree):
    tree.write(tree.state / "messages.jsonl", "")
    tree.run("send", "--from", "pm1tl1", "--to", "pm1", "--kind", "note", "--body", "hi")
    tree.run("send", "--from", "pm1su1w1", "--to", "pm1tl1", "--kind", "note", "--body", "hi")
    lines = tree.state_lines("messages.jsonl")
    assert len(lines) == 2
    for line in lines:
        assert {"ts", "from", "to", "kind", "msg_id", "outcome", "provenance"} <= line.keys()
    assert [line["outcome"] for line in lines] == ["delivered", "refused"]
    assert lines[1]["reason"] == "topology"


@pytest.mark.ac("AC-40")
def test_managed_lead_cannot_speak_as_l1(tree):
    result = tree.run("send", "--from", "l1", "--to", "pm2", "--kind", "note", "--body", "hi",
                      env={"YGG_NODE": "pm1tl1"})
    assert result.returncode == 3
    assert "REFUSED: identity" in result.stderr
    assert "hi" not in tree.run("inbox", "pm2").stdout


@pytest.mark.ac("AC-40")
def test_worker_cannot_write_as_lead(tree):
    result = tree.run("tracker", "write", "--as", "pm1tl1", "--project", "alpha-p1", "--package", "WP-41",
                      "--kind", "checkpoint", "--body", "done", env={"YGG_NODE": "pm1tl1w2"})
    assert result.returncode == 3
    assert "REFUSED: identity" in result.stderr


@pytest.mark.ac("AC-40")
def test_lead_can_speak_as_own_child(tree):
    result = tree.run("send", "--from", "pm1tl1w1", "--to", "pm1tl1w2", "--kind", "note", "--body", "fixture moved",
                      env={"YGG_NODE": "pm1tl1"})
    assert result.returncode == 0
    assert "fixture moved" in tree.run("inbox", "pm1tl1w2").stdout
