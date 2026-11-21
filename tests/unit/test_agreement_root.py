"""Agents see the user's agreement on the Linear tracker too: the launcher pins the user's files root as
YGG_AGREEMENT_ROOT, and only an agent session (YGG_NODE set) reads it (pilot 2 run 4)."""
from __future__ import annotations

from pathlib import Path

from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


def test_the_launcher_pins_the_users_files_root_for_the_agreement(world):
    from yggdrasil import launcher, util
    pinned = launcher.pinned_env()
    assert pinned["YGG_AGREEMENT_ROOT"] == str(Path(util.files_root()).resolve())


def test_an_agent_session_reads_the_agreement_from_the_pinned_root(world, monkeypatch, tmp_path):
    from yggdrasil import permissions
    user_root = tmp_path / "user-files"
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "linear-cache"))
    monkeypatch.setenv("YGG_AGREEMENT_ROOT", str(user_root))
    monkeypatch.setenv("YGG_NODE", "pm1tl1")
    assert permissions.agreement_path() == user_root / "_ygg" / "agents-agreement.json"


def test_outside_an_agent_session_the_pinned_root_is_ignored(world, monkeypatch, tmp_path):
    from yggdrasil import permissions
    monkeypatch.delenv("YGG_NODE", raising=False)
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "files"))
    monkeypatch.setenv("YGG_AGREEMENT_ROOT", str(tmp_path / "elsewhere"))
    assert permissions.agreement_path() == tmp_path / "files" / "_ygg" / "agents-agreement.json"


def test_a_t3_session_gets_the_agreement_root_too():
    from yggdrasil import t3
    node = {"id": "pm1tl1", "programme": "alpha", "project": "p1"}
    environment = t3.session_environment(node, "act-0001", {"YGG_AGREEMENT_ROOT": "/user/files"})
    assert environment["YGG_AGREEMENT_ROOT"] == "/user/files"
