"""`ygg node release` and `ygg node reset` refuse every managed session, like the other user-only verbs: an agent
that dropped YGG_NODE and YGG_ACTIVATION but still runs in its managed worktree is refused by its working folder."""

from test_a7 import cli
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


def _inside_agent_folder(world, monkeypatch):
    from yggdrasil import state
    worktree = world / "agent-worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / state.MANAGED_MARKER).write_text("")
    for name in ("YGG_NODE", "YGG_ACTIVATION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(worktree / "src")
    assert state.managed_session()


def test_node_release_is_refused_in_a_managed_folder_without_ygg_node(world, monkeypatch):
    from yggdrasil import state
    state.set_state("tl1", "stalled")
    _inside_agent_folder(world, monkeypatch)
    assert cli("node", "release", "tl1", "--as", "aborted", monkeypatch=monkeypatch) == \
        (3, "node release is user-only")
    assert state.runtime("tl1")["state"] == "stalled"


def test_node_reset_is_refused_in_a_managed_folder_without_ygg_node(world, monkeypatch):
    from yggdrasil import state
    state.set_state("tl1", "stalled")
    _inside_agent_folder(world, monkeypatch)
    assert cli("node", "reset", "tl1", monkeypatch=monkeypatch) == (3, "node reset is user-only")
    assert state.runtime("tl1")["state"] == "stalled"


def test_node_repair_still_refuses_the_l1_marker(world, monkeypatch):
    from yggdrasil import state
    state.set_state("tl1", "stalled")
    assert cli("node", "reset", "tl1", env={"YGG_NODE": "l1"}, monkeypatch=monkeypatch) == (3, "node reset is user-only")
    assert state.runtime("tl1")["state"] == "stalled"
