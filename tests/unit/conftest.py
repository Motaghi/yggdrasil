import pytest


@pytest.fixture(autouse=True)
def _isolated_state_root(tmp_path, monkeypatch):
    """No unit test ever writes the real state root (~/.local/state/yggdrasil) or the default files root
    (./ygg-tracker, which resolves against the checkout when pytest runs from the repository root): every test
    starts with its own of each, which a test's own fixture may replace."""
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "isolated-state"))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "isolated-tracker"))
