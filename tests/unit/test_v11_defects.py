"""v1.1 known-defect fixes: pid-ready only for a new, rejected or replanned plan (AC-16, AC-17), send with --from
needs --kind (AC-18), init without a terminal says so (AC-21), secret-looking key names (AC-22), a malformed config
is an error (AC-23), the team lead prompt explains the pair reviewer (AC-24), and the Codex refusal texts."""
from __future__ import annotations

import pytest

from yggdrasil import util
from yggdrasil.util import YggError
from test_round2 import clear_caches, world  # noqa: F401  (fixture and helper)

PID_READY_REASON = "pid-ready is only for a new, rejected or replanned PID"


def _state(project="p1"):
    from yggdrasil import tracker
    return tracker.project(project)["state"]


def _set_state(value, project="p1"):
    from yggdrasil import tracker
    data = tracker.project(project)
    data["state"] = value
    tracker.save_project(data)


def _write_ready(project="p1", node="pm1"):
    from yggdrasil import commands
    return commands.tracker_write(node, project, "pid-ready", "ready")


def _raise_exception(reason="the scope grew", plan="reduce it"):
    import re
    from yggdrasil import commands
    return re.search(r"E-[0-9]+", commands.exception_raise("pm1", reason, plan))[0]


def _pid_ready_alerts():
    from yggdrasil import state
    return sum(1 for entry in state.alerts() if entry.get("kind") == "ygg-pid-ready")


def _follow_up():
    from yggdrasil import tracker
    found = [i for i in tracker.issues("p1") if i.get("kind") == "decision" and i.get("exception")]
    return sorted(found, key=lambda i: i.get("exception"))[-1]


# AC-16 row 1
def test_pid_ready_is_refused_for_an_executing_project(world):
    assert _state() == "executing"
    before = _pid_ready_alerts()
    with pytest.raises(YggError) as caught:
        _write_ready()
    assert caught.value.code == util.EXIT_REFUSED
    assert "p1 is executing" in caught.value.message and PID_READY_REASON in caught.value.message
    assert _state() == "executing" and _pid_ready_alerts() == before


# AC-16 row 2 and AC-17 row 1
def test_pid_ready_after_a_rejected_exception_plan_waits_for_the_replan_answer(world):
    from yggdrasil import commands, decisions
    identifier = _raise_exception()
    commands.exception_decide(identifier, "reject")
    before = _pid_ready_alerts()
    with pytest.raises(YggError) as caught:
        _write_ready()
    assert caught.value.code == util.EXIT_REFUSED and PID_READY_REASON in caught.value.message
    assert _state() == "exception" and _pid_ready_alerts() == before
    decisions.decide(_follow_up().id, "replan", None)
    _write_ready()
    assert _state() == "pid-ready" and _pid_ready_alerts() == before + 1


def test_an_older_replan_answer_does_not_cover_a_newer_rejected_exception(world):
    from yggdrasil import commands, decisions
    first = _raise_exception()
    commands.exception_decide(first, "reject")
    decisions.decide(_follow_up().id, "replan", None)
    second = _raise_exception("it grew again", "reduce it more")
    commands.exception_decide(second, "reject")
    with pytest.raises(YggError) as caught:
        _write_ready()
    assert PID_READY_REASON in caught.value.message and _state() == "exception"


def test_a_close_answer_to_the_follow_up_keeps_pid_ready_refused(world):
    from yggdrasil import commands, decisions
    identifier = _raise_exception()
    commands.exception_decide(identifier, "reject")
    decisions.decide(_follow_up().id, "close", None)
    with pytest.raises(YggError) as caught:
        _write_ready()
    assert PID_READY_REASON in caught.value.message and _state() == "exception"


def _decision_with(question, **extra):
    from yggdrasil import tracker
    identifier = tracker.allocate("decision")
    issue = tracker.new_issue("p1", identifier, "decision", ["decision"], "pm1", question=question,
                              options=["replan", "close"], recommended="replan", **extra)
    issue.save()
    return issue


def test_a_look_alike_decision_does_not_open_the_replan_path(world):
    from yggdrasil import commands
    identifier = _raise_exception()
    commands.exception_decide(identifier, "reject")
    _decision_with("Exception E-9999 plan rejected: what next?", chosen="replan", chosen_by="user")
    with pytest.raises(YggError) as caught:
        _write_ready()
    assert PID_READY_REASON in caught.value.message and _state() == "exception"


def test_an_envelope_answer_does_not_open_the_replan_path(world):
    from yggdrasil import commands, tracker
    identifier = _raise_exception()
    commands.exception_decide(identifier, "reject")
    follow_up = _follow_up()
    follow_up.header.update(chosen="replan", chosen_by="envelope")
    follow_up.save()
    with pytest.raises(YggError):
        _write_ready()
    assert tracker.project("p1")["state"] == "exception"


