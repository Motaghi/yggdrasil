import json
import re

import pytest


INIT_ENV = {
    "YGG_CONFIG": None,
    "YGG_PROFILE": None,
    "YGG_TRACKER": None,
    "YGG_STATE_ROOT": None,
    "YGG_FILES_ROOT": None,
}


def _config(driver, **settings):
    path = driver.root / "yggdrasil.yaml"
    driver.write_yaml(path, {"schema": "ygg.config.v1", **settings})
    driver.env["YGG_CONFIG"] = str(path)
    driver.env.pop("YGG_TRACKER", None)
    return path


def _json(result):
    assert result.stdout.strip(), "The signed example requires JSON output"
    return json.loads(result.stdout)


@pytest.fixture
def review_layers(tree):
    _config(tree, preset="solo-builder")
    profile = tree.root / "profile.yaml"
    tree.write_yaml(profile, {"name": "example", "review_rounds": 4})
    tree.env["YGG_PROFILE"] = str(profile)
    tree.pid(review_rounds=5)
    return tree


@pytest.fixture
def awaiting_stage_two(tree):
    tree.at("2026-11-16T09:00:00Z")
    tree.stage_executing(2)
    tree.report(2)
    tree.run("stage", "end", "alpha-p1", "--within-tolerance", "yes",
             "--report", "end-stage-2.md")
    return tree


def _memory_plugin(driver, api_version=1):
    directory = driver.make_cwd("plugins")
    driver.write(
        directory / "ygg_memtracker.py",
        f"api_version = {api_version}\n\n"
        "class Tracker:\n"
        "    api_version = api_version\n",
    )
    metadata = directory / "ygg_memtracker-0.1.dist-info"
    driver.write(metadata / "METADATA",
                 "Metadata-Version: 2.1\nName: ygg_memtracker\nVersion: 0.1\n")
    driver.write(metadata / "entry_points.txt",
                 "[yggdrasil.trackers]\nmemory = ygg_memtracker:Tracker\n")
    driver.env["YGG_PLUGIN_PATH"] = str(directory)


