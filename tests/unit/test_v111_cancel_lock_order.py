"""v1.1.1: one lock order for clone lanes, the lane lock before the journal lock. A cancel and `--finish` hold the
journal lock across every check, the drop record, the quarantine and the new generation's assembly; the assembly
takes the lane lock, so the cancel takes that lane lock first. The quarantine and the assembly themselves are
stand-ins that take the real lane lock, as `clones.assemble` does."""

import pytest

from yggdrasil import cancel, clones, util

PROJECT, LANE = "alpha-p1", "pm1tl1"


class Issue(dict):
    def __init__(self, identifier, lane):
        super().__init__(kind="work-package", lane=lane, state="authorised")
        self.id = identifier


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    from yggdrasil import lanedrop, review, state, tracker
    lanes = {"WP-2": [LANE]}
    seen = {"quarantined": [], "reset": [], "completed": [], "reads": []}

    def read_issue(project, identifier):
        seen["reads"].append(util.lock_held("journal"))
        values = lanes.get(identifier)
        if not values:
            return None
        return Issue(identifier, values.pop(0) if len(values) > 1 else values[0])

    monkeypatch.setattr(tracker, "project", lambda name: {"id": name, "pm": "pm1"})
    monkeypatch.setattr(tracker, "read_issue", read_issue)
    monkeypatch.setattr(tracker, "issues", lambda project: [])
    monkeypatch.setattr(tracker, "ensure_writable", lambda: None)
    monkeypatch.setattr(state, "all_nodes", lambda: {})
    monkeypatch.setattr(state, "check_user_only", lambda verb: None)
    monkeypatch.setattr(review, "apply_cap_answers", lambda: None)
    monkeypatch.setattr(cancel, "_identity", lambda project, name: None)
    monkeypatch.setattr(cancel, "interrupted", lambda project=None, lane=None: [])
    monkeypatch.setattr(cancel, "_home", lambda project: str(tmp_path / "home"))
    monkeypatch.setattr(cancel, "_running_refusal", lambda lane, nodes: None)
    monkeypatch.setattr(cancel, "_clone", lambda project, lane: {"kind": "lane", "generation": 1, "state": "ready"})
    monkeypatch.setattr(cancel, "_finish_state", lambda project, packages, lane, nodes: None)
    monkeypatch.setattr(lanedrop, "plan", lambda project, lane, packages, repo: {
        "lane": lane, "packages": list(packages), "drop_point": "d" * 40, "head": "e" * 40, "dropped": []})
    monkeypatch.setattr(lanedrop, "record", lambda project, plan: "drop-1")
    monkeypatch.setattr(lanedrop, "reset", lambda repo, project, lane, point, clean=False: seen["reset"].append(point))
    monkeypatch.setattr(lanedrop, "complete", lambda project, identifier: seen["completed"].append(identifier))
    monkeypatch.setattr(lanedrop, "summary", lambda plan: "cancelled\n")

    def quarantine_clone(project, lane, drop):
        # what the quarantine's rebuild does: `clones.assemble` takes the lane lock, the journal lock inside it
        with clones.lane_lock(project, lane):
            with util.locked():
                seen["quarantined"].append((lane, drop.get("drop_point")))
    monkeypatch.setattr(cancel, "_quarantine_clone", quarantine_clone)
    return {"lanes": lanes, "seen": seen}


def test_the_lane_lock_is_still_refused_inside_the_journal_lock():
    with util.locked():
        with pytest.raises(RuntimeError, match="the lane lock is taken before the journal lock"):
            clones.lane_lock(PROJECT, LANE)


def test_a_held_lane_lock_is_re_entered_inside_the_journal_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    with clones.lane_lock(PROJECT, LANE):
        with util.locked():
            with clones.lane_lock(PROJECT, LANE):
                assert util.lock_held("lane-clone-" + PROJECT + "-" + LANE)
            with pytest.raises(RuntimeError, match="lock order"):
                clones.lane_lock(PROJECT, "pm1tl2")  # another lane's lock is never taken inside the journal lock


def test_cancel_takes_the_lane_lock_before_the_journal_lock(world):
    assert cancel.cancel(PROJECT, ["WP-2"]) == "cancelled\n"
    seen = world["seen"]
    assert seen["quarantined"] == [(LANE, "d" * 40)]
    assert seen["reset"] == ["d" * 40] and seen["completed"] == ["drop-1"]
    assert seen["reads"] and all(seen["reads"])  # every read of the named packages is under the journal lock


def test_cancel_refuses_when_the_packages_lane_changed_before_the_lock(world):
    world["lanes"]["WP-2"] = [LANE, "pm1tl2"]
    with pytest.raises(util.YggError) as caught:
        cancel.cancel(PROJECT, ["WP-2"])
    assert caught.value.code == util.EXIT_REFUSED and "try again" in caught.value.message
    assert world["seen"]["quarantined"] == [] and world["seen"]["reset"] == []


