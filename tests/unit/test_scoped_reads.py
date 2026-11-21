from yggdrasil import state


def test_visible_programme_follows_the_caller(monkeypatch):
    nodes = {"pm1": {"id": "pm1", "programme": "alpha"}}
    monkeypatch.delenv("YGG_NODE", raising=False)
    assert state.visible_programme(nodes) is None
    monkeypatch.setenv("YGG_NODE", "pm1")
    assert state.visible_programme(nodes) == "alpha"
    monkeypatch.setenv("YGG_NODE", "pm9")
    assert state.visible_programme(nodes) == ""


def test_activation_only_session_gets_the_empty_scope(monkeypatch):
    nodes = {"pm1": {"id": "pm1", "programme": "alpha"}}
    monkeypatch.delenv("YGG_NODE", raising=False)
    monkeypatch.setenv("YGG_ACTIVATION", "act-0007")
    assert state.visible_programme(nodes) == ""


def test_status_shows_only_the_callers_paused_programmes(tmp_path, monkeypatch):
    from yggdrasil import status
    monkeypatch.setattr(state, "all_nodes", lambda: {"pm1": {"id": "pm1", "programme": "alpha", "role": "pm"}})
    from yggdrasil import interlock  # paused programmes come from the active pause intents
    monkeypatch.setattr(interlock, "paused_view", lambda: {"global": False, "programmes": ["alpha", "beta"]})
    monkeypatch.setattr(status.tracker, "programmes", lambda: [])
    monkeypatch.setattr(status.tracker, "projects", lambda: [])
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "tracker"))
    monkeypatch.setenv("YGG_NODE", "pm1")
    monkeypatch.delenv("YGG_ACTIVATION", raising=False)
    assert status.build_status()["paused"]["programmes"] == ["alpha"]
    monkeypatch.delenv("YGG_NODE")
    assert status.build_status()["paused"]["programmes"] == ["alpha", "beta"]
