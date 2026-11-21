from yggdrasil import hooks, state


def test_managed_caller_unknown_to_the_tracker_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("YGG_NODE", "pm9tl9")
    monkeypatch.setattr(state, "all_nodes", lambda: {})
    code, message = hooks.pretool('{"tool_name": "Read", "tool_input": {"file_path": "README.md"}}')
    assert (code, message) == (2, hooks.UNAVAILABLE)
