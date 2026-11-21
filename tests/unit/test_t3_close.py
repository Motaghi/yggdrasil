"""Closing a lane or a project removes its T3 projects, then its worktrees (A5.1)."""

import json
import os
import subprocess

import pytest
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)

from yggdrasil import util


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def lane(world, monkeypatch):
    """A git-repo programme with one lane: tl2 (and its worker w2) work in the lane worktree, registered as
    T3 project proj-1 with the guard settings in place, and lane package WP-1 (open)."""
    from yggdrasil import commands, launcher, state, t3, tracker
    home = world / "beta-home"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    _git(home, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "root")
    commands.programme_create("beta", "beta", str(home), "git-repo")
    commands.project_create("q1", "beta", "pm2")
    commands.node_add("tl2", "tl", "pm2")
    commands.node_add("w2", "worker", "tl2")
    tracker.new_issue("q1", "WP-1", "work-package", [], "pm2", lane="tl2", state="accepted").save()
    nodes = state.all_nodes()
    worktree = launcher.working_directory(nodes["tl2"], nodes)
    t3.prepare_worktree(worktree, nodes["tl2"], "act-0001", {}, home=str(home))
    monkeypatch.setattr(t3, "host_usable", lambda: True)
    calls = []
    remote = {"proj-1"}

    def fake_run(*args, **kw):
        calls.append({"argv": list(args), "worktree_present": worktree.exists()})
        if args[:2] == ("project", "add"):
            remote.add("proj-2")
            return subprocess.CompletedProcess(args, 0, "Added project proj-2 (q1/tl2) at " + str(worktree) + ".\n", "")
        if args[:2] == ("project", "remove"):
            if args[-1] not in remote:
                return subprocess.CompletedProcess(args, 1, "", "Project not found.")
            remote.discard(args[-1])
            return subprocess.CompletedProcess(args, 0, "Removed project " + args[-1] + " (q1/tl2).\n", "")
        if args[:2] == ("thread", "list"):
            return subprocess.CompletedProcess(args, 0, "[]", "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected")

    monkeypatch.setattr(t3, "run", fake_run)
    known = {os.path.realpath(worktree): {"id": "proj-1", "title": "q1/tl2", "path": str(worktree)}}
    util.write_json(state.root() / "t3-projects.json", known)
    return {"home": home, "worktree": worktree, "calls": calls, "run": fake_run, "remote": remote}


def _merged(*, finished=True):
    """WP-1 merged; by default its project also gives the lane no more work (ready to merge), which is when a
    merged lane loses its worktree."""
    from yggdrasil import tracker
    issue = tracker.read_issue("q1", "WP-1")
    issue.header["state"] = "merged"
    issue.save()
    if finished:
        project = tracker.project("q1")
        project["state"] = "ready-to-merge"
        tracker.save_project(project)


def _commit(node, activation, phase, **fields):
    from yggdrasil import lifecycle
    assert lifecycle.commit(node, activation, phase, **fields)


def _spawned(node, activation, worktree, project="proj-1"):
    _commit(node, activation, "reserved", launcher_pid=1, launcher_started_at="x", work={})
    _commit(node, activation, "spawned", launcher="t3", t3_kind="start", t3_title=node + " " + activation,
            t3_worktree=str(worktree), t3_project_id=project, supervisor_pid=1, supervisor_started_at="x")
    _commit(node, activation, "exited", exit_code=0)


def _removes(lane):
    return [c for c in lane["calls"] if c["argv"][:2] == ["project", "remove"]]


def _alerts(kind):
    from yggdrasil import state
    return [a for a in state.alerts() if a["kind"] == kind]


def _local(name):
    from yggdrasil import state
    return util.read_json(state.root() / name, {}) or {}


def test_a_merged_lane_loses_its_t3_project_and_then_its_worktree(lane):
    from yggdrasil import t3
    worktree = lane["worktree"]
    _merged()
    t3.close_lane("q1", "tl2")
    removes = _removes(lane)
    assert [c["argv"] for c in removes] == [["project", "remove", "--force", "proj-1"]]
    assert removes[0]["worktree_present"] is True  # the project goes first, then the worktree
    assert not worktree.exists()
    listed = subprocess.run(["git", "-C", str(lane["home"]), "worktree", "list"], capture_output=True, text=True,
                            check=False)
    assert str(worktree) not in listed.stdout
    real = os.path.realpath(worktree)
    assert real not in _local("t3-projects.json") and real not in _local("t3-settings.json")
    assert "proj-1" in t3._removed_projects()
    t3.finish_closes()
    assert len(_removes(lane)) == 1  # nothing is owed any more


