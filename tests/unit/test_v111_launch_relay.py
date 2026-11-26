"""v1.1.1: the hermetic Codex home, the version pin, the Codex command line and environments, and the relay's
verb allowlist and server protocol."""

import json
import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from yggdrasil import codexlaunch, launcher, relay, state, util


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    return tmp_path


def _test_pin(monkeypatch, tmp_path, version, platform="darwin"):
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir(exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.setenv("YGG_CODEX_BIN", str(binary))
    if version is None:
        monkeypatch.delenv("YGG_TEST_CODEX_VERSION", raising=False)
    else:
        monkeypatch.setenv("YGG_TEST_CODEX_VERSION", version)
    if platform is None:
        monkeypatch.delenv("YGG_TEST_PLATFORM", raising=False)
    else:
        monkeypatch.setenv("YGG_TEST_PLATFORM", platform)
    return binary


# ---------------------------------------------------------------- the version pin (CB-D13)

@pytest.mark.parametrize("found, shown", [
    ("codex-cli 0.171.0", "codex-cli 0.171.0"),
    ("codex-cli 0.170.0-alpha.1", "codex-cli 0.170.0-alpha.1"),
    ("codex-cli 0.170.01", "codex-cli 0.170.01"),
    ("codex-cli 0.170.0\nextra", "an unknown Codex version"),
    ("garbage", "an unknown Codex version"),
    ("", "an unknown Codex version"),
])
def test_pin_refuses_every_line_but_the_probed_one(root, monkeypatch, found, shown):
    _test_pin(monkeypatch, root, found)
    with pytest.raises(util.YggError) as error:
        codexlaunch.check_builder()
    assert error.value.code == util.EXIT_REFUSED
    assert ("launch precondition: " + shown + " has not been probed with Yggdrasil; Codex builders run only on "
            "codex-cli 0.170.0") in error.value.message


def test_pin_accepts_the_probed_line_with_one_trailing_newline(root, monkeypatch):
    binary = _test_pin(monkeypatch, root, "codex-cli 0.170.0\n")
    found = codexlaunch.check_builder()
    assert found["version"] == "codex-cli 0.170.0" and found["probed"]
    assert found["path"] == os.path.realpath(binary)
    assert codexlaunch.same_binary(found)
    assert codexlaunch.version_text(found) == "codex version: codex-cli 0.170.0"


def test_pin_checks_the_platform_first_and_helpers_use_their_own_prefix(root, monkeypatch):
    _test_pin(monkeypatch, root, "codex-cli 0.171.0", platform="linux")
    with pytest.raises(util.YggError) as error:
        codexlaunch.check_builder()
    assert ("launch precondition: Codex builders have not been probed on linux; they run only on darwin with "
            "codex-cli 0.170.0") in error.value.message
    with pytest.raises(util.YggError) as error:
        codexlaunch.check_helper()
    assert "re-check refused: Codex builders have not been probed on linux" in error.value.message
    assert "launch precondition" not in error.value.message


def test_pin_is_not_checked_in_test_mode_without_the_test_variables(root, monkeypatch):
    _test_pin(monkeypatch, root, None, platform=None)
    found = codexlaunch.check_builder()
    assert not found["checked"]
    assert codexlaunch.version_text(found) == "codex version: not checked (test mode)"


def test_reviewer_version_is_recorded_never_refused(root, monkeypatch):
    _test_pin(monkeypatch, root, "codex-cli 0.171.0", platform="linux")
    found = codexlaunch.probe(strict=False)
    assert not found["probed"]
    assert codexlaunch.version_text(found) == "codex version: codex-cli 0.171.0 (not probed)"


def test_a_changed_binary_is_an_unknown_version(root, monkeypatch):
    binary = _test_pin(monkeypatch, root, "codex-cli 0.170.0")
    found = codexlaunch.check_builder()
    binary.write_text("#!/bin/sh\necho changed\nexit 0\n")
    assert not codexlaunch.same_binary(found)


def test_real_version_run_uses_a_trusted_environment_and_one_line(root, monkeypatch):
    record = root / "seen-env"
    binary = root / "codex"
    binary.write_text("#!/bin/sh\nenv > " + str(record) + "\nprintf 'codex-cli 0.170.0\\n'\n")
    binary.chmod(0o755)
    monkeypatch.delenv("YGG_TEST_MODE", raising=False)
    monkeypatch.setenv("YGG_CODEX_BIN", str(binary))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-pass")
    monkeypatch.setattr(sys, "platform", "darwin")
    found = codexlaunch.check_builder()
    assert found["version"] == "codex-cli 0.170.0"
    names = {line.split("=", 1)[0] for line in record.read_text().splitlines() if "=" in line}
    assert "OPENAI_API_KEY" not in names
    seen = dict(line.split("=", 1) for line in record.read_text().splitlines() if "=" in line)
    assert seen["TMPDIR"].startswith(str(state.root()))


def test_test_controls_never_reach_an_agent(root, monkeypatch):
    for name in ("YGG_TEST_CODEX_VERSION", "YGG_TEST_PLATFORM", "YGG_TEST_RELAY_DELAY", "YGG_TEST_RELAY_HOLD",
                 "YGG_TEST_ASSEMBLY_HOLD", "YGG_RELAY", "YGG_VIA_RELAY"):
        assert name in launcher.AGENT_ENV_DENIED
        monkeypatch.setenv(name, str(root / "x"))
    environment = launcher.child_env({"node": "n1", "activation": "act-0001", "cli": "codex"})
    assert not any(name in environment for name in launcher.AGENT_ENV_DENIED)


# ---------------------------------------------------------------- the hermetic home (CB-D10)

def test_hermetic_home_holds_only_config_and_never_copies_the_users_login(root, monkeypatch):
    monkeypatch.setenv("HOME", str(root / "home"))  # no Yggdrasil Codex login there
    monkeypatch.setenv("TMPDIR", str(root / "tmpdir"))
    source = root / "user-codex"
    source.mkdir()
    (source / "auth.json").write_bytes(b'{"secret": 1}')
    (source / "AGENTS.md").write_text("untrusted")
    (source / "config.toml").write_text(
        'model_provider = "hub"\napproval_policy = "on-request"\nnotify = ["/x"]\n'
        '[model_providers.hub]\nname = "hub"\nbase_url = "http://127.0.0.1:1/v1"\nenv_key = "K"\n'
        'wire_api = "responses"\nrequires_openai_auth = true\nhttp_headers = { "X-A" = "b" }\n'
        '[mcp_servers.rec]\ncommand = "/x"\n[[hooks.PreToolUse]]\ncommand = "/x"\n[profiles.p]\n'
        '[projects."/Users/x"]\ntrust_level = "trusted"\n')
    monkeypatch.setenv("CODEX_HOME", str(source))
    home = codexlaunch.write_home("act-0001")["home"]
    assert sorted(os.listdir(home)) == ["config.toml"]
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    data = tomllib.loads((home / "config.toml").read_text())
    assert set(data) == {"model_provider", "approval_policy", "sandbox_mode", "web_search", "sandbox_workspace_write",
                         "agents", "skills", "features", "model_providers", "cli_auth_credentials_store",
                         "shell_environment_policy"}
    assert data["shell_environment_policy"] == {"exclude": ["CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN",
                                                            "K"]}
    assert data["approval_policy"] == "never" and data["sandbox_mode"] == "workspace-write"
    assert data["model_providers"] == {"hub": {"name": "hub", "base_url": "http://127.0.0.1:1/v1", "env_key": "K",
                                               "wire_api": "responses", "http_headers": {"X-A": "b"}}}
    assert data["features"] == {name: False for name in codexlaunch.CODEX_DISABLED_FEATURES}
    assert data["agents"] == {"enabled": False} and data["skills"] == {"include_instructions": False}
    assert data["sandbox_workspace_write"] == {"writable_roots": [], "network_access": False,
                                               "exclude_slash_tmp": True, "exclude_tmpdir_env_var": True}


def test_hermetic_home_without_user_config_or_auth(root, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(root / "missing"))
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("TMPDIR", str(root / "tmpdir"))
    home = codexlaunch.write_home("act-0002", reviewer=True)["home"]
    assert os.listdir(home) == ["config.toml"]
    data = tomllib.loads((home / "config.toml").read_text())
    assert "model_provider" not in data and "sandbox_mode" not in data


def test_disabled_feature_list_is_the_signed_one():
    assert codexlaunch.CODEX_DISABLED_FEATURES == (
        "hooks", "apps", "plugins", "remote_plugin", "plugin_sharing", "browser_use", "browser_use_external",
        "computer_use", "in_app_browser", "in_app_local_automation", "multi_agent", "skill_mcp_dependency_install",
        "image_generation", "tool_suggest", "system_proxy_fallback", "respect_system_proxy")


# ---------------------------------------------------------------- the command line and environment

def _builder_data(root, role="tl", kind="lane"):
    box = root / "box"
    return {"node": "pm1tl1", "activation": "act-0007", "role": role, "cli": "codex", "model": "gpt-6.1-sol",
            "effort": "high", "box": str(box), "cwd": str(box / "checkout"), "box_kind": kind,
            "relay": str(state.root() / "relay" / "act-0007"), "codex_home": str(state.root() / "launch/x/codex-home"),
            "codex_pin": {"path": "/opt/codex"}, "pinned": {"YGG_STATE_ROOT": str(state.root())}}


def test_builder_command_line_names_only_the_box_and_relay_in(root):
    data = _builder_data(root)
    argv = launcher.codex_builder_command(data)
    assert argv[:2] == ["/opt/codex", "exec"] and argv[-1] == "-"
    pairs = [(a, argv[i + 1]) for i, a in enumerate(argv[:-1])]
    assert [v for a, v in pairs if a == "--cd"] == [data["box"]]
    assert [v for a, v in pairs if a == "--add-dir"] == [data["relay"] + "/in"]
    assert {v for a, v in pairs if a == "--disable"} == set(codexlaunch.CODEX_DISABLED_FEATURES)
    for value in ('approval_policy="never"', "mcp_servers={}", 'web_search="disabled"', "agents.enabled=false",
                  "skills.include_instructions=false", "sandbox_workspace_write.writable_roots=[]",
                  "sandbox_workspace_write.network_access=false", "sandbox_workspace_write.exclude_slash_tmp=true",
                  "sandbox_workspace_write.exclude_tmpdir_env_var=true", "model_reasoning_effort=high"):
        assert value in [v for a, v in pairs if a == "-c"]
    assert "--ignore-rules" in argv and "--strict-config" in argv


def test_builder_environment_pins_relay_home_and_temporary_folders(root, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/users/own/codex")
    monkeypatch.setenv("YGG_RELAY", "/elsewhere")
    data = _builder_data(root)
    environment = launcher.codex_builder_env(data)
    assert environment["CODEX_HOME"] == data["codex_home"]
    assert environment["YGG_RELAY"] == data["relay"]
    for key in ("TMPDIR", "TMP", "TEMP"):
        assert environment[key] == data["box"] + "/tmp"
    assert environment["XDG_CACHE_HOME"] == data["box"] + "/tmp/cache"


@pytest.mark.parametrize("kind", ["lane", "scratch"])
def test_builder_environment_stops_git_discovery_above_the_box(root, monkeypatch, kind):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", "/elsewhere")
    data = _builder_data(root, kind=kind)
    environment = launcher.codex_builder_env(data)
    # Git never stops at a ceiling equal to the folder it starts in, so the ceiling is the box's parent.
    assert environment["GIT_CEILING_DIRECTORIES"] == str(Path(data["box"]).parent)


def test_builder_git_from_the_box_root_finds_no_repository(root):
    import subprocess
    home = root / "home"
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    box = home / ".ygg-worktrees" / "alpha-p1" / "lane" / "pm1tl1.g000001"
    (box / ".git").mkdir(parents=True)  # the empty anchor, which git does not take for a repository
    subprocess.run(["git", "init", "-q", str(box / "checkout")], check=True)
    data = dict(_builder_data(root), box=str(box), cwd=str(box / "checkout"))
    environment = launcher.codex_builder_env(data)
    environment["PATH"] = os.environ.get("PATH", "")
    environment.pop("GIT_DIR", None)
    environment.pop("GIT_WORK_TREE", None)

    def toplevel(folder):
        return subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=folder, env=environment,
                              capture_output=True, text=True)

    from_root = toplevel(box)
    assert from_root.returncode == 128 and "not a git repository" in from_root.stderr
    from_checkout = toplevel(box / "checkout")
    assert from_checkout.returncode == 0
    assert os.path.realpath(from_checkout.stdout.strip()) == os.path.realpath(box / "checkout")


def test_fchdir_spawn_starts_in_the_descriptor(root):
    import subprocess
    folder = root / "work"
    folder.mkdir()
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        result = subprocess.run([sys.executable, "-c", "import os; print(os.getcwd())"], capture_output=True,
                                text=True, preexec_fn=launcher._fchdir(descriptor), check=True)
    finally:
        os.close(descriptor)
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(folder)


def test_open_work_area_refuses_a_replaced_box(root):
    box = root / "box"
    (box / "checkout").mkdir(parents=True)
    found = os.stat(box)
    data = {"box": str(box), "box_device": found.st_dev, "box_inode": found.st_ino, "box_kind": "lane"}
    descriptor, refusal = launcher._open_work_area(data)
    assert refusal is None
    os.close(descriptor)
    os.rename(box, root / "old")
    (box / "checkout").mkdir(parents=True)
    descriptor, refusal = launcher._open_work_area(data)
    assert descriptor is None and "was replaced" in refusal
    import shutil
    shutil.rmtree(box / "checkout")
    (box / "checkout").symlink_to(root / "old" / "checkout")
    data.update(box_device=os.stat(box).st_dev, box_inode=os.stat(box).st_ino)
    descriptor, refusal = launcher._open_work_area(data)
    assert descriptor is None and "cannot be opened" in refusal


# ---------------------------------------------------------------- the relay: verbs, files, environment

def _top_level_parsers():
    from yggdrasil import cli
    parser = cli.build()
    action = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    return action.choices


def test_relay_verb_allowlist_excludes_internal_and_user_verbs():
    for verb in relay.MANAGED_VERBS:
        assert verb not in relay.INTERNAL_VERBS and verb not in state.USER_VERBS
    for verb in ("hook", "push-guard", "lane-check", "_supervise", "_tend", "launch", "test", "init", "demo"):
        assert not relay.is_relayable([verb])
    assert set(relay.MANAGED_VERBS) <= set(_top_level_parsers())


def test_every_relayable_file_option_is_carried():
    """A relayable verb that reads a file option must have that option in FILE_OPTIONS, or the relayed child
    would read a path the agent named."""
    file_dests = {"body_file", "file", "report", "answers", "message_file", "plan_file"}

    def walk(parser):
        yield parser
        for action in parser._actions:
            if action.__class__.__name__ == "_SubParsersAction":
                for sub in action.choices.values():
                    yield from walk(sub)

    for verb, parser in _top_level_parsers().items():
        if verb not in relay.MANAGED_VERBS:
            continue
        for sub in walk(parser):
            for action in sub._actions:
                if action.dest in file_dests:
                    assert all(option in relay.FILE_OPTIONS for option in action.option_strings), \
                        (verb, action.option_strings)


def test_relay_normalises_and_positions_file_options():
    argv = relay.normalise(["send", "--from", "a", "--body-file=x.txt"])
    assert argv == ["send", "--from", "a", "--body-file", "x.txt"]
    assert relay.file_positions(argv) == [4]


def test_relay_limits_per_verb():
    assert relay.verb_limit(["send"]) == 600
    assert relay.verb_limit(["wp", "ready"]) == relay.verb_limit(["lane", "sync"]) == 1800
    assert relay.verb_limit(["wp", "cancel", "p"]) == 600


def _server(root, role="tl", cli_data=None):
    activation = "act-0007"
    fields = relay.make_folder(activation)
    data = dict(_builder_data(root, role), **fields, agent_nonce="n" * 32, programme="alpha", project="alpha-p1")
    data.update(cli_data or {})
    return relay.Server(activation, data, {"id": data["node"], "role": role}), data


@pytest.mark.parametrize("role", ["tl", "pm", "su"])
def test_relayed_child_environment_is_built_from_the_allowlist(root, monkeypatch, role):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-y")
    monkeypatch.setenv("LINEAR_API_KEY", "lin")
    monkeypatch.setenv("MY_PASSTHROUGH", "p")
    monkeypatch.setenv("CODEX_HOME", "/home/codex")
    monkeypatch.setenv("YGG_RELAY", "/r")
    monkeypatch.setenv("YGG_SUPERVISOR_FOR", "act-0007")
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.setenv("YGG_TEST_RELAY_HOLD", str(root / "hold"))
    monkeypatch.setenv("LC_ALL", "C")
    server, data = _server(root, role)
    server.sequence = 3
    temporary = server.root / "tmp" / ("a" * 32)
    environment = server._environment("a" * 32, temporary)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LINEAR_API_KEY", "MY_PASSTHROUGH", "CODEX_HOME",
                 "YGG_RELAY", "YGG_SUPERVISOR_FOR"):
        assert name not in environment
    assert environment["LC_ALL"] == "C"
    assert environment["YGG_NODE"] == data["node"] and environment["YGG_VIA_RELAY"] == "act-0007"
    assert environment["YGG_ACTIVATION_NONCE"] == "n" * 32
    assert environment["YGG_TEST_RELAY_SEQ"] == "3"
    assert environment["TMPDIR"] == str(temporary) and environment["XDG_CACHE_HOME"] == str(temporary / "cache")
    for value in environment.values():
        for inside in (data["box"], str(state.root() / "scratch"), str(server.root / "in")):
            assert not value.startswith(inside)
    server.close_descriptors()