# AC-16 row 3
def test_pid_ready_is_refused_for_a_closed_project(world):
    from yggdrasil import commands
    commands.project_close("p1", premature=True, note="done")
    with pytest.raises(YggError) as caught:
        _write_ready()
    assert caught.value.code == util.EXIT_REFUSED and "p1 is closed" in caught.value.message
    assert _state() == "closed"


def test_pid_ready_is_refused_when_the_plan_already_waits_for_approval(world):
    _set_state("pid-ready")
    with pytest.raises(YggError) as caught:
        _write_ready()
    assert "p1 is pid-ready" in caught.value.message


# AC-17 rows 2 and 3
def test_pid_ready_is_allowed_for_a_rejected_plan(world):
    _set_state("pid-rejected")
    _write_ready()
    assert _state() == "pid-ready"


def test_pid_ready_is_allowed_for_a_new_project(world):
    from yggdrasil import commands
    commands.project_create("p2", "alpha", "pm2")
    assert _state("p2") == "initiating"
    _write_ready("p2", "pm2")
    assert _state("p2") == "pid-ready"


# AC-18
def test_send_with_from_and_without_kind_names_the_missing_flag(world, capsys):
    from yggdrasil import cli
    assert cli.main(["send", "--from", "tl1", "--to", "pm1", "--body", "hi"]) == util.EXIT_USAGE
    err = capsys.readouterr().err
    assert "--kind is required with --from" in err and "send without --from" not in err


def test_send_from_the_users_shell_still_defaults_to_work(world, capsys):
    from yggdrasil import cli
    assert cli.main(["send", "--to", "pm1", "--body", "hi"]) == 0
    assert "delivered" in capsys.readouterr().out


# AC-21
def test_init_without_a_terminal_says_it_used_the_defaults(tmp_path, monkeypatch, capsys):
    import io
    import sys
    from yggdrasil import init
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    init.run(None, False, None, str(tmp_path / "cfg.yaml"), False)
    assert (tmp_path / "cfg.yaml").exists()
    assert "no terminal: the five questions were skipped and their defaults used" in capsys.readouterr().err
    init.run(None, True, None, str(tmp_path / "cfg2.yaml"), False)
    assert "no terminal" not in capsys.readouterr().err


# AC-22
def test_new_secret_key_names_are_hidden_and_other_values_stay_shown():
    shown = util.redact_profile({"launch_env": {"STRIPE_KEY": "abcdef123456", "AUTH_HEADER": "Bearer abcdef123457",
                                                "SESSION_COOKIE": "sid-abcdef123458", "COOKIE": "x1y2z3w4v5u6",
                                                "LOG_LEVEL": "debug"},
                                 "linear_keychain_service": "com.example.ygg"})
    assert shown["launch_env"] == {"STRIPE_KEY": "[redacted]", "AUTH_HEADER": "[redacted]",
                                   "SESSION_COOKIE": "[redacted]", "COOKIE": "[redacted]", "LOG_LEVEL": "debug"}
    assert shown["linear_keychain_service"] == "com.example.ygg"


@pytest.mark.parametrize("name", ["api.key", "x-auth-y", "Authorization", "my_pat", "BEARER", "request_signature"])
def test_secret_words_split_on_underscore_dash_and_dot_in_any_case(name):
    assert util.redact_profile({name: "value-1"}) == {name: "[redacted]"}


@pytest.mark.parametrize("name", ["keychain", "monkey", "author", "patch", "sessions", "pools", "hostkeys"])
def test_whole_word_match_does_not_hide_longer_names(name):
    assert util.redact_profile({name: "value-1"}) == {name: "value-1"}


@pytest.mark.parametrize("name", ["tokens", "client_secrets", "linear_client_secret", "api_token", "PASSWORD",
                                  "Credential_file", "private_key_path", "apikey", "passwd"])
def test_everything_hidden_before_stays_hidden(name):
    assert util.redact_profile({name: {"a": "value-1"}}) == {name: "[redacted]"}


# AC-23
def test_a_config_that_is_not_valid_yaml_is_an_error_naming_the_file(world, capsys, monkeypatch):
    from yggdrasil import cli
    path = world / "bad.yaml"
    path.write_text("models: [")
    monkeypatch.setenv("YGG_CONFIG", str(path))
    clear_caches()
    assert cli.main(["status"]) == util.EXIT_USAGE
    assert str(path) + ": not valid YAML" in capsys.readouterr().err


def test_a_config_that_is_not_a_mapping_is_an_error_and_an_empty_one_is_fine(world, capsys, monkeypatch):
    from yggdrasil import cli
    path = world / "list.yaml"
    path.write_text("- a\n")
    monkeypatch.setenv("YGG_CONFIG", str(path))
    clear_caches()
    assert cli.main(["status"]) == util.EXIT_USAGE
    assert str(path) + ": not a mapping" in capsys.readouterr().err
    path.write_text("")
    clear_caches()
    assert cli.main(["status"]) == 0


