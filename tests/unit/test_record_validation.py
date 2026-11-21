import json

import pytest

from yggdrasil import launcher, tracker, util
from yggdrasil.util import YggError


def test_unknown_role_and_bad_pairs_are_corrupt(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps({"nodes": [{"id": "pm1tl9", "role": "../x", "parent": "pm1"}]}))
    with pytest.raises(YggError) as caught:
        tracker.node_entries(path)
    assert caught.value.code == 8 and "role ../x" in caught.value.message
    path.write_text(json.dumps({"nodes": [{"id": "pm1tl9", "role": "tl", "pairs_with": ["Bad Id"]}]}))
    with pytest.raises(YggError):
        tracker.node_entries(path)


def test_relative_home_resolves_against_the_tracker_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "q" / "ygg-tracker"))
    monkeypatch.setenv("YGG_TRACKER", "files")
    monkeypatch.chdir(tmp_path)
    assert tracker.programme_home({"home": "./demo-home"}) == str((tmp_path / "q" / "demo-home").resolve())


def test_allowed_home_refuses_root_and_own_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "tracker"))
    monkeypatch.setenv("YGG_TRACKER", "files")
    (tmp_path / "tracker" / "inside").mkdir(parents=True)
    (tmp_path / "work").mkdir()
    assert not launcher.allowed_home("/")
    assert not launcher.allowed_home(str(tmp_path / "tracker" / "inside"))
    assert not launcher.allowed_home("relative/path")
    assert launcher.allowed_home(str(tmp_path / "work"))


def test_project_nodes_need_a_role_and_ids_match_fully(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps({"nodes": [{"id": "pm1tl9", "parent": "pm1"}]}))
    with pytest.raises(YggError):
        tracker.node_entries(path)
    assert tracker.node_entries(path, roles=tracker.PROGRAMME_ROLES, default_role="reviewer")
    path.write_text(json.dumps({"nodes": [{"id": "pm1tl9", "role": "tl", "pairs_with": False}]}))
    with pytest.raises(YggError):
        tracker.node_entries(path)
    assert not tracker._valid_id("pm1\n")


def test_project_records_need_their_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    with pytest.raises(YggError):
        tracker._checked(tmp_path / "project.json", {"id": "alpha-p1", "programme": "alpha"}, tracker.PROJECT_IDS)


def test_safety_folder_is_not_an_allowed_home(tmp_path, monkeypatch):
    from yggdrasil import safety
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "tracker"))
    monkeypatch.setenv("YGG_TRACKER", "files")
    inside = safety.root() / "interlocks"
    inside.mkdir(parents=True)
    assert not launcher.allowed_home(str(inside))


def _tracker(tmp_path, monkeypatch, nodes_by_project):
    from yggdrasil import state
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "tracker"))
    monkeypatch.setenv("YGG_TRACKER", "files")
    programme = tracker.programme_path("alpha")
    programme.parent.mkdir(parents=True)
    programme.write_text(json.dumps({"id": "alpha", "domain": "alpha"}))
    for number, nodes in nodes_by_project.items():
        project = "alpha-p" + number
        tracker.project_dir(project).mkdir(parents=True)
        (tracker.project_dir(project) / "project.json").write_text(
            json.dumps({"id": project, "pm": "pm" + number, "programme": "alpha"}))
        tracker.nodes_file(project).parent.mkdir(parents=True, exist_ok=True)
        tracker.nodes_file(project).write_text(json.dumps({"nodes": nodes}))
    return state


GOOD = [{"id": "pm1tl1", "role": "tl", "parent": "pm1"}, {"id": "pm1su1", "role": "su", "parent": "pm1",
        "pairs_with": ["pm1tl1"]}, {"id": "pm1tl1w1", "role": "worker", "parent": "pm1tl1"}]


def test_valid_topology_is_accepted(tmp_path, monkeypatch):
    state = _tracker(tmp_path, monkeypatch, {"1": GOOD, "2": []})
    assert set(state.all_nodes()) == {"pm1", "pm2", "pm1tl1", "pm1su1", "pm1tl1w1"}


