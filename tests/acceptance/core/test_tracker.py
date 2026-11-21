import pytest

from observations import comments, document_body, document_title, field, has_label


def scalar_values(value):
    if isinstance(value, dict):
        return [item for nested in value.values() for item in scalar_values(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in scalar_values(nested)]
    return [value]


@pytest.mark.ac("AC-29")
def test_files_programme_project_mapping(empty):
    empty.domain_repositories()
    empty.run("programme", "create", "alpha", "--domain", "alpha", "--home", empty.repo_a, "--kind", "git-repo")
    empty.run("project", "create", "alpha-p1", "--programme", "alpha", "--pm", "pm1")
    assert empty.tracker_text("initiatives/alpha/initiative.json")
    project = empty.tracker_json("projects/alpha-p1/project.json")
    assert isinstance(project, dict)
    assert "alpha" in scalar_values(project)


@pytest.mark.ac("AC-29")
def test_files_worker_task_and_checkpoint_mapping(tree):
    tree.package(criteria=True, authorise=True)
    tree.run("tracker", "write", "--as", "pm1tl1", "--project", "alpha-p1", "--package", "WP-41",
             "--worker", "pm1tl1w2", "--kind", "worker-task", "--body", "split save module")
    tree.run("tracker", "write", "--as", "pm1tl1", "--project", "alpha-p1", "--package", "WP-41",
             "--kind", "checkpoint", "--body", "WP-41 60 percent done")
    issues = tree.documents("projects/alpha-p1/issues")
    assert any(field(text, "parent") == "WP-41" and document_title(text) == "split save module" for _, text in issues)
    package = tree.tracker_text("projects/alpha-p1/issues/WP-41.md")
    body = comments(package)
    assert body is not None
    assert "WP-41 60 percent done" in body


@pytest.mark.ac("AC-29")
def test_files_highlight_health(tree):
    tree.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1", "--kind", "highlight",
             "--body", "health: offTrack; Boat outage")
    assert any(field(text, "health") == "offTrack" for _, text in tree.documents("projects/alpha-p1/updates"))


@pytest.mark.ac("AC-30")
@pytest.mark.parametrize("node,writer", [
    ("pm1tl1", "pm1tl1w2"), ("pm1su1w1", "pm1su1w1"),
], ids=["agent-tool-worker", "codex-worker"])
def test_worker_write_refused_without_tracker_change(tree, node, writer):
    tree.package()
    before = tree.snapshot()
    result = tree.run("tracker", "write", "--as", writer, "--project", "alpha-p1", "--package", "WP-41",
                      "--kind", "checkpoint", "--body", "done", env={"YGG_NODE": node})
    assert result.returncode == 3
    assert tree.snapshot() == before


@pytest.mark.ac("AC-30")
def test_lead_write_changes_tracker(tree):
    tree.package()
    before = tree.snapshot()
    result = tree.run("tracker", "write", "--as", "pm1tl1", "--project", "alpha-p1", "--package", "WP-41",
                      "--kind", "checkpoint", "--body", "done", env={"YGG_NODE": "pm1tl1"})
    assert result.returncode == 0
    assert tree.snapshot() != before


@pytest.mark.ac("AC-54")
def test_lead_issue_informs_pm_without_alert(tree):
    before = tree.state_text("alerts.jsonl")
    tree.run("issue", "raise", "--from", "pm1tl1", "--reason", "fixture data missing")
    assert has_label(tree.documents("projects/alpha-p1/issues"), "issue")
    assert "fixture data missing" in tree.run("inbox", "pm1").stdout
    assert tree.state_text("alerts.jsonl") == before


@pytest.mark.ac("AC-54")
def test_lead_cannot_raise_exception(tree):
    result = tree.run("exception", "raise", "--from", "pm1tl1", "--reason", "stage will slip", "--plan", "add a day")
    assert result.returncode == 3
    assert "REFUSED: exceptions are raised by the PM" in result.stderr


@pytest.mark.ac("AC-63")
def test_invalid_project_id_cannot_create_external_files(tree):
    before = tree.snapshot("outside-tracker")
    result = tree.run("project", "create", "../../etc", "--programme", "alpha", "--pm", "pm9")
    assert result.returncode == 1
    assert "../../etc" in result.stderr
    assert tree.snapshot("outside-tracker") == before


@pytest.mark.ac("AC-63")
def test_secret_is_scrubbed_from_stored_update(tree):
    secret = "sk-" + "ant-" + "api03-AbCdEf123"
    result = tree.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1", "--kind", "highlight",
                      "--body", "key " + secret + " leaked")
    assert result.returncode == 0
    updates = tree.documents("projects/alpha-p1/updates")
    assert any("[REDACTED]" in document_body(text) for _, text in updates)
    assert all(secret not in text for _, text in updates)