def test_a_lane_with_an_open_package_or_no_t3_project_is_left_alone(lane):
    from yggdrasil import journal, state, t3
    before = journal.revision("beta")
    t3.close_lane("q1", "tl2")  # WP-1 is accepted, not merged
    assert _removes(lane) == [] and lane["worktree"].exists()
    _merged()
    util.write_json(state.root() / "t3-projects.json", {})
    t3.finish_closes()  # merged and T3-marked, but no T3 project on record: held, never removed first
    assert _removes(lane) == [] and lane["worktree"].exists()
    assert journal.revision("beta") == before


def test_the_close_is_held_while_an_activation_in_the_worktree_is_unsettled(lane):
    from yggdrasil import t3
    _merged()
    _commit("w2", "act-0002", "reserved", launcher_pid=1, launcher_started_at="x", work={})
    t3.close_lane("q1", "tl2")
    t3.finish_closes()
    assert _removes(lane) == [] and lane["worktree"].exists()
    assert len(_alerts("ygg-t3-cleanup")) == 1  # once, however often the hold is seen
    _commit("w2", "act-0002", "aborted", abort_reason="test")
    t3.finish_closes()
    assert len(_removes(lane)) == 1 and not lane["worktree"].exists()


def test_no_reservation_starts_in_a_worktree_while_it_is_being_closed(lane, monkeypatch):
    from yggdrasil import state, t3
    _merged()
    refused = []

    def reserving_meanwhile():
        nodes = state.all_nodes()
        try:
            t3.refuse_if_closing(nodes["w2"], nodes)
        except util.YggError as error:
            refused.append(error.message)
        return True

    monkeypatch.setattr(t3, "host_usable", reserving_meanwhile)
    t3.finish_closes()
    assert refused and "is being closed" in refused[0]
    assert not lane["worktree"].exists()
    nodes = state.all_nodes()
    t3.refuse_if_closing(nodes["w2"], nodes)  # the claim is released afterwards


def test_a_failed_removal_keeps_the_worktree_and_is_retried(lane, monkeypatch):
    from yggdrasil import t3
    _merged()
    failing = [True]

    def refusing(*args, **kw):
        if args[:2] == ("project", "remove") and failing:
            lane["calls"].append({"argv": list(args), "worktree_present": lane["worktree"].exists()})
            return subprocess.CompletedProcess(args, 1, "", "server unavailable")
        return lane["run"](*args, **kw)

    monkeypatch.setattr(t3, "run", refusing)
    t3.finish_closes()
    t3.finish_closes()
    assert len(_removes(lane)) == 2 and lane["worktree"].exists()
    assert len(_alerts("ygg-t3-cleanup")) == 1
    failing.clear()
    t3.finish_closes()
    assert not lane["worktree"].exists()


def test_a_close_needs_a_usable_t3(lane, monkeypatch):
    from yggdrasil import t3
    _merged()
    monkeypatch.setattr(t3, "host_usable", lambda: False)
    t3.finish_closes()
    assert _removes(lane) == [] and lane["worktree"].exists()
    monkeypatch.setattr(t3, "host_usable", lambda: True)
    t3.finish_closes()
    assert len(_removes(lane)) == 1 and not lane["worktree"].exists()


def test_a_dirty_worktree_is_kept_with_its_project(lane):
    from yggdrasil import t3
    _merged()
    (lane["worktree"] / "work.txt").write_text("unsaved\n")
    t3.finish_closes()
    t3.finish_closes()
    assert _removes(lane) == []  # checked before anything is removed in T3
    assert (lane["worktree"] / "work.txt").exists()
    assert len(_alerts("ygg-worktree-dirty")) == 1


@pytest.mark.parametrize("restored", [False, True])
def test_a_project_id_known_from_the_journal_is_confirmed_before_removal(lane, monkeypatch, restored):
    from yggdrasil import state, t3
    worktree = lane["worktree"]
    _spawned("tl2", "act-0002", worktree)
    util.write_json(state.root() / "t3-projects.json", {})  # a wiped state root
    if restored:
        assert t3.ensure_project(worktree, "q1/tl2") == "proj-1"  # restored into the local map, unconfirmed
    _merged()
    t3.finish_closes()
    assert _removes(lane) == [] and worktree.exists()  # no thread confirms proj-1 at this path
    listing = json.dumps([{"threadId": "th-4", "projectId": "proj-1", "worktreePath": str(worktree),
                           "title": "tl2 act-0002"}])

    def confirming(*args, **kw):
        if args[:2] == ("thread", "list"):
            return subprocess.CompletedProcess(args, 0, listing, "")
        return lane["run"](*args, **kw)

    monkeypatch.setattr(t3, "run", confirming)
    t3.finish_closes()
    assert [c["argv"] for c in _removes(lane)] == [["project", "remove", "--force", "proj-1"]]
    assert not worktree.exists()


