"""A current-folder config may not name anything Yggdrasil or its agents run, nor where the Linear credentials
live: every such key is trusted-only."""

import pytest

from yggdrasil import config
from yggdrasil.util import YggError


KEYS = ["t3_bin", "alert_command", "merge_check_path", "agent_claude_config_dir", "linear_token_store",
        "linear_token_file", "linear_keychain_service", "agent_codex_login_dir"]


def _clear():
    config.file_layers.cache_clear()
    config.profile.cache_clear()
    config.effective.cache_clear()


def _config_layer() -> dict:
    return next(values for label, values in config.file_layers() if label.startswith("config:"))


def _current_folder_config(tmp_path, monkeypatch, text):
    for name in ("YGG_CONFIG", "YGG_PROFILE", "YGG_NODE", "YGG_ACTIVATION", "YGG_PLUGIN_PATH", "YGG_T3_BIN"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "yggdrasil.yaml").write_text(text)
    monkeypatch.chdir(tmp_path)
    _clear()
    assert not config.user_config_trusted()


@pytest.mark.parametrize("key", KEYS)
def test_a_current_folder_config_naming_something_to_run_is_refused(tmp_path, monkeypatch, key):
    assert key in config.TRUSTED_ONLY_KEYS
    _current_folder_config(tmp_path, monkeypatch, key + ": " + str(tmp_path / "planted") + "\n")
    try:
        expected = str((tmp_path / "yggdrasil.yaml").resolve()) + ": " + key + " " + config.TRUSTED_ONLY
        assert expected in config.untrusted_code_settings()
        with pytest.raises(YggError) as caught:
            config.validate(check_plugins=False)
        assert expected in caught.value.message
        assert key not in _config_layer()
    finally:
        _clear()


@pytest.mark.parametrize("key", KEYS)
def test_the_same_key_is_honoured_from_a_config_named_by_ygg_config(tmp_path, monkeypatch, key):
    _current_folder_config(tmp_path, monkeypatch, key + ": " + str(tmp_path / "planted") + "\n")
    monkeypatch.setenv("YGG_CONFIG", str(tmp_path / "yggdrasil.yaml"))
    _clear()
    try:
        assert config.user_config_trusted()
        assert config.untrusted_code_settings() == []
        assert key in _config_layer()
    finally:
        _clear()


def test_a_current_folder_t3_bin_is_never_executed(tmp_path, monkeypatch):
    from yggdrasil import t3
    planted = tmp_path / "t3"
    planted.write_text("#!/bin/sh\ntouch " + str(tmp_path / "ran") + "\n")
    planted.chmod(0o755)
    _current_folder_config(tmp_path, monkeypatch, "t3_bin: " + str(planted) + "\n")
    try:
        assert t3.binary() is None
        assert not (tmp_path / "ran").exists()
    finally:
        _clear()