def _plant(server, name, text=None, target=None):
    path = server.root / "in" / name
    if target is not None:
        path.symlink_to(target)
    else:
        path.write_text(text)
    return path


def _out(server, request_id):
    return json.loads((server.root / "out" / (request_id + ".json")).read_text())


def test_server_refuses_linked_large_and_malformed_requests(root):
    server, _ = _server(root)
    source = root / "req.json"
    source.write_text(json.dumps({"argv": ["status"], "stdin": "", "files": {}}))
    _plant(server, "1" * 32 + ".json", target=source)
    server.serve_one()
    assert "refused: not a regular file" in _out(server, "1" * 32)["stderr"]
    _plant(server, "2" * 32 + ".json", '["inbox"]')
    server.serve_one()
    assert "refused: not one JSON object" in _out(server, "2" * 32)["stderr"]
    _plant(server, "3" * 32 + ".json", json.dumps({"argv": ["status"], "stdin": "", "files": {}, "env": {}}))
    server.serve_one()
    assert "refused: unknown key env" in _out(server, "3" * 32)["stderr"]
    _plant(server, "4" * 32 + ".json", json.dumps({"argv": ["launch", "x"], "stdin": "", "files": {}}))
    server.serve_one()
    assert "launch is not available to managed sessions" in _out(server, "4" * 32)["stderr"]
    _plant(server, "5" * 32 + ".json", json.dumps({"argv": ["send", "--body-file", "/etc/hosts"], "stdin": "",
                                                    "files": {"1": "x"}}))
    server.serve_one()
    assert "refused: files must name" in _out(server, "5" * 32)["stderr"]
    assert source.read_text().startswith("{")
    server.close_descriptors()


