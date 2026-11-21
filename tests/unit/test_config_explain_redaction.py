"""`ygg config explain` never prints a secret: a value under a secret-looking key is hidden whole and token-shaped
strings are masked (pre-publication review, 21 Nov 2026)."""
from __future__ import annotations

import json

from yggdrasil import config

SECRET = "sk-ant-api03-" + "a" * 40


def _values(monkeypatch, values):
    monkeypatch.setattr(config, "explain_all", lambda project=None, package=None: dict(values))


def test_explain_all_hides_a_launch_env_secret(monkeypatch):
    _values(monkeypatch, {"launch_env": ({"ANTHROPIC_API_KEY": SECRET, "WORKER_GUIDANCE": "/opt/guide.md"},
                                         "profile:/home/u/profile.yaml")})
    text = config.explain(None, None, None, False)
    assert SECRET not in text
    assert "[redacted]" in text
    assert "/opt/guide.md" in text
    items = json.loads(config.explain(None, None, None, True))
    shown = next(i for i in items if i["key"] == "launch_env")["value"]
    assert shown == {"ANTHROPIC_API_KEY": "[redacted]", "WORKER_GUIDANCE": "/opt/guide.md"}


def test_explain_one_key_masks_a_token_shaped_value(monkeypatch):
    _values(monkeypatch, {"launch_env": ({"NOTE": "key is " + SECRET}, "profile:/p.yaml")})
    text = config.explain("launch_env", None, None, False)
    assert SECRET not in text
    data = json.loads(config.explain("launch_env", None, None, True))
    assert SECRET not in json.dumps(data)


def test_ordinary_values_are_shown_unchanged(monkeypatch):
    _values(monkeypatch, {"tracker": ("files", "core"), "max_concurrent_sessions": (2, "config:/c.yaml")})
    assert json.loads(config.explain("tracker", None, None, True))["value"] == "files"
    assert json.loads(config.explain("max_concurrent_sessions", None, None, True))["value"] == 2


def test_the_linear_token_location_keys_are_shown(monkeypatch):
    _values(monkeypatch, {"linear_token_store": ("file", "config:/c.yaml"),
                          "linear_token_file": ("/home/u/.config/ygg/linear.json", "config:/c.yaml")})
    assert json.loads(config.explain("linear_token_store", None, None, True))["value"] == "file"
    assert json.loads(config.explain("linear_token_file", None, None, True))["value"] == "/home/u/.config/ygg/linear.json"


def test_validation_messages_never_echo_a_secret():
    problems = config.value_problems({"max_concurrent_sessions": {"api_key": "plain-secret-value"},
                                      "stage_close": SECRET})
    text = "\n".join(problems)
    assert "plain-secret-value" not in text and SECRET not in text
    assert any("max_concurrent_sessions" in p for p in problems)
    assert any("stage_close" in p for p in problems)


def test_ordinary_validation_messages_are_unchanged():
    problems = config.value_problems({"stage_close": "sometimes"})
    assert any("stage_close must be default, auto or pm-only, not 'sometimes'" in p for p in problems)


def test_doctor_masks_a_token_in_the_denylist_path(monkeypatch):
    from yggdrasil import doctor, tracker
    monkeypatch.setattr(config, "get", lambda key, default=None: ("/tmp/" + SECRET + "/deny.txt")
                        if key == "leak_denylist" else default)
    monkeypatch.setattr(config, "profile", lambda: {})
    monkeypatch.setattr(doctor, "_linear_ready", lambda: (False, "no Linear token"))
    monkeypatch.setattr(doctor, "_t3_ready", lambda: (False, "t3 command not found"))
    monkeypatch.setattr(tracker, "selected", lambda: "files")
    data = doctor.report()
    assert SECRET not in json.dumps(data)
    assert data["leak_check"]["denylist"].startswith("/tmp/")


def test_explain_masks_a_token_in_the_source_label_and_in_a_mapping_key(monkeypatch):
    _values(monkeypatch, {"launch_env": ({SECRET: "x"}, "profile:/home/u/" + SECRET + "/profile.yaml")})
    for as_json in (False, True):
        assert SECRET not in config.explain(None, None, None, as_json)
        assert SECRET not in config.explain("launch_env", None, None, as_json)


def test_a_plugin_check_message_never_quotes_a_plain_secret(monkeypatch):
    import pytest
    from yggdrasil import util
    monkeypatch.setattr(config, "untrusted_code_settings", lambda: [])
    monkeypatch.setattr(config, "misplaced_agreements", lambda: [])
    monkeypatch.setattr(config, "file_layers", lambda: [("core", {})])
    monkeypatch.setattr(config, "_scoped_layers", lambda project: [])
    monkeypatch.setattr(config, "_pid_only_everywhere", lambda: [])
    monkeypatch.setattr(config, "value_problems", lambda values: [])
    monkeypatch.setattr(config, "effective_unvalidated", lambda: {})
    monkeypatch.setattr(config, "get", lambda key, default=None:
                        {"api_key": "plain-secret-value"} if key == "tracker" else default)
    with pytest.raises(util.YggError) as caught:
        config.validate()
    assert "plain-secret-value" not in str(caught.value)
    assert "tracker" in str(caught.value)


def test_doctor_hides_a_plain_secret_nested_in_the_denylist(monkeypatch):
    from yggdrasil import doctor, tracker
    monkeypatch.setattr(config, "get", lambda key, default=None:
                        {"api_key": "plain-secret-value"} if key == "leak_denylist" else default)
    monkeypatch.setattr(config, "profile", lambda: {})
    monkeypatch.setattr(doctor, "_linear_ready", lambda: (False, "no Linear token"))
    monkeypatch.setattr(doctor, "_t3_ready", lambda: (False, "t3 command not found"))
    monkeypatch.setattr(tracker, "selected", lambda: "files")
    assert "plain-secret-value" not in json.dumps(doctor.report())


def test_status_hides_a_plain_secret_nested_in_the_cadence(monkeypatch):
    from yggdrasil import status
    monkeypatch.setattr(config, "profile", lambda: {"agile_cadence_default": {"api_key": "plain-secret-value"}})
    monkeypatch.setattr(config, "get", lambda key, default=None: default)
    monkeypatch.setattr(config, "explain_all", lambda project=None, package=None: {})
    approach, source, cadence = status.effective_delivery("p1")
    assert "plain-secret-value" not in json.dumps([approach, source, cadence])
