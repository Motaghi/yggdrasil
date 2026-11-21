"""Port follow-ups: settling an unconfirmed T3 project removal, and closing headless-only merged lanes."""

import os
import subprocess
import sys
import threading

import pytest
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)
from test_t3_close import _alerts, _merged, _removes, lane  # noqa: F401  (shared fixture and helpers)

from yggdrasil import util


def _stuck(lane):
    """A removal of proj-1 was journalled, the closer stopped, and T3 can no longer tell it gone."""
    from yggdrasil import journal, t3
    _merged()
    journal.add("beta", "t3-close", close_id="tc-00000000000e", project="q1", worktree=str(lane["worktree"]),
                removing="proj-1", threads_seen=0)
    lane["remote"].discard("proj-1")
    t3.finish_closes()
    assert lane["worktree"].exists()


def _blocked(node="tl2"):
    from yggdrasil import state, t3
    nodes = state.all_nodes()
    try:
        t3.refuse_if_closing(nodes[node], nodes)
    except util.YggError as error:
        return error.message
    return None


# 1: `ygg t3 settle <id> --as removed|kept`

def test_the_refusal_names_the_way_out(lane):
    _stuck(lane)
    assert "ygg t3 settle proj-1 --as removed|kept" in _blocked()


def test_settled_as_removed_lifts_the_block_and_the_close_finishes(lane):
    from yggdrasil import t3
    _stuck(lane)
    assert t3.settle("proj-1", "removed") == "settled the removal of T3 project proj-1 as removed\n"
    assert _blocked() is None
    t3.finish_closes()
    assert not lane["worktree"].exists()
    assert "proj-1" in t3._removed_projects()  # never handed out again


def test_settled_as_kept_lifts_the_block_and_keeps_project_and_worktree(lane):
    from yggdrasil import t3
    _stuck(lane)
    removes = len(_removes(lane))
    assert t3.settle("proj-1", "kept") == "settled the removal of T3 project proj-1 as kept\n"
    assert _blocked() is None
    t3.finish_closes()
    assert lane["worktree"].exists() and len(_removes(lane)) == removes
    assert "proj-1" in t3._removed_projects()  # Yggdrasil no longer manages it


def test_settling_is_idempotent_and_never_changes_its_answer(lane):
    from yggdrasil import journal, t3
    _stuck(lane)
    t3.settle("proj-1", "removed")
    before = journal.revision("beta")
    assert t3.settle("proj-1", "removed") == "the removal of T3 project proj-1 is already settled as removed\n"
    assert journal.revision("beta") == before
    with pytest.raises(util.YggError) as error:
        t3.settle("proj-1", "kept")
    assert error.value.code == util.EXIT_REFUSED and "already settled as removed" in error.value.message


def test_settling_is_journalled(lane):
    from yggdrasil import journal, t3
    _stuck(lane)
    t3.settle("proj-1", "kept")
    trusted, _ = journal.trusted_entries("beta")
    entry = [e for e in trusted if e.get("kind") == "t3-close"][-1]
    assert entry["kept"] == "proj-1" and entry["worktree"] == str(lane["worktree"]) and entry["project"] == "q1"


def test_only_a_started_removal_can_be_settled(lane):
    from yggdrasil import t3
    with pytest.raises(util.YggError) as error:
        t3.settle("proj-1", "removed")
    assert error.value.code == util.EXIT_REFUSED and "no unsettled removal" in error.value.message
    with pytest.raises(util.YggError) as error:
        t3.settle("proj-1", "gone")
    assert error.value.code == util.EXIT_USAGE


@pytest.mark.parametrize("variable", ["YGG_NODE", "YGG_ACTIVATION"])
def test_settling_is_user_only(lane, monkeypatch, variable):
    from yggdrasil import journal, t3
    _stuck(lane)
    before = journal.revision("beta")
    monkeypatch.setenv(variable, "tl2" if variable == "YGG_NODE" else "act-0009")
    with pytest.raises(util.YggError) as error:
        t3.settle("proj-1", "removed")
    assert error.value.code == util.EXIT_REFUSED and "user-only" in error.value.message
    assert journal.revision("beta") == before


def test_settling_is_refused_inside_a_managed_worktree_without_identity(lane, monkeypatch):
    from yggdrasil import journal, t3
    _stuck(lane)
    before = journal.revision("beta")
    monkeypatch.delenv("YGG_NODE", raising=False)
    monkeypatch.delenv("YGG_ACTIVATION", raising=False)
    monkeypatch.chdir(lane["worktree"])  # carries the `.ygg-managed` marker
    with pytest.raises(util.YggError) as error:
        t3.settle("proj-1", "removed")
    assert error.value.code == util.EXIT_REFUSED and "user-only" in error.value.message
    env = {k: v for k, v in os.environ.items() if k not in ("YGG_NODE", "YGG_ACTIVATION")}
    result = subprocess.run([sys.executable, "-m", "yggdrasil", "t3", "settle", "proj-1", "--as", "removed"],
                            capture_output=True, text=True, env=env, cwd=lane["worktree"])
    assert result.returncode == util.EXIT_REFUSED and "user-only: 't3 settle'" in result.stderr
    assert journal.revision("beta") == before