@pytest.mark.parametrize("bad", [
    {"id": "pm1tl2", "role": "tl"},                                          # no parent
    {"id": "pm1tl2", "role": "tl", "parent": "nobody"},                      # unknown parent
    {"id": "pm1tl2", "role": "tl", "parent": "pm1tl2"},                      # itself
    {"id": "pm1tl2", "role": "tl", "parent": "pm1tl1"},                      # wrong parent role
    {"id": "pm1tl2", "role": "tl", "parent": "pm2"},                         # another project's PM
    {"id": "pm1w9", "role": "worker", "parent": "pm1tl1w1"},                 # worker under a worker
    {"id": "pm1su2", "role": "su", "parent": "pm1", "pairs_with": ["nobody"]},   # unknown pairing
    {"id": "pm1su2", "role": "su", "parent": "pm1", "pairs_with": ["pm1su2"]},   # paired with itself
    {"id": "pm1su2", "role": "su", "parent": "pm1", "pairs_with": ["pm1tl1w1"]},  # not a team lead
])
def test_bad_topology_is_corrupt(tmp_path, monkeypatch, bad):
    state = _tracker(tmp_path, monkeypatch, {"1": GOOD + [bad], "2": []})
    with pytest.raises(YggError) as caught:
        state.all_nodes()
    assert caught.value.code == 8 and bad["id"] in caught.value.message


def test_cross_project_pairing_is_corrupt(tmp_path, monkeypatch):
    state = _tracker(tmp_path, monkeypatch, {"1": GOOD, "2": [
        {"id": "pm2su1", "role": "su", "parent": "pm2", "pairs_with": ["pm1tl1"]}]})
    with pytest.raises(YggError):
        state.all_nodes()


@pytest.mark.parametrize("text", ["{", "[]", "null", "42"])
def test_direct_reads_of_torn_records_are_corrupt(tmp_path, monkeypatch, text):
    _tracker(tmp_path, monkeypatch, {"1": GOOD})
    tracker.programme_path("alpha").write_text(text)
    with pytest.raises(YggError) as caught:
        tracker.programme("alpha")
    assert caught.value.code == 8
    (tracker.project_dir("alpha-p1") / "project.json").write_text(text)
    with pytest.raises(YggError):
        tracker.project("alpha-p1")
    assert tracker.project("alpha-p9") is None and tracker.programme("beta") is None


@pytest.mark.parametrize("bad", [
    {"id": "pm1x1", "role": "wizard", "parent": "pm1"},                      # unknown role
    {"id": "pm1rv1", "role": "reviewer", "parent": "pm1", "pairs_with": ["pm1tl1"]},  # a project reviewer pairs with no node
    {"id": "pm1rv1", "role": "reviewer", "parent": "pm1tl1"},                # a project reviewer hangs under the PM
    {"id": "pm1rv1", "role": "reviewer", "parent": "alpha"},                 # even parented to the programme
    {"id": "pm1tl2", "role": "tl", "parent": "pm1", "pairs_with": ["pm1su1"]},     # pairing a non-lead
    {"id": "pm1tl2", "role": "tl", "parent": "pm1", "pairs_with": ["nobody"]},     # pairing an unknown node
    {"id": "pm1tl1w2", "role": "worker", "parent": "pm1tl1", "pairs_with": ["pm1tl1w2"]},  # pairing itself
])
def test_project_file_roles_and_pairings_are_checked_for_every_role(tmp_path, monkeypatch, bad):
    state = _tracker(tmp_path, monkeypatch, {"1": GOOD + [bad]})
    with pytest.raises(YggError) as caught:
        state.all_nodes()
    assert caught.value.code == 8 and bad["id"] in caught.value.message


