import pytest


@pytest.mark.ac("AC-01")
def test_launch_refuses_stalled_suspect_live_activation(a7_tree):
    a7_tree.stalled_suspect_seed()
    starts_before = len(a7_tree.launches("pm1tl1"))

    result = a7_tree.run("launch", "pm1tl1")

    assert result.returncode == 3
    assert result.stderr.rstrip("\n") == (
        "pm1tl1 has a live activation; release or reset it first"
    )
    assert len(a7_tree.launches("pm1tl1")) == starts_before


@pytest.mark.ac("AC-01")
def test_launch_refuses_queued_live_activation(a7_tree):
    a7_tree.seed_activation("pm1tl1", "--state", "queued", "--process", "live")

    result = a7_tree.run("launch", "pm1tl1")

    assert result.returncode == 3
    assert result.stderr.rstrip("\n") == (
        "pm1tl1 has a live activation; release or reset it first"
    )


@pytest.mark.ac("AC-01")
def test_launch_accepts_queued_dead_activation(a7_tree):
    a7_tree.seed_activation("pm1tl1", "--state", "queued", "--process", "dead")

    result = a7_tree.run("launch", "pm1tl1")

    assert result.returncode == 0
    assert len(a7_tree.launches("pm1tl1")) == 1


@pytest.mark.ac("AC-04")
def test_parent_cannot_write_senior_user_criteria(a7_tree):
    a7_tree.package("WP-41")

    result = a7_tree.run(
        "tracker", "write", "--as", "pm1su1", "--project", "alpha-p1",
        "--package", "WP-41", "--kind", "criteria", "--body", "WP-41",
        env={"YGG_NODE": "pm1"},
    )

    assert result.returncode == 3
    assert result.stderr.rstrip("\n") == (
        "criteria for pm1su1 must be written by pm1su1 itself"
    )


@pytest.mark.ac("AC-04")
def test_parent_cannot_record_team_lead_review(a7_tree):
    a7_tree.package("WP-41")

    result = a7_tree.run(
        "review", "record", "--from", "pm1tl1", "--package", "WP-41",
        "--round", "1", "--verdict", "pass", env={"YGG_NODE": "pm1"},
    )

    assert result.returncode == 3
    assert result.stderr.rstrip("\n") == (
        "review for pm1tl1 must be recorded by pm1tl1 itself"
    )


@pytest.mark.ac("AC-04")
def test_senior_user_writes_own_criteria(a7_tree):
    a7_tree.package("WP-41")

    result = a7_tree.run(
        "tracker", "write", "--as", "pm1su1", "--project", "alpha-p1",
        "--package", "WP-41", "--kind", "criteria", "--body", "WP-41",
        env={"YGG_NODE": "pm1su1"},
    )

    assert result.returncode == 0


@pytest.mark.ac("AC-05")
def test_managed_node_cannot_launch(a7_tree):
    result = a7_tree.run("launch", "pm1tl1", env={"YGG_NODE": "pm1"})

    assert result.returncode == 3
    assert result.stderr.rstrip("\n") == "REFUSED: user-only: 'launch' is a user verb and cannot run in a managed session"


@pytest.mark.ac("AC-05")
def test_managed_activation_cannot_tick_conductor(a7_tree):
    result = a7_tree.run(
        "conductor", "tick", env={"YGG_ACTIVATION": "act-0001", "YGG_NODE": None},
    )

    assert result.returncode == 3
    assert result.stderr.rstrip("\n") == "REFUSED: user-only: 'conductor tick' is a user verb and cannot run in a managed session"


@pytest.mark.ac("AC-05")
def test_managed_node_cannot_rehydrate(a7_tree):
    result = a7_tree.run("rehydrate", env={"YGG_NODE": "pm1"})

    assert result.returncode == 3
    assert result.stderr.rstrip("\n") == "REFUSED: user-only: 'rehydrate' is a user verb and cannot run in a managed session"