def test_used_id_rule_never_runs_or_answers_twice(root):
    server, _ = _server(root)
    request_id = "6" * 32
    _plant(server, request_id + ".json", '["x"]')
    server.serve_one()
    first = (server.root / "out" / (request_id + ".json")).read_bytes()
    _plant(server, request_id + ".json", json.dumps({"argv": ["status"], "stdin": "", "files": {}}))
    server.serve_one()
    assert (server.root / "out" / (request_id + ".json")).read_bytes() == first
    assert not (server.root / "in" / (request_id + ".json")).exists()
    lines = [json.loads(line) for line in (server.root / "refused.jsonl").read_text().splitlines()]
    assert [(line["id"], line["reason"]) for line in lines] == [(request_id, "id already used")]
    server._respond(request_id, 0, "", "")
    assert (server.root / "out" / (request_id + ".json")).read_bytes() == first
    assert json.loads((server.root / "refused.jsonl").read_text().splitlines()[-1])["reason"] == \
        "response already written"
    server.close_descriptors()


def test_withdrawal_is_written_by_the_server_and_blocks_the_id(root):
    server, _ = _server(root)
    request_id = "7" * 32
    _plant(server, request_id + ".json", json.dumps({"argv": ["status"], "stdin": "", "files": {}}))
    _plant(server, request_id + ".withdraw", "")
    server.withdrawals()
    record = json.loads((server.root / "withdrawn" / (request_id + ".json")).read_text())
    assert record["id"] == request_id
    assert os.listdir(server.root / "in") == []
    _plant(server, request_id + ".json", json.dumps({"argv": ["status"], "stdin": "", "files": {}}))
    server.serve_one()
    assert not (server.root / "out" / (request_id + ".json")).exists()
    server.close_descriptors()