def test_a_thread_of_the_project_at_another_path_confirms_nothing(lane, monkeypatch):
    from yggdrasil import state, t3
    worktree = lane["worktree"]
    _spawned("tl2", "act-0002", worktree)
    util.write_json(state.root() / "t3-projects.json", {})
    _merged()
    for path, removed in (("/elsewhere", False), (None, True)):
        listing = json.dumps([{"threadId": "th-4", "projectId": "proj-1", "worktreePath": path}])
        monkeypatch.setattr(t3, "run", lambda *args, _l=listing, **kw: subprocess.CompletedProcess(args, 0, _l, "")
                            if args[:2] == ("thread", "list") else lane["run"](*args, **kw))
        t3.finish_closes()
        assert bool(_removes(lane)) is removed


def test_a_crash_after_the_remote_removal_is_finished_and_the_id_never_reused(lane, monkeypatch):
    from yggdrasil import journal, launcher, state, t3
    worktree = lane["worktree"]
    _spawned("tl2", "act-0002", worktree)
    _merged()
    # The earlier closer journalled its intent, T3 removed the project, then the closer died.
    journal.add("beta", "t3-close", close_id="tc-00000000000a", project="q1", worktree=str(worktree),
                removing="proj-1", threads_seen=1)
    lane["remote"].discard("proj-1")
    t3.finish_closes()
    assert "proj-1" in t3._removed_projects() and not worktree.exists()
    nodes = state.all_nodes()
    again = launcher.working_directory(nodes["tl2"], nodes)
    t3.prepare_worktree(again, nodes["tl2"], "act-0004", {}, home=str(lane["home"]))  # no stale baseline
    assert t3.ensure_project(again, "q1/tl2") == "proj-2"


def test_a_removed_id_left_in_the_local_map_is_never_reused(lane):
    from yggdrasil import journal, t3
    journal.add("beta", "t3-close", close_id="tc-00000000000b", project="q1", worktree=str(lane["worktree"]),
                removed="proj-1")
    assert t3.ensure_project(lane["worktree"], "q1/tl2") == "proj-2"


def test_a_wiped_state_root_without_a_journalled_project_removes_nothing(lane):
    from yggdrasil import state, t3
    util.write_json(state.root() / "t3-projects.json", {})  # registered, never spawned, then wiped
    _merged()
    t3.finish_closes()
    assert _removes(lane) == [] and lane["worktree"].exists()


def test_a_baseline_is_dropped_when_the_worktree_is_already_gone(lane):
    from yggdrasil import journal, t3
    worktree = lane["worktree"]
    _merged()
    journal.add("beta", "t3-close", close_id="tc-00000000000c", project="q1", worktree=str(worktree),
                removed="proj-1")
    _git(lane["home"], "worktree", "remove", "--force", str(worktree))  # removed, then the closer died
    assert os.path.realpath(worktree) in _local("t3-settings.json")
    t3.finish_closes()
    assert os.path.realpath(worktree) not in _local("t3-settings.json")


def test_a_closed_project_is_closed_even_if_the_close_call_never_ran(lane):
    from yggdrasil import t3, tracker
    project = tracker.project("q1")
    project["state"] = "closed"
    tracker.save_project(project)  # a crash right after the save
    t3.finish_closes()
    assert [c["argv"] for c in _removes(lane)] == [["project", "remove", "--force", "proj-1"]]
    assert not lane["worktree"].exists()


def test_closing_a_project_removes_the_t3_projects_of_its_worktrees(lane):
    from yggdrasil import commands
    commands.project_close("q1", premature=True, note="stopped")
    assert [c["argv"] for c in _removes(lane)] == [["project", "remove", "--force", "proj-1"]]
    assert not lane["worktree"].exists()


def test_a_path_outside_the_project_worktrees_is_never_a_close_target(lane):
    from yggdrasil import state, t3
    known = _local("t3-projects.json")
    known[os.path.realpath(lane["home"])] = {"id": "proj-9", "title": "x", "path": str(lane["home"])}
    util.write_json(state.root() / "t3-projects.json", known)
    _merged()
    t3.finish_closes()
    assert [c["argv"][-1] for c in _removes(lane)] == ["proj-1"]
    assert lane["home"].exists()


def test_each_conductor_tick_finishes_owed_closes(lane, monkeypatch):
    from yggdrasil import conductor, t3
    seen = []
    monkeypatch.setattr(t3, "finish_closes", lambda: seen.append(True))
    conductor.tick()
    assert seen == [True]