def test_an_error_for_a_bad_config_never_quotes_the_file_text(world, capsys, monkeypatch):
    from yggdrasil import cli
    path = world / "secret.yaml"
    path.write_text("linear_token: sk-abcdef-1234567\nmodels: [\n")
    monkeypatch.setenv("YGG_CONFIG", str(path))
    clear_caches()
    assert cli.main(["status"]) == util.EXIT_USAGE
    assert "sk-abcdef-1234567" not in capsys.readouterr().err


def test_a_missing_config_file_named_by_ygg_config_is_unchanged(world, monkeypatch):
    from yggdrasil import cli
    monkeypatch.setenv("YGG_CONFIG", str(world / "nope.yaml"))
    clear_caches()
    assert cli.main(["status"]) == 0


# AC-24
def test_team_lead_prompt_explains_the_pair_reviewer(world):
    from yggdrasil import launcher, state, tracker
    tracker.write_document("p1", "brief", "# Brief\n\nGoal: ship it.\n")
    lead = launcher.prompt_for(state.all_nodes()["tl1"], "act-0001")
    assert ("When you start your pair reviewer (the agent you start on the configured pair model to check your "
            "work), pass it the project brief verbatim:") in lead
    assert "Sol pair" not in lead


# Codex refusal texts (amendments A-1 to A-3)
def test_codex_refusal_texts_say_not_in_this_release():
    from yggdrasil import models
    assert models.CODEX_REFUSAL == "Codex builds are not in this release"
    import pathlib
    source = pathlib.Path(models.__file__).parent
    for name in ("commands.py", "mergecheck.py", "models.py"):
        text = (source / name).read_text()
        assert "come in v1.1" not in text


# The project is read again under the lock
def _change_state_after_first_read(monkeypatch, new_state, project="p1"):
    from yggdrasil import commands
    real = commands._project_for
    calls = {"n": 0}

    def reading(name):
        data = real(name)
        calls["n"] += 1
        if calls["n"] == 1:
            _set_state(new_state, project)  # a concurrent writer changes the stored state after our first read
        return data

    monkeypatch.setattr(commands, "_project_for", reading)


def test_pid_ready_checks_the_state_stored_when_the_lock_is_held(world, monkeypatch):
    from yggdrasil import commands
    commands.project_create("p2", "alpha", "pm2")
    assert _state("p2") == "initiating"
    _change_state_after_first_read(monkeypatch, "executing", "p2")
    before = _pid_ready_alerts()
    with pytest.raises(YggError) as caught:
        _write_ready("p2", "pm2")
    assert "p2 is executing" in caught.value.message
    assert _state("p2") == "executing" and _pid_ready_alerts() == before


def test_a_highlight_does_not_undo_a_state_change_made_after_its_first_read(world, monkeypatch):
    from yggdrasil import commands, tracker
    _change_state_after_first_read(monkeypatch, "closed")
    commands.tracker_write("pm1", "p1", "highlight", "health: atRisk")
    assert _state() == "closed" and tracker.project("p1")["health"] == "atRisk"


# The malformed-config error is a fixed category, never parser text
@pytest.mark.parametrize("content,secret", [
    (b"a: *Hunter2secret\n", "Hunter2secret"),
    (b"a: !S3cretVal x\n", "S3cretVal"),
    (b"token: sk-abcdef-1234567\nmodels: [\n", "sk-abcdef-1234567"),
    (b"password: hunter2\n\xff\xfe\x00broken\n", "hunter2"),
], ids=["alias", "tag", "unclosed", "bad-utf8"])
def test_a_bad_config_error_never_echoes_the_file(world, capsys, monkeypatch, content, secret):
    from yggdrasil import cli
    path = world / "bad.yaml"
    path.write_bytes(content)
    monkeypatch.setenv("YGG_CONFIG", str(path))
    clear_caches()
    assert cli.main(["status"]) == util.EXIT_USAGE
    err = capsys.readouterr().err
    assert str(path) + ": not valid YAML" in err
    assert secret not in err and "Traceback" not in err
    assert err.strip().startswith(str(path) + ": not valid YAML")
    assert len(err.strip().splitlines()) == 1


@pytest.mark.parametrize("content", ["null\n", "~\n", "---\n...\n", "# only a comment\nnull\n", "42\n", "just text\n"])
def test_an_explicit_null_or_scalar_document_is_not_a_mapping(world, capsys, monkeypatch, content):
    from yggdrasil import cli
    path = world / "scalar.yaml"
    path.write_text(content)
    monkeypatch.setenv("YGG_CONFIG", str(path))
    clear_caches()
    assert cli.main(["status"]) == util.EXIT_USAGE
    assert str(path) + ": not a mapping" in capsys.readouterr().err


@pytest.mark.parametrize("content", ["", "\n\n", "# nothing but a comment\n"])
def test_an_empty_or_comment_only_config_is_an_empty_config(world, monkeypatch, content):
    from yggdrasil import cli
    path = world / "empty.yaml"
    path.write_text(content)
    monkeypatch.setenv("YGG_CONFIG", str(path))
    clear_caches()
    assert cli.main(["status"]) == 0