@pytest.mark.ac("AC-75")
def test_starter_init_writes_config_with_comment_before_every_key(empty):
    directory = empty.make_cwd("init-starter")

    result = empty.run("init", "--preset", "starter", "--non-interactive",
                       cwd=directory, env=INIT_ENV)

    assert result.returncode == 0
    path = directory / "yggdrasil.yaml"
    assert empty.exists(path)
    text = empty.read(path)
    assert "preset: starter" in text
    assert "tracker: files" in text
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^\s*[^\s#][^:]*:", line):
            assert index > 0 and lines[index - 1].startswith("#")


@pytest.mark.ac("AC-75")
def test_starter_config_runs_demo_quickstart(empty):
    directory = empty.make_cwd("init-quickstart")
    init = empty.run("init", "--preset", "starter", "--non-interactive",
                     cwd=directory, env=INIT_ENV)
    assert init.returncode == 0

    validate = empty.run("config", "validate", cwd=directory, env=INIT_ENV)
    assert validate.returncode == 0
    home = empty.make_cwd("init-quickstart/demo-home")
    programme = empty.run("programme", "create", "demo", "--domain", "demo",
                          "--home", home, "--kind", "vault-docs",
                          cwd=directory, env=INIT_ENV)
    assert programme.returncode == 0
    project = empty.run("project", "create", "demo-p1", "--programme", "demo",
                        "--pm", "pm1", cwd=directory, env=INIT_ENV)
    assert project.returncode == 0
    status = empty.run("status", cwd=directory, env=INIT_ENV)
    assert status.returncode == 0
    assert "demo-p1" in status.stdout


@pytest.mark.ac("AC-75")
def test_init_answers_select_solo_builder_codex_and_prince2(empty):
    directory = empty.make_cwd("init-answers")
    empty.write_yaml(directory / "answers.yaml", {
        "schema": "ygg.init-answers.v1",
        "use": "software",
        "ai_tool": "codex",
        "tracker": "files",
        "autonomy": "overnight-within-limits",
        "vocabulary": "prince2",
    })

    result = empty.run("init", "--non-interactive", "--answers", "answers.yaml",
                       cwd=directory, env=INIT_ENV)

    assert result.returncode == 0
    text = empty.read(directory / "yggdrasil.yaml")
    assert "preset: solo-builder" in text
    assert "launcher: headless" in text
    assert "launcher_cli: codex" in text
    assert "vocabulary: prince2" in text


@pytest.mark.ac("AC-76")
def test_config_explain_project_review_rounds_come_from_pid(review_layers):
    result = review_layers.run("config", "explain", "review_rounds",
                               "--project", "alpha-p1", "--json")

    assert result.returncode == 0
    assert _json(result) == {
        "key": "review_rounds", "value": 5, "source": "pid:alpha-p1",
    }


@pytest.mark.ac("AC-76")
def test_config_explain_global_review_rounds_come_from_profile(review_layers):
    result = review_layers.run("config", "explain", "review_rounds", "--json")

    assert result.returncode == 0
    output = _json(result)
    assert output["value"] == 4
    assert output["source"].startswith("profile:")


@pytest.mark.ac("AC-76")
def test_config_explain_review_rounds_without_profile_come_from_preset(review_layers):
    result = review_layers.run("config", "explain", "review_rounds", "--json",
                               env={"YGG_PROFILE": None})

    assert result.returncode == 0
    output = _json(result)
    assert output["value"] == 2
    assert output["source"] == "preset:solo-builder"


@pytest.mark.ac("AC-77")
def test_config_validate_unknown_key_suggests_tracker(empty):
    _config(empty, tracekr="files")

    result = empty.run("config", "validate")

    assert result.returncode == 1
    assert "unknown key 'tracekr'" in result.stderr
    assert "did you mean 'tracker'" in result.stderr


@pytest.mark.ac("AC-77")
def test_config_validate_refuses_vocabulary_in_pid(tree):
    tree.pid(vocabulary="prince2")

    result = tree.run("config", "validate", "--project", "alpha-p1")

    assert result.returncode == 1
    assert "vocabulary cannot be set in a PID" in result.stderr


@pytest.mark.ac("AC-77")
def test_config_validate_unknown_tracker_lists_files_and_linear(empty):
    _config(empty, tracker="memroy")

    result = empty.run("config", "validate")

    assert result.returncode == 1
    assert "unknown tracker 'memroy'" in result.stderr
    assert "files" in result.stderr
    assert "linear" in result.stderr


@pytest.mark.ac("AC-78")
def test_plain_status_labels_awaiting_approval_as_phase(awaiting_stage_two):
    _config(awaiting_stage_two, vocabulary="plain")

    result = awaiting_stage_two.run("status")

    assert result.returncode == 0
    assert "alpha-p1: waiting for your approval, phase 2" in result.stdout.splitlines()


@pytest.mark.ac("AC-78")
def test_prince2_status_labels_awaiting_approval_as_stage(awaiting_stage_two):
    _config(awaiting_stage_two, vocabulary="prince2")

    result = awaiting_stage_two.run("status")

    assert result.returncode == 0
    assert "alpha-p1: awaiting approval, stage 2" in result.stdout.splitlines()


@pytest.mark.ac("AC-78")
def test_status_json_is_identical_under_plain_and_prince2(awaiting_stage_two):
    _config(awaiting_stage_two, vocabulary="plain")
    plain = awaiting_stage_two.run("status", "--json")
    _config(awaiting_stage_two, vocabulary="prince2")
    prince2 = awaiting_stage_two.run("status", "--json")

    assert plain.stdout == prince2.stdout
    assert _json(plain)["projects"][0]["state"] == "awaiting-approval"


@pytest.mark.ac("AC-79")
def test_entry_point_tracker_plugin_is_listed_with_distribution_and_api(empty):
    _memory_plugin(empty)

    result = empty.run("plugins", "list", "--json")

    assert result.returncode == 0
    assert {
        "group": "trackers", "name": "memory",
        "distribution": "ygg_memtracker", "api_version": 1,
    } in _json(result)


@pytest.mark.ac("AC-79")
def test_entry_point_tracker_plugin_is_selectable_in_config(empty):
    _memory_plugin(empty)
    _config(empty, tracker="memory")

    validate = empty.run("config", "validate")
    assert validate.returncode == 0
    explain = empty.run("config", "explain", "tracker", "--json")
    assert _json(explain)["value"] == "memory"


@pytest.mark.ac("AC-79")
def test_config_validate_refuses_plugin_api_version_two(empty):
    _memory_plugin(empty, api_version=2)
    _config(empty, tracker="memory")

    result = empty.run("config", "validate")

    assert result.returncode == 1
    assert "api_version 2 not supported" in result.stderr