def test_finish_takes_the_lane_lock_before_the_journal_lock(world, monkeypatch):
    record = {"id": "drop-1", "lane": LANE, "packages": ["WP-2"], "drop_point": "d" * 40, "clone_generation": 1}
    monkeypatch.setattr(cancel, "interrupted", lambda project=None, lane=None: [dict(record, project=PROJECT)])
    monkeypatch.setattr(cancel, "_preview", lambda project, record_: (["a listing line"], "abcd1234",
                                                                      {"extra": [], "drop_point": "d" * 40}))
    with pytest.raises(cancel.PreviewRefusal):
        cancel.cancel(PROJECT, [], finish=LANE)
    assert cancel.cancel(PROJECT, [], finish=LANE, code="abcd1234").startswith("WP-2 cancelled; lane pm1tl1 reset")
    assert world["seen"]["quarantined"] == [(LANE, "d" * 40)] and world["seen"]["completed"] == ["drop-1"]



def test_the_preview_names_its_code_after_yes():
    error = cancel._preview_refusal(["a listing line"], "abcd1234")
    assert error.stdout.splitlines()[-1] == "finish code: abcd1234"
    assert "--yes abcd1234" in error.message


def test_the_cli_preview_prints_the_listing_and_names_the_code_after_yes(monkeypatch, capsys):
    from yggdrasil import cli

    def preview(project, packages, finish=None, code=None):
        raise cancel._preview_refusal(["a listing line"], "abcd1234")
    monkeypatch.setattr(cancel, "cancel", preview)
    code, output = cli.dispatch(cli.build().parse_args(["wp", "cancel", PROJECT, "--finish", LANE]))
    assert code == util.EXIT_REFUSED and output.splitlines()[-1] == "finish code: abcd1234"
    assert "--yes abcd1234" in capsys.readouterr().err


def test_the_preview_of_a_clone_lane_names_the_box_of_the_drop_records_generation(tmp_path, monkeypatch):
    from yggdrasil import lanedrop
    home = tmp_path / "home"
    monkeypatch.setattr(cancel, "_home", lambda project: str(home))
    monkeypatch.setattr(lanedrop, "plan", lambda project, lane, packages, repo: {
        "lane": lane, "packages": list(packages), "drop_point": "d" * 40, "head": "e" * 40, "dropped": []})
    record = {"id": "drop-1", "lane": LANE, "packages": ["WP-2"], "drop_point": "d" * 40, "head": "e" * 40,
              "commits": [], "clone_generation": 1}
    lines, digest, _ = cancel._preview(PROJECT, record)
    box = home / ".ygg-worktrees" / PROJECT / "lane" / "pm1tl1.g000001"
    assert lines[0].startswith("the box " + str(box) + " is quarantined where it stands and kept")
    assert len(digest) == 8


def _interrupted_calls(monkeypatch, *answers):
    """`cancel.interrupted` answering each call in turn (the last answer repeats)."""
    calls = list(answers)

    def interrupted(project=None, lane=None):
        value = calls.pop(0) if len(calls) > 1 else calls[0]
        return [dict(r, project=PROJECT) for r in value]
    monkeypatch.setattr(cancel, "interrupted", interrupted)


DROP = {"id": "drop-1", "lane": LANE, "packages": ["WP-2"], "drop_point": "d" * 40, "clone_generation": 1}


@pytest.mark.parametrize("later", [[], [dict(DROP, id="drop-2")], [dict(DROP, commits=["c" * 40])]])
def test_finish_refuses_when_the_drop_completed_or_changed_while_it_waited(world, monkeypatch, later):
    _interrupted_calls(monkeypatch, [DROP], later)
    previews = []
    monkeypatch.setattr(cancel, "_preview", lambda project, record_: previews.append(record_) or (
        [], "abcd1234", {"extra": [], "drop_point": "d" * 40}))
    with pytest.raises(util.YggError) as caught:
        cancel.cancel(PROJECT, [], finish=LANE, code="abcd1234")
    assert caught.value.code == util.EXIT_REFUSED and "while --finish waited" in caught.value.message
    assert previews == [] and world["seen"]["quarantined"] == [] and world["seen"]["reset"] == []


def test_finish_checks_the_nodes_read_under_the_locks(world, monkeypatch):
    from yggdrasil import state
    _interrupted_calls(monkeypatch, [DROP])
    worker = {"pm1tl1w1": {"role": "worker", "parent": LANE}}
    reads = []

    def all_nodes():
        reads.append(util.lock_held("journal") and util.lock_held("lane-clone-" + PROJECT + "-" + LANE))
        return dict(worker) if reads[-1] else {}
    monkeypatch.setattr(state, "all_nodes", all_nodes)
    seen = []
    monkeypatch.setattr(cancel, "_running_refusal", lambda lane, nodes: seen.append(dict(nodes)) or (
        "the lane still has a running agent (pm1tl1w1); stop it first" if "pm1tl1w1" in nodes else None))
    with pytest.raises(util.YggError) as caught:
        cancel.cancel(PROJECT, [], finish=LANE, code="abcd1234")
    assert "running agent (pm1tl1w1)" in caught.value.message
    assert seen == [worker] and world["seen"]["reset"] == []