def test_withdrawal_after_take_is_only_recorded(root):
    server, _ = _server(root)
    request_id = "8" * 32
    _plant(server, request_id + ".json", '["x"]')
    server.serve_one()
    _plant(server, request_id + ".withdraw", "")
    server.withdrawals()
    assert not (server.root / "withdrawn" / (request_id + ".json")).exists()
    assert "withdrawal after take" in (server.root / "refused.jsonl").read_text()
    server.close_descriptors()


def test_unremovable_entry_is_refused_once(root):
    server, _ = _server(root)
    request_id = "9" * 32
    _plant(server, request_id + ".json", '["x"]')
    server.serve_one()
    (server.root / "in" / (request_id + ".json")).mkdir()
    for _ in range(5):
        server.serve_one()
        server.answer_waiting("x {id}")
    assert len((server.root / "refused.jsonl").read_text().splitlines()) == 1
    server.close_descriptors()


def test_stop_and_drain_answer_waiting_requests(root):
    server, _ = _server(root)
    first, second = "a" * 32, "b" * 32
    _plant(server, first + ".json", json.dumps({"argv": ["status"], "stdin": "", "files": {}}))
    server.stop()
    server.answer_waiting("relay: the session is stopping; request {id} was not run")
    assert _out(server, first)["exit"] == 75
    assert "request " + first + " was not run" in _out(server, first)["stderr"]
    _plant(server, second + ".json", json.dumps({"argv": ["status"], "stdin": "", "files": {}}))
    server.stopping = False
    server.drain()
    assert "the session ended before request " + second + " ran" in _out(server, second)["stderr"]
    assert sorted(os.listdir(server.root / "taken")) == [first + ".json", second + ".json"]
    server.close_descriptors()


