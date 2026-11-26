"""v1.1.1: cleanup and recovery never mix generations. Cleanup holds the lane lock across its eligibility checks, the
quarantine and the recovery; every journal-lock write of `clones.quarantine`, `do_next` and `missing_box` acts only on
the record it started from (same generation, same nonce, the expected state). The tender's report and the assembly
are stand-ins."""

import os

import pytest

from yggdrasil import clones, quarantine, util

PROJECT, LANE = "alpha-p1", "pm1tl1"
BRANCH = "ygg/alpha-p1/lane/pm1tl1"
LOCK = "lane-clone-" + PROJECT + "-" + LANE
FINISHED = {"result": "ok", "counts": dict.fromkeys(quarantine.COUNTS, 0), "metadata": [], "size": 2048,
            "head": "a" * 40, "lane_branch": "a" * 40, "detached": False}


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    home = tmp_path / "home"
    folder = home / ".ygg-worktrees" / PROJECT / "lane"

    def box(generation, nonce, state="ready"):
        path = folder / clones.box_name(LANE, generation)
        (path / "checkout" / ".git").mkdir(parents=True, exist_ok=True)
        info = os.stat(path)
        return {"kind": "lane", "generation": generation, "box": str(path), "path": str(path / "checkout"),
                "home": str(home), "branch": BRANCH, "state": state, "nonce": nonce, "box_device": info.st_dev,
                "box_inode": info.st_ino, "given_head": "a" * 40, "given_ref": "a" * 40}
    clones.write(PROJECT, LANE, box(1, "1" * 32))
    monkeypatch.setattr(clones, "report", lambda record_, head: dict(FINISHED))
    monkeypatch.setattr(clones, "_rev", lambda home_, ref: "a" * 40)
    monkeypatch.setattr(clones, "lane_alive", lambda tl, besides=None: False)
    monkeypatch.setattr(quarantine, "end_orphans", lambda lane_: None)
    monkeypatch.setattr("yggdrasil.lanedrop.interrupted", lambda project, lane_: None)
    monkeypatch.setattr("yggdrasil.tracker.projects", lambda: [{"id": PROJECT}])
    built = []

    def assemble(project, tl, home_, at=None):
        with clones.lane_lock(project, tl), util.locked():
            fresh = box(clones.next_generation(project, tl, str(home_)), "f" * 32)
            clones.write(project, tl, fresh)
        built.append(at)
        return fresh
    monkeypatch.setattr(clones, "assemble", assemble)
    return {"box": box, "built": built}


def _newer(lane):
    """Another holder rebuilt the lane meanwhile: generation 2, its own nonce, ready."""
    clones.write(PROJECT, LANE, lane["box"](2, "2" * 32))


def _metadata(generation):
    return (clones.quarantine_dir(PROJECT) / (clones.box_name(LANE, generation) + ".json")).exists()


def test_the_quarantine_record_write_stops_when_a_newer_generation_replaced_the_record(lane, monkeypatch):
    def report(record_, head):
        _newer(lane)  # replaced while the report ran, before the journal-lock write
        return dict(FINISHED)
    monkeypatch.setattr(clones, "report", report)
    with pytest.raises(util.YggError) as caught:
        clones.quarantine(PROJECT, LANE, reason="kept when WP-2 was cancelled", next_step="rebuild")
    assert caught.value.code == util.EXIT_REFUSED
    record = clones.read(PROJECT, LANE)
    assert (record["generation"], record["state"]) == (2, "ready") and "reason" not in record
    assert not _metadata(1) and not _metadata(2)


def test_the_metadata_write_stops_when_a_newer_generation_replaced_the_record(lane, monkeypatch):
    def boundary(name):
        if name == "quarantine-before-metadata":
            _newer(lane)
    monkeypatch.setattr("yggdrasil.faults.boundary", boundary)
    with pytest.raises(util.YggError):
        clones.quarantine(PROJECT, LANE, reason="kept when WP-2 was cancelled", next_step="rebuild")
    assert not _metadata(1) and not _metadata(2)
    assert clones.read(PROJECT, LANE)["state"] == "ready"


@pytest.mark.parametrize("next_step", ["rebuild", "close"])
def test_do_next_leaves_a_newer_record_alone(lane, monkeypatch, next_step):
    stale = dict(lane["box"](1, "1" * 32, state="quarantining"), next=next_step)
    _newer(lane)
    real_read, calls = clones.read, []

    def read(project, name):
        calls.append(name)
        return dict(stale) if len(calls) == 1 else real_read(project, name)  # the first read is the stale snapshot
    monkeypatch.setattr(clones, "read", read)
    with pytest.raises(util.YggError):
        clones.do_next(PROJECT, LANE)
    record = real_read(PROJECT, LANE)
    assert (record["generation"], record["state"]) == (2, "ready") and "replaced_pending" not in record
    assert lane["built"] == []


def test_missing_box_leaves_a_newer_record_alone(lane):
    stale = clones.read(PROJECT, LANE)
    _newer(lane)
    with pytest.raises(util.YggError):
        clones.missing_box(PROJECT, LANE, stale)
    record = clones.read(PROJECT, LANE)
    assert (record["generation"], record["state"]) == (2, "ready") and not _metadata(1)


def test_cleanup_holds_the_lane_lock_across_its_checks_the_quarantine_and_the_rebuild(lane, monkeypatch):
    monkeypatch.setattr(quarantine, "_lane_done", lambda project, lane_: "merged")
    held = []
    real_quarantine, real_next = clones.quarantine, clones.do_next
    monkeypatch.setattr(clones, "lane_alive", lambda tl, besides=None: held.append(("alive", util.lock_held(LOCK)))
                        or False)
    monkeypatch.setattr(clones, "quarantine", lambda *a, **k: held.append(("quarantine", util.lock_held(LOCK)))
                        or real_quarantine(*a, **k))
    monkeypatch.setattr(clones, "do_next", lambda *a, **k: held.append(("next", util.lock_held(LOCK)))
                        or real_next(*a, **k))
    quarantine.cleanup_step()
    assert clones.read(PROJECT, LANE)["state"] == "closed" and _metadata(1)
    assert ("alive", True) in held and ("quarantine", True) in held and ("next", True) in held


def test_cleanup_checks_again_under_the_lane_lock(lane, monkeypatch):
    monkeypatch.setattr(quarantine, "_lane_done", lambda project, lane_: "merged")
    # an activation that started while cleanup waited for the lane lock
    monkeypatch.setattr(clones, "lane_alive", lambda tl, besides=None: util.lock_held(LOCK))
    quarantine.cleanup_step()
    assert clones.read(PROJECT, LANE)["state"] == "ready" and not _metadata(1)


def test_a_recovery_in_cleanup_runs_under_the_lane_lock(lane, monkeypatch):
    clones.write(PROJECT, LANE, dict(lane["box"](1, "1" * 32, state="quarantining"), next="rebuild",
                                     reason="assembly was interrupted"))
    held = []
    real_next = clones.do_next
    monkeypatch.setattr(clones, "do_next", lambda *a, **k: held.append(util.lock_held(LOCK)) or real_next(*a, **k))
    quarantine.cleanup_step()
    record = clones.read(PROJECT, LANE)
    assert (record["generation"], record["state"]) == (2, "ready") and _metadata(1) and held == [True]
