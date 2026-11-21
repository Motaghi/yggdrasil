"""Plain `ygg status` says when Yggdrasil is paused, with or without projects."""

import pytest

from yggdrasil import commands, status
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


@pytest.fixture
def empty(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("YGG_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "tracker"))
    monkeypatch.setenv("YGG_TRACKER", "files")
    monkeypatch.setenv("YGG_CONFIG", "none")
    monkeypatch.setenv("YGG_PLATFORM_STATE", str(tmp_path / "platform"))
    monkeypatch.chdir(tmp_path)
    clear_caches()
    return tmp_path


def _plain():
    return status.human(status.build_status()).splitlines()


def test_no_projects_and_not_paused(empty):
    assert _plain() == ["No projects yet."]


def test_a_global_pause_shows_without_projects(empty):
    commands.pause(None)
    assert _plain() == ["Paused: everything.", "No projects yet."]
    commands.resume(None)
    assert _plain() == ["No projects yet."]


def test_a_programme_pause_shows_with_its_projects(world):
    commands.pause("alpha")
    lines = _plain()
    assert lines[0] == "Paused: programme alpha."
    assert any(line.startswith("p1: ") and line.endswith(", paused") for line in lines)
    commands.pause(None)
    assert _plain()[0] == "Paused: everything, and programme alpha."