def test_replaced_in_folder_serves_nothing_more(root):
    server, _ = _server(root)
    os.rename(server.root / "in", server.root / "in-old")
    (server.root / "in").mkdir()
    _plant(server, "c" * 32 + ".json", json.dumps({"argv": ["status"], "stdin": "", "files": {}}))
    server.serve_one()
    assert not (server.root / "out" / ("c" * 32 + ".json")).exists()
    assert "the request folder was replaced" in (server.root / "refused.jsonl").read_text()
    server.close_descriptors()


def test_archive_keeps_the_record_and_removes_the_agent_folder(root):
    server, _ = _server(root)
    _plant(server, "d" * 32 + ".json", '["x"]')
    server.serve_one()
    _plant(server, "e" * 32 + ".json", "late")
    server.close_descriptors()
    relay.archive("act-0007")
    kept = state.root() / "output" / "act-0007" / "relay"
    assert sorted(os.listdir(kept)) == ["out", "taken", "withdrawn"]
    assert not (state.root() / "relay" / "act-0007").exists()
    assert not (state.root() / "relay" / "_removing" / "act-0007-in").exists()


def test_client_reports_unknown_without_a_supervisor_record(root, monkeypatch, capsys):
    folder = state.root() / "relay" / "act-0009"
    for name in relay.FOLDERS:
        (folder / name).mkdir(parents=True)
    monkeypatch.setenv("YGG_RELAY", str(folder))
    monkeypatch.setattr(relay, "ALIVE_STALE", 0.3)
    monkeypatch.setattr(relay, "WITHDRAW_WAIT", 0.3)
    assert relay.client_main(["status"]) == 75
    assert relay.UNKNOWN in capsys.readouterr().err
    names = os.listdir(folder / "in")
    assert len([n for n in names if n.endswith(".json")]) == 1
    assert len([n for n in names if n.endswith(".withdraw")]) == 1
    assert not [n for n in names if n.startswith(".tmp-")]