def test_settling_waits_for_no_running_close(lane):
    from yggdrasil import t3
    _stuck(lane)
    held, release = threading.Event(), threading.Event()

    def closer():
        with util.locked("t3-close"):
            held.set()
            release.wait(10)

    worker = threading.Thread(target=closer)
    worker.start()
    held.wait(10)
    try:
        with pytest.raises(util.YggError) as error:
            t3.settle("proj-1", "removed")
        assert "a close is running" in error.value.message
    finally:
        release.set()
        worker.join(10)
    assert _blocked() is not None  # nothing was settled


def test_the_cli_settles(lane):
    _stuck(lane)
    result = subprocess.run([sys.executable, "-m", "yggdrasil", "t3", "settle", "proj-1", "--as", "removed"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "settled the removal of T3 project proj-1 as removed\n"


# 2: a merged lane's worktree goes whether or not it ever had a T3 project

def _headless(lane):
    """The lane worktree as a headless launch leaves it: no T3 project, marker or guard settings."""
    from yggdrasil import state
    util.write_json(state.root() / "t3-projects.json", {})
    (lane["worktree"] / ".ygg-managed").unlink()
    (lane["worktree"] / ".claude" / "settings.json").unlink()
    (lane["worktree"] / ".claude").rmdir()


def test_a_t3_marked_worktree_without_a_known_project_is_held_not_removed(lane):
    from yggdrasil import state, t3
    util.write_json(state.root() / "t3-projects.json", {})  # registered but lost, the marker still in place
    _merged()
    t3.finish_closes()
    t3.finish_closes()
    assert lane["worktree"].exists() and _removes(lane) == []
    assert len(_alerts("ygg-t3-cleanup")) == 1


def test_a_merged_headless_lane_loses_its_worktree(lane, monkeypatch):
    from yggdrasil import journal, t3
    _headless(lane)
    seen = []
    real_add = journal.add

    def watching(programme, kind, **fields):
        seen.append((kind, dict(fields), lane["worktree"].exists()))
        return real_add(programme, kind, **fields)

    monkeypatch.setattr(journal, "add", watching)
    _merged()
    t3.close_lane("q1", "tl2")
    assert not lane["worktree"].exists() and _removes(lane) == []
    listed = subprocess.run(["git", "-C", str(lane["home"]), "worktree", "list"], capture_output=True, text=True)
    assert str(lane["worktree"]) not in listed.stdout
    closes = [(f.get("phase"), present) for kind, f, present in seen if kind == "worktree-close"]
    assert closes == [("removing", True), ("removed", False)]  # journalled before anything local changes
    t3.finish_closes()
    assert [k for k, _, _ in seen].count("worktree-close") == 2  # nothing more is owed


def test_an_unmerged_headless_lane_keeps_its_worktree(lane):
    from yggdrasil import journal, t3
    _headless(lane)
    before = journal.revision("beta")
    t3.finish_closes()
    assert lane["worktree"].exists() and journal.revision("beta") == before


def test_a_headless_lane_marked_merged_with_unmerged_commits_journals_no_removal(lane):
    from test_t3_close import _git
    from yggdrasil import journal, t3
    _headless(lane)
    (lane["worktree"] / "unmerged.txt").write_text("work not integrated\n")
    _git(lane["worktree"], "add", "unmerged.txt")
    _git(lane["worktree"], "commit", "-qm", "unmerged work")
    _merged()  # the package says merged, but the lane's tip is in neither the integration branch nor main
    t3.finish_closes()
    t3.finish_closes()
    assert (lane["worktree"] / "unmerged.txt").exists()
    phases = [e for e in journal.trusted_entries("beta")[0] if e.get("kind") == "worktree-close"]
    assert phases == []  # the removal is journalled only once the lane is confirmed merged
    assert len(_alerts("ygg-t3-cleanup")) == 1


def test_a_dirty_headless_worktree_is_kept_with_one_alert_and_nothing_journalled(lane):
    from yggdrasil import journal, t3
    _headless(lane)
    _merged()
    before = journal.revision("beta")
    (lane["worktree"] / "work.txt").write_text("unsaved\n")
    t3.finish_closes()
    t3.finish_closes()
    assert (lane["worktree"] / "work.txt").exists()
    assert len(_alerts("ygg-worktree-dirty")) == 1
    assert journal.revision("beta") == before


def test_no_reservation_starts_in_a_headless_worktree_being_removed(lane, monkeypatch):
    from yggdrasil import t3
    _headless(lane)
    _merged()
    refused = []
    real = util.trusted_git

    def git_meanwhile(*args, **kw):
        if "worktree" in args and "remove" in args:
            refused.append(_blocked("w2"))
        return real(*args, **kw)

    monkeypatch.setattr(util, "trusted_git", git_meanwhile)
    t3.finish_closes()
    assert refused and refused[0] and "is being closed" in refused[0]
    assert not lane["worktree"].exists() and _blocked("w2") is None


def test_a_headless_close_waits_for_unsettled_activations(lane):
    from test_t3_close import _commit
    from yggdrasil import t3
    _headless(lane)
    _merged()
    _commit("w2", "act-0002", "reserved", launcher_pid=1, launcher_started_at="x", work={})
    t3.finish_closes()
    assert lane["worktree"].exists()
    _commit("w2", "act-0002", "aborted", abort_reason="test")
    t3.finish_closes()
    assert not lane["worktree"].exists()


def test_a_removal_interrupted_after_its_record_is_finished_next_time(lane, monkeypatch):
    from yggdrasil import journal, t3
    _headless(lane)
    _merged()
    real = util.trusted_git
    calls = {"n": 0}

    def crash_once(*args, **kw):
        if "worktree" in args and "remove" in args and not calls["n"]:
            calls["n"] += 1
            raise OSError("interrupted")
        return real(*args, **kw)

    monkeypatch.setattr(util, "trusted_git", crash_once)
    t3.finish_closes()
    assert lane["worktree"].exists()
    t3.finish_closes()
    assert not lane["worktree"].exists()
    phases = [e.get("phase") for e in journal.trusted_entries("beta")[0] if e.get("kind") == "worktree-close"]
    assert phases == ["removing", "removed"]  # the started removal is not journalled twice


# Follow-ups review round 1: a crash after the git removal, and a repository's own settings file.
def _close_phases():
    from yggdrasil import journal
    return [e.get("phase") for e in journal.trusted_entries("beta")[0]
            if e.get("kind") == "worktree-close"]


def test_a_crash_right_after_the_git_removal_is_completed_exactly_once(lane):
    from yggdrasil import journal, state, t3
    _headless(lane)
    util.write_json(state.root() / "t3-settings.json", {})
    _merged()
    code = """
import os
from yggdrasil import t3, util
original = util.trusted_git
def interrupted(*args, **kwargs):
    result = original(*args, **kwargs)
    if "worktree" in args and "remove" in args and result.returncode == 0:
        os._exit(97)
    return result
util.trusted_git = interrupted
t3.finish_closes()
"""
    stopped = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert stopped.returncode == 97, stopped.stderr
    assert not lane["worktree"].exists()
    assert _close_phases() == ["removing"]
    for _ in range(3):
        t3.finish_closes()
    assert _close_phases() == ["removing", "removed"], {
        "phases": _close_phases(), "owed": t3.owed_closes(),
        "still_pending": t3._worktree_removal_pending("beta", os.path.realpath(lane["worktree"]))}


def test_a_tracked_settings_file_is_left_for_git_to_remove(lane):
    from test_t3_close import _git
    from yggdrasil import journal, state, t3
    _headless(lane)
    util.write_json(state.root() / "t3-settings.json", {})
    settings = lane["worktree"] / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"repo_owned": true}\n')
    _git(lane["worktree"], "add", "-f", ".claude/settings.json")
    _git(lane["worktree"], "commit", "-qm", "repository settings")
    branch = subprocess.check_output(["git", "-C", str(lane["worktree"]), "branch", "--show-current"], text=True).strip()
    _git(lane["home"], "merge", "--ff-only", branch)
    assert t3._kept(str(lane["worktree"]), os.path.realpath(lane["worktree"]), str(lane["home"])) is None
    _merged()
    t3.finish_closes()
    t3.finish_closes()
    assert not lane["worktree"].exists(), {
        "settings_exists": settings.exists(),
        "status": subprocess.check_output(["git", "-C", str(lane["worktree"]), "status", "--porcelain"], text=True),
        "phases": _close_phases()}


def test_a_merged_lane_keeps_its_worktree_while_its_project_can_still_give_it_work(lane):
    from yggdrasil import t3, tracker
    _headless(lane)
    project = tracker.project("q1")
    project["state"] = "executing"
    tracker.save_project(project)
    _merged(finished=False)  # every package merged, but the project is still executing
    assert tracker.project("q1")["state"] == "executing"
    t3.close_lane("q1", "tl2")
    t3.finish_closes()
    assert lane["worktree"].exists()
    project = tracker.project("q1")
    project["state"] = "closed"
    tracker.save_project(project)
    t3.finish_closes()
    assert not lane["worktree"].exists()
