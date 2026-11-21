import pytest

from yggdrasil import config, launcher
from yggdrasil.util import YggError, redact_profile

DATA = {"node": "pm1tl1", "activation": "act-1", "programme": "alpha", "project": "alpha-p1",
        "model": "claude-opus-5-5", "effort": "high", "cli": "claude-code", "pinned": {}}


def test_agent_env_is_allowlisted_and_identity_is_last(monkeypatch):
    monkeypatch.setattr(config, "profile", lambda: {"launch_env": {"EXTRA_FLAG": "on"}})
    monkeypatch.setenv("GH_TOKEN", "gh-secret-1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-3")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-2")
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.setenv("YGG_NOW", "2026-11-16T08:00:00Z")
    monkeypatch.setenv("YGG_NODE", "user")
    monkeypatch.setenv("YGG_ACTIVATION", "act-old")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "for-claude")
    environment = launcher.child_env(DATA)
    for name in ("GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "YGG_TEST_MODE", "YGG_NOW"):
        assert name not in environment
    assert environment["YGG_NODE"] == "pm1tl1"
    assert environment["YGG_ACTIVATION"] == "act-1"
    assert environment["EXTRA_FLAG"] == "on"
    assert environment["ANTHROPIC_API_KEY"] == "for-claude"


def test_codex_agents_get_only_codex_credentials(monkeypatch):
    monkeypatch.setattr(config, "profile", lambda: {})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "for-claude")
    environment = launcher.child_env(dict(DATA, cli="codex"))
    assert environment["OPENAI_API_KEY"] == "sk-test-2"
    assert "ANTHROPIC_API_KEY" not in environment


@pytest.mark.parametrize("name", ["YGG_NODE", "YGG_ACTIVATION", "YGG_STATE_ROOT"])
def test_launch_env_may_not_set_ygg_variables(name):
    with pytest.raises(YggError) as caught:
        launcher.check_launch_env({"launch_env": {name: "x"}})
    assert caught.value.code == 1
    assert caught.value.message == "launch_env may not set " + name


def test_profile_display_redacts_secret_values():
    shown = redact_profile({"name": "example", "linear_client_secret": "sec-xyz-9",
                            "nested": {"api_token": "abc"}, "hub_base_url": "http://127.0.0.1:9999"})
    assert shown == {"name": "example", "linear_client_secret": "[redacted]",
                     "nested": {"api_token": "[redacted]"}, "hub_base_url": "http://127.0.0.1:9999"}


def test_profile_display_redacts_whole_containers_under_secret_keys():
    shown = redact_profile({"tokens": {"linear": "abc", "github": ["x", "y"]},
                            "client_secrets": ["one", "two"], "pools": {"live": "pool-a"}})
    assert shown == {"tokens": "[redacted]", "client_secrets": "[redacted]", "pools": {"live": "pool-a"}}
