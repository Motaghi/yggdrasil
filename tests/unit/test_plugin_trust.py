import pytest

from yggdrasil import config
from yggdrasil.util import YggError


def _clear():
    config.file_layers.cache_clear()
    config.profile.cache_clear()
    config.effective.cache_clear()


def test_current_folder_config_cannot_add_plugin_paths(tmp_path, monkeypatch):
    for name in ("YGG_CONFIG", "YGG_PROFILE", "YGG_NODE", "YGG_ACTIVATION", "YGG_PLUGIN_PATH"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "yggdrasil.yaml").write_text("plugin_paths: [./plugins]\nlauncher: other\n")
    monkeypatch.chdir(tmp_path)
    _clear()
    assert not config.user_config_trusted()
    problems = config.untrusted_code_settings()
    assert any(p.endswith(": plugin_paths " + config.TRUSTED_ONLY) for p in problems)
    assert any(": launcher: other " in p for p in problems)
    assert "plugin_paths" not in dict(config.file_layers()[-1][1])
    with pytest.raises(YggError):
        config.validate(check_plugins=False)
    monkeypatch.setenv("YGG_CONFIG", str(tmp_path / "yggdrasil.yaml"))
    _clear()
    assert config.user_config_trusted()
    assert config.untrusted_code_settings() == []
    _clear()


def test_duplicate_of_a_selected_builtin_name_is_refused(monkeypatch):
    from yggdrasil import plugins

    class Entry:
        def __init__(self, dist):
            self.name, self.dist = "files", type("D", (), {"metadata": {"Name": dist}})()

    monkeypatch.setattr(plugins.metadata, "entry_points", lambda group: [Entry("yggdrasil"), Entry("ygg_dupe")])
    monkeypatch.setattr(plugins, "_extend_path", lambda: None)
    with pytest.raises(YggError) as caught:
        plugins.check("trackers", "files")
    assert caught.value.message == "plugin name files is registered twice in yggdrasil.trackers (ygg_dupe, yggdrasil)"