def test_a_busy_close_lock_is_skipped_not_waited_on(lane):
    import threading

    from yggdrasil import t3
    _merged()
    with util.locked("t3-close"):
        worker = threading.Thread(target=t3.finish_closes)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
    assert _removes(lane) == [] and lane["worktree"].exists()


def test_a_dirty_worktree_stays_in_view_and_closes_once_clean(lane):
    from yggdrasil import t3
    _merged()
    (lane["worktree"] / "work.txt").write_text("unsaved\n")
    t3.finish_closes()  # kept dirty, with its T3 project
    assert _removes(lane) == [] and lane["worktree"].exists()
    (lane["worktree"] / "work.txt").unlink()
    t3.finish_closes()
    assert not lane["worktree"].exists() and len(_removes(lane)) == 1


def test_a_registration_is_journalled_so_a_wipe_never_removes_the_worktree_first(lane):
    from yggdrasil import journal, state, t3
    worktree = lane["worktree"]
    journal.add("beta", "t3-close", close_id="tc-00000000000d", project="q1", worktree=str(worktree),
                removed="proj-1")  # an earlier close of this lane
    util.write_json(state.root() / "t3-projects.json", {})
    assert t3.ensure_project(worktree, "q1/tl2", state.all_nodes()["tl2"]) == "proj-2"  # the lane came back
    util.write_json(state.root() / "t3-projects.json", {})  # then a wipe, before any spawn
    _merged()
    t3.finish_closes()
    assert _removes(lane) == [] and worktree.exists()  # proj-2 is known but unconfirmed: held, nothing removed
    assert any("proj-2" in a["message"] for a in _alerts("ygg-t3-cleanup"))


def test_the_close_claim_lives_beside_the_locks(lane, monkeypatch):
    from yggdrasil import safety, state, t3
    _merged()
    where = []
    monkeypatch.setattr(t3, "host_usable", lambda: where.append(t3._claims_path()) or True)
    t3.finish_closes()
    assert where and where[0].parent == safety.root() and state.root() not in where[0].parents


def test_each_hold_reason_alerts_once_whatever_the_order(lane, monkeypatch):
    from yggdrasil import t3
    _merged()
    usable = [False]
    monkeypatch.setattr(t3, "host_usable", lambda: usable[0])
    for value in (False, True, False, True):
        usable[0] = value
        if value:
            (lane["worktree"] / "work.txt").write_text("unsaved\n")
        t3.finish_closes()
    assert len(_alerts("ygg-worktree-dirty")) == 1 and len(_alerts("ygg-t3-cleanup")) == 1


def test_a_project_registered_before_the_claim_is_removed_before_the_worktree(lane, monkeypatch):
    from yggdrasil import journal, state, t3
    worktree = lane["worktree"]
    _merged()
    journal.add("beta", "t3-close", close_id="tc-00000000000f", project="q1", worktree=str(worktree),
                removed="proj-1")  # an earlier close removed proj-1 and kept the worktree
    util.write_json(state.root() / "t3-projects.json", {})
    lane["remote"].discard("proj-1")
    original = t3._unsettled
    registered = []

    def registering_meanwhile(*args):
        if not registered:  # after the first read: a launch registers a new project, then stops before it spawns
            registered.append(t3.ensure_project(worktree, "q1/tl2", state.all_nodes()["tl2"]))
        return original(*args)

    monkeypatch.setattr(t3, "_unsettled", registering_meanwhile)
    t3.finish_closes()
    removes = _removes(lane)
    assert registered == ["proj-2"]
    assert [c["argv"][-1] for c in removes] == ["proj-2"] and removes[0]["worktree_present"] is True
    assert not worktree.exists()


def test_an_unsettled_removal_holds_launches_and_never_hands_out_its_id(lane):
    from yggdrasil import journal, state, t3
    worktree = lane["worktree"]
    _merged()
    journal.add("beta", "t3-close", close_id="tc-00000000000e", project="q1", worktree=str(worktree),
                removing="proj-1", threads_seen=0)  # a threadless project; the closer stopped after the call
    lane["remote"].discard("proj-1")
    t3.finish_closes()
    assert worktree.exists()  # it cannot be told gone: held
    nodes = state.all_nodes()
    with pytest.raises(util.YggError) as error:
        t3.refuse_if_closing(nodes["tl2"], nodes)
    assert "not settled" in error.value.message
    with pytest.raises(util.YggError):
        t3.ensure_project(worktree, "q1/tl2")


