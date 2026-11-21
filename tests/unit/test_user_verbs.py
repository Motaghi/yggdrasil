import pytest

from yggdrasil import commands, state
from yggdrasil.util import YggError


@pytest.mark.parametrize("command,action,verb", [
    ("launch", None, "launch"), ("_supervise", None, "_supervise"), ("conductor", "run", "conductor run"),
    ("heimdall", "admit", "heimdall admit"), ("event", "boat", "event boat"), ("rehydrate", None, "rehydrate"),
])
def test_managed_sessions_cannot_run_user_verbs(monkeypatch, command, action, verb):
    monkeypatch.setenv("YGG_NODE", "pm1")
    with pytest.raises(YggError) as caught:
        state.check_user_verb(command, action)
    assert caught.value.code == 3
    assert caught.value.message == "REFUSED: user-only: '" + verb + "' is a user verb and cannot run in a managed session"


def test_user_shell_and_other_verbs_are_unaffected(monkeypatch):
    monkeypatch.delenv("YGG_NODE", raising=False)
    state.check_user_verb("launch", None)
    monkeypatch.setenv("YGG_NODE", "pm1")
    state.check_user_verb("status", None)


def test_hardstop_check_names_only_the_caller(monkeypatch):
    monkeypatch.setenv("YGG_NODE", "pm1")
    with pytest.raises(YggError) as caught:
        commands.hardstop_check("pm1tl1", "push", "main")
    assert (caught.value.code, caught.value.message) == (3, "hardstop: pm1 may only ask for itself")


def test_activation_only_session_cannot_schedule(monkeypatch):
    monkeypatch.delenv("YGG_NODE", raising=False)
    monkeypatch.setenv("YGG_ACTIVATION", "act-0007")
    with pytest.raises(YggError) as caught:
        state.check_user_verb("conductor", "tick")
    assert caught.value.message == "REFUSED: user-only: 'conductor tick' is a user verb and cannot run in a managed session"


def test_unmarked_user_shell_may_schedule(monkeypatch):
    monkeypatch.delenv("YGG_NODE", raising=False)
    monkeypatch.delenv("YGG_ACTIVATION", raising=False)
    state.check_user_verb("conductor", "tick")


def test_activation_only_session_cannot_use_older_user_verbs(monkeypatch):
    monkeypatch.delenv("YGG_NODE", raising=False)
    monkeypatch.setenv("YGG_ACTIVATION", "act-0007")
    with pytest.raises(YggError) as caught:
        state.check_user_only("pid approve")
    assert (caught.value.code, caught.value.message) == (
        3, "REFUSED: user-only: 'pid approve' is a user verb and cannot run in a managed session")
    monkeypatch.delenv("YGG_ACTIVATION")
    state.check_user_only("pid approve")