def test_project_file_holds_a_project_reviewer_under_the_pm(tmp_path, monkeypatch):
    # A11 amendment 23: `ygg pid approve` adds `<pm>rv1` (role reviewer, parent the PM) to the project's nodes file.
    state = _tracker(tmp_path, monkeypatch, {"1": GOOD + [{"id": "pm1rv1", "role": "reviewer", "parent": "pm1"}]})
    node = state.all_nodes()["pm1rv1"]
    assert (node["role"], node["parent"], node["project"]) == ("reviewer", "pm1", "alpha-p1")


def test_programme_file_holds_only_reviewers(tmp_path, monkeypatch):
    state = _tracker(tmp_path, monkeypatch, {"1": GOOD})
    reviewers = tracker.nodes_file(programme_name="alpha")
    reviewers.write_text(json.dumps({"nodes": [{"id": "rv1"}]}))
    assert state.all_nodes()["rv1"]["role"] == "reviewer"
    reviewers.write_text(json.dumps({"nodes": [{"id": "rv1"}, {"id": "tl9", "role": "tl", "parent": "pm1"}]}))
    with pytest.raises(YggError) as caught:
        state.all_nodes()
    assert caught.value.code == 8 and "tl9" in caught.value.message


def test_reviewers_pair_with_nothing(tmp_path, monkeypatch):
    state = _tracker(tmp_path, monkeypatch, {"1": GOOD})
    reviewers = tracker.nodes_file(programme_name="alpha")
    reviewers.write_text(json.dumps({"nodes": [{"id": "rv1", "pairs_with": []}]}))
    assert state.all_nodes()["rv1"]["pairs_with"] == []
    reviewers.write_text(json.dumps({"nodes": [{"id": "rv1", "pairs_with": ["pm1tl1"]}]}))
    with pytest.raises(YggError) as caught:
        state.all_nodes()
    assert caught.value.code == 8 and "rv1" in caught.value.message


def test_node_add_refuses_a_paired_reviewer(tmp_path, monkeypatch):
    from yggdrasil import commands
    _tracker(tmp_path, monkeypatch, {"1": GOOD})
    with pytest.raises(YggError) as caught:
        commands.node_add("rv1", "reviewer", "alpha", "pm1tl1")
    assert caught.value.code == util.EXIT_USAGE and "--pairs-with" in caught.value.message
    assert not tracker.nodes_file(programme_name="alpha").exists()


def _shape(project, shape, actor="user"):
    from yggdrasil import commands
    issue = tracker.Issue(project, "pid", {"schema": "ygg.pid.v1", "project": project, "programme": "alpha",
                                           "draft": True, "kind": "pid", "actor": "user"})
    issue.records.append(tracker.record("shape", None, actor=actor, shape=shape))
    tracker.write_text(tracker.document_path(project, "pid"), commands._render_doc(issue))


PM_WORKER = {"id": "pm1w1", "role": "worker", "parent": "pm1"}


def test_pm_parented_worker_needs_a_user_collapsed_shape(tmp_path, monkeypatch):
    state = _tracker(tmp_path, monkeypatch, {"1": [PM_WORKER]})
    with pytest.raises(YggError) as caught:
        state.all_nodes()
    assert caught.value.code == 8 and "pm1w1" in caught.value.message and "collapsed" in caught.value.message
    _shape("alpha-p1", "collapsed", actor="pm1")  # a shape record not written by the user does not count
    with pytest.raises(YggError):
        state.all_nodes()
    _shape("alpha-p1", "collapsed")
    assert state.all_nodes()["pm1w1"]["parent"] == "pm1"


def test_a_collapsed_project_with_pm_workers_cannot_turn_standard(tmp_path, monkeypatch):
    from yggdrasil import commands
    state = _tracker(tmp_path, monkeypatch, {"1": [PM_WORKER]})
    _shape("alpha-p1", "collapsed")
    with pytest.raises(YggError) as caught:
        commands.project_shape("alpha-p1", False, True)
    assert caught.value.code == util.EXIT_REFUSED and "pm1w1" in caught.value.message
    assert state.all_nodes()["pm1w1"]["parent"] == "pm1"
