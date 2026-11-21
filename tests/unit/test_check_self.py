import pytest

from yggdrasil import state
from yggdrasil.util import YggError


def test_parent_cannot_record_a_child_judgement(monkeypatch):
    monkeypatch.setenv("YGG_NODE", "pm1")
    with pytest.raises(YggError) as caught:
        state.check_self("pm1su1", "acceptance")
    assert (caught.value.code, caught.value.message) == (3, "acceptance for pm1su1 must be recorded by pm1su1 itself")


def test_node_itself_and_user_may_record(monkeypatch):
    monkeypatch.setenv("YGG_NODE", "pm1su1")
    state.check_self("pm1su1", "acceptance")
    assert state.caller_id() == "pm1su1"
    monkeypatch.delenv("YGG_NODE")
    state.check_self("pm1su1", "acceptance")
    assert state.caller_id() == "user"


def test_criteria_refusal_says_written(monkeypatch):
    monkeypatch.setenv("YGG_NODE", "pm1")
    with pytest.raises(YggError) as caught:
        state.check_self("pm1su1", "criteria", "written")
    assert caught.value.message == "criteria for pm1su1 must be written by pm1su1 itself"