def test_server_refuses_a_request_that_does_not_upload_every_file_it_names(root):
    server, _ = _server(root)
    _plant(server, "6" * 32 + ".json", json.dumps({"argv": ["send", "--body-file", "a.txt", "--file", "b.txt"],
                                                    "stdin": "", "files": {"2": "x"}}))
    server.serve_one()
    assert "refused: files must name" in _out(server, "6" * 32)["stderr"]
    assert not any((server.root / "files").iterdir())
    server.close_descriptors()


def _status_request(server, request_id):
    _plant(server, request_id + ".json", json.dumps({"argv": ["status"], "stdin": "", "files": {}}))


@pytest.mark.parametrize("kind,scope,text", [
    ("pause", "alpha", "Yggdrasil is paused for this scope; stop work now."),
    ("pause", None, "Yggdrasil is paused for this scope; stop work now."),
    ("stop", "pm1tl1", "Yggdrasil emergency stop for this node; stop work now."),
    ("stop", "alpha", "Yggdrasil emergency stop for this node; stop work now."),
])
def test_a_paused_or_stopped_node_gets_no_relayed_call(root, monkeypatch, kind, scope, text):
    from yggdrasil import safety
    server, _ = _server(root)
    started = []
    monkeypatch.setattr(server, "_start", lambda request_id, value: started.append(request_id))
    safety.publish_marker(kind, scope)
    request_id = "7" * 32
    _status_request(server, request_id)
    server.serve_one()
    assert started == []
    response = _out(server, request_id)
    assert response["exit"] == 1
    assert response["stderr"] == "relay: request " + request_id + " refused: " + text + "\n"
    assert sorted(os.listdir(server.root / "taken")) == [request_id + ".json"]
    server.close_descriptors()


