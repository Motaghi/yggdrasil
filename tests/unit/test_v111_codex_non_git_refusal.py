"""v1.1.1: a Codex builder (PM, team lead, worker or senior user) in a programme that is not a git repository
is refused before anything is reserved; a Codex reviewer there is not."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from yggdrasil import launcher, relay, state, tracker, util


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    return tmp_path


# ---------------------------------------------------------------- Codex builders only in git programmes

@pytest.mark.parametrize("role", ["pm", "tl", "worker", "su"])
def test_codex_builder_in_a_non_git_programme_is_refused_before_anything(root, monkeypatch, role):
    monkeypatch.setattr(tracker, "programme", lambda name: {"id": name, "kind": "vault-docs"})
    monkeypatch.setattr(launcher, "lane_owner_of", lambda node, nodes: None)
    node = {"id": "n1", "role": role, "programme": "docs", "project": None, "parent": "tl1"}
    with pytest.raises(util.YggError) as error:
        launcher.codex_preflight(node, {"n1": node, "tl1": {"id": "tl1", "role": "tl"}}, "codex")
    assert error.value.code == util.EXIT_REFUSED
    assert ("launch precondition: Codex builders work only in git programmes; programme docs is of kind "
            "vault-docs") in error.value.message
    assert not (state.root() / "relay").exists() and not (state.root() / "scratch").exists()


def test_codex_reviewer_in_a_non_git_programme_is_not_refused(root, monkeypatch):
    monkeypatch.setattr(tracker, "programme", lambda name: {"id": name, "kind": "vault-docs"})
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.delenv("YGG_TEST_CODEX_VERSION", raising=False)
    node = {"id": "rv1", "role": "reviewer", "programme": "docs", "project": "p1"}
    assert launcher.codex_preflight(node, {"rv1": node}, "codex")["checked"] is False