def test_a_hold_alerts_before_it_is_marked(lane, monkeypatch):
    from yggdrasil import state, t3
    _merged()
    monkeypatch.setattr(t3, "host_usable", lambda: False)
    marked_at_alert = []
    original = state.alert
    monkeypatch.setattr(state, "alert", lambda *a, **k: marked_at_alert.append(bool(_local("t3-close-holds.json")))
                        or original(*a, **k))
    t3.finish_closes()
    assert marked_at_alert == [False]


def test_a_removal_intent_alone_keeps_its_id_after_a_wipe(lane):
    from yggdrasil import journal, state, t3
    worktree = lane["worktree"]
    _merged()
    journal.add("beta", "t3-close", close_id="tc-000000000010", project="q1", worktree=str(worktree),
                removing="proj-1", threads_seen=0)  # the closer stopped before the call
    util.write_json(state.root() / "t3-projects.json", {})  # and the state root was wiped
    t3.finish_closes()
    assert [c["argv"][-1] for c in _removes(lane)] == ["proj-1"]
    assert removes_first(lane) and not worktree.exists()


def removes_first(lane):
    return _removes(lane)[0]["worktree_present"] is True


def test_a_worktree_recreated_after_its_removal_is_handled_under_the_lock(lane, monkeypatch):
    from yggdrasil import journal, t3
    worktree = lane["worktree"]
    _merged()
    journal.add("beta", "t3-close", close_id="tc-000000000011", project="q1", worktree=str(worktree),
                removed="proj-1")
    _git(lane["home"], "worktree", "remove", "--force", str(worktree))  # removed, its baseline left behind
    original = t3._unsettled

    def relaunched_meanwhile(*args):  # a launch recreates the worktree and is still running
        if not worktree.exists():
            _git(lane["home"], "worktree", "add", "-q", "--detach", str(worktree), "main")
            _commit("tl2", "act-0005", "reserved", launcher_pid=1, launcher_started_at="x", work={})
        return original(*args)

    monkeypatch.setattr(t3, "_unsettled", relaunched_meanwhile)
    t3.finish_closes()
    assert worktree.exists()  # the running activation holds the close


def test_a_stale_baseline_goes_even_when_the_lane_has_reopened(lane):
    from yggdrasil import journal, t3, tracker
    worktree = lane["worktree"]
    journal.add("beta", "t3-close", close_id="tc-000000000012", project="q1", worktree=str(worktree),
                removed="proj-1")
    _git(lane["home"], "worktree", "remove", "--force", str(worktree))
    tracker.new_issue("q1", "WP-2", "work-package", [], "pm2", lane="tl2", state="authorised").save()
    t3.finish_closes()
    assert os.path.realpath(worktree) not in _local("t3-settings.json")


def test_a_claim_left_by_a_stopped_closer_is_pruned(lane):
    from yggdrasil import t3
    util.write_json(t3._claims_path(), {"/x": {"pid": 999999, "started_at": "gone"}})
    t3.finish_closes()
    assert util.read_json(t3._claims_path(), {}) == {}


def _commit_in(worktree, name):
    (worktree / name).write_text("work\n")
    _git(worktree, "add", name)
    _git(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "lane work " + name)
    return subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.mark.parametrize("close", ["lane", "project"])
def test_a_clean_worktree_with_unmerged_commits_is_kept(lane, close):
    from yggdrasil import commands, t3
    worktree = lane["worktree"]
    tip = _commit_in(worktree, "unmerged.txt")
    if close == "lane":
        _merged()
        t3.finish_closes()
    else:
        commands.project_close("q1", premature=True, note="stopped")
    assert worktree.exists(), "removed a clean worktree whose commit " + tip + " is merged nowhere"
    assert any("not merged" in a["message"] for a in _alerts("ygg-t3-cleanup"))


def test_a_worktree_whose_commits_reached_the_integration_branch_is_removed(lane):
    from yggdrasil import t3
    worktree = lane["worktree"]
    tip = _commit_in(worktree, "merged.txt")
    _git(lane["home"], "branch", "-f", "ygg/q1/_integration", tip)
    _merged()
    t3.finish_closes()
    assert not worktree.exists()


def test_a_tip_that_cannot_be_read_keeps_the_worktree(lane, monkeypatch):
    from yggdrasil import t3, util as ygg_util
    _merged()
    original = ygg_util.trusted_git

    def failing(*args, **kw):
        if "merge-base" in args:
            return subprocess.CompletedProcess(args, 128, "", "fatal: not a valid object")
        return original(*args, **kw)

    monkeypatch.setattr(ygg_util, "trusted_git", failing)
    t3.finish_closes()
    assert lane["worktree"].exists()