def test_a_pause_of_another_programme_or_a_stop_of_another_node_does_not_refuse(root, monkeypatch):
    from yggdrasil import safety
    server, _ = _server(root)
    started = []
    monkeypatch.setattr(server, "_start", lambda request_id, value: started.append(request_id))
    safety.publish_marker("pause", "beta")
    safety.publish_marker("stop", "pm2tl1")
    request_id = "8" * 32
    _status_request(server, request_id)
    server.serve_one()
    assert started == [request_id]
    assert not (server.root / "out" / (request_id + ".json")).exists()
    server.close_descriptors()


def test_a_call_already_running_when_the_pause_lands_finishes(root, monkeypatch):
    from yggdrasil import safety

    class Done:
        pid = os.getpid()
        returncode = 0

        def poll(self):
            return 0

    server, _ = _server(root)
    request_id = "9" * 32
    _status_request(server, request_id)

    def start(request_id, value):
        capture = server.root / "tmp" / (request_id + ".run")
        capture.mkdir(mode=0o700)
        (capture / "stdout").write_text("done\n")
        (capture / "stderr").write_text("")
        server.child = {"id": request_id, "process": Done(), "deadline": 1e18, "files": [],
                        "tmp": server.root / "tmp" / request_id, "capture": capture, "killed_at": None}
    monkeypatch.setattr(server, "_start", start)
    monkeypatch.setattr("yggdrasil.interlock.group_alive", lambda pid: False)
    server.serve_one()
    safety.publish_marker("pause", "alpha")
    server.reap()
    response = _out(server, request_id)
    assert response["exit"] == 0 and response["stdout"] == "done\n"
    later = "a1" * 16
    _status_request(server, later)
    server.serve_one()
    assert "refused: Yggdrasil is paused for this scope; stop work now." in _out(server, later)["stderr"]
    server.close_descriptors()
