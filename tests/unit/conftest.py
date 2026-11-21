import pytest


@pytest.fixture(autouse=True)
def _isolated_state_root(tmp_path, monkeypatch):
    """No unit test ever writes the real state root (~/.local/state/yggdrasil): every test starts with one
    of its own, which a test's own fixture may replace."""
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "isolated-state"))
