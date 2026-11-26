"""v1.1.1: cleanup and recovery through the shared quarantine primitive (clones.quarantine and do_next), the
cancel's quarantine of the drop record's generation, the box listing of `ygg status`, the time-out alert and the
merge `--repo` box check. The tender's report and the assembly are stand-ins here."""

import hashlib
import json
import os

import pytest

from yggdrasil import clones, quarantine, util

PROJECT, LANE = "alpha-p1", "pm1tl1"
BRANCH = "ygg/alpha-p1/lane/pm1tl1"
FINISHED = {"result": "ok", "counts": dict.fromkeys(quarantine.COUNTS, 0), "metadata": [], "size": 2048,
            "head": "a" * 40, "lane_branch": "a" * 40, "detached": False}


class Fault(Exception):
    pass


def _digest(box):
    found = {}
    for folder, dirs, files in os.walk(box, followlinks=False):
        for name in dirs + files:
            path = os.path.join(folder, name)
            if os.path.islink(path):
                found[os.path.relpath(path, box)] = os.readlink(path)
            elif os.path.isfile(path):
                found[os.path.relpath(path, box)] = hashlib.sha256(open(path, "rb").read()).hexdigest()
            else:
                found[os.path.relpath(path, box)] = "d"
    return found


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    home = tmp_path / "home"
    folder = home / ".ygg-worktrees" / PROJECT / "lane"
    box = folder / "pm1tl1.g000001"
    (box / "checkout" / ".git").mkdir(parents=True)
    (box / "checkout" / "notes.txt").write_text("keep me")
    info = os.stat(box)
    record = {"kind": "lane", "generation": 1, "box": str(box), "path": str(box / "checkout"), "home": str(home),
              "branch": BRANCH, "state": "ready", "box_device": info.st_dev, "box_inode": info.st_ino,
              "given_head": "a" * 40, "given_ref": "a" * 40}
    clones.write(PROJECT, LANE, record)
    reports = {"value": dict(FINISHED)}
    monkeypatch.setattr(clones, "report", lambda record_, head: dict(reports["value"]))
    monkeypatch.setattr(clones, "_rev", lambda home_, ref: "a" * 40)
    monkeypatch.setattr(clones, "lane_alive", lambda tl, besides=None: False)
    monkeypatch.setattr(quarantine, "end_orphans", lambda lane_: None)
    monkeypatch.setattr("yggdrasil.lanedrop.interrupted", lambda project, lane_: None)
    built = []

    def assemble(project, tl, home_, at=None):
        with util.locked():
            generation = clones.next_generation(project, tl, str(home_))
            new = folder / clones.box_name(tl, generation)
            (new / "checkout" / ".git").mkdir(parents=True)
            made = os.stat(new)
            fresh = dict(record, generation=generation, box=str(new), path=str(new / "checkout"), state="ready",
                         made_from=at, box_device=made.st_dev, box_inode=made.st_ino)
            clones.write(project, tl, fresh)
        built.append(at)
        return fresh
    monkeypatch.setattr(clones, "assemble", assemble)
    return {"home": home, "folder": folder, "box": box, "built": built, "tmp": tmp_path, "reports": reports}


def _fault_at(monkeypatch, name):
    def boundary(reached):
        if reached == name:
            raise Fault(name)
    monkeypatch.setattr("yggdrasil.faults.boundary", boundary)


def _merged(monkeypatch, why="merged"):
    monkeypatch.setattr(quarantine, "_lane_done", lambda project, lane_: why)
    monkeypatch.setattr("yggdrasil.tracker.projects", lambda: [{"id": PROJECT}])


def test_cleanup_quarantines_the_box_where_it_stands_and_closes_the_lane(lane, monkeypatch):
    _merged(monkeypatch)
    before = _digest(lane["box"])
    quarantine.cleanup_step()
    assert _digest(lane["box"]) == before
    assert sorted(os.listdir(lane["folder"])) == ["pm1tl1.g000001"]
    record = clones.read(PROJECT, LANE)
    assert record["state"] == "closed" and record["generation"] == 1
    kept = json.loads((clones.quarantine_dir(PROJECT) / "pm1tl1.g000001.json").read_text())
    assert kept["path"] == str(lane["box"]) and kept["reason"] == "kept after lane ygg/alpha-p1/lane/pm1tl1 merged"
    assert "self_contained" not in kept and lane["built"] == []


def test_cleanup_waits_while_an_activation_of_the_lane_runs(lane, monkeypatch):
    _merged(monkeypatch)
    monkeypatch.setattr(clones, "lane_alive", lambda tl, besides=None: True)
    quarantine.cleanup_step()
    assert clones.read(PROJECT, LANE)["state"] == "ready"
    assert not clones.quarantine_dir(PROJECT).exists()


@pytest.mark.parametrize("boundary", ["quarantine-before-metadata", "quarantine-after-metadata"])
def test_a_crash_at_either_boundary_is_finished_by_a_later_tick(lane, monkeypatch, boundary):
    _merged(monkeypatch)
    _fault_at(monkeypatch, boundary)
    with pytest.raises(Fault):
        quarantine.cleanup_step()
    assert clones.read(PROJECT, LANE)["state"] == "quarantining"
    target = clones.quarantine_dir(PROJECT) / "pm1tl1.g000001.json"
    assert target.exists() == (boundary == "quarantine-after-metadata")
    monkeypatch.setattr("yggdrasil.faults.boundary", lambda name: None)
    quarantine.cleanup_step()
    quarantine.cleanup_step()  # nothing more to do
    assert clones.read(PROJECT, LANE)["state"] == "closed" and clones.read(PROJECT, LANE)["generation"] == 1
    assert sorted(os.listdir(clones.quarantine_dir(PROJECT))) == ["pm1tl1.g000001.json"]
    assert "merged" in json.loads(target.read_text())["reason"]
    assert sorted(os.listdir(lane["folder"])) == ["pm1tl1.g000001"]


def test_a_tick_leaves_an_interrupted_cancel_to_finish(lane, monkeypatch):
    _merged(monkeypatch)
    drop = {"id": "drop-1", "packages": ["WP-2"], "drop_point": "d" * 40, "clone_generation": 1}
    _fault_at(monkeypatch, "quarantine-before-metadata")
    with pytest.raises(Fault):
        quarantine.quarantine_clone(PROJECT, LANE, drop)
    monkeypatch.setattr("yggdrasil.faults.boundary", lambda name: None)
    monkeypatch.setattr("yggdrasil.lanedrop.interrupted", lambda project, lane_: {"id": "drop-1"})
    quarantine.cleanup_step()
    assert clones.read(PROJECT, LANE)["state"] == "quarantining" and lane["built"] == []


def test_cancel_quarantines_the_drop_records_generation_at_the_drop_point(lane):
    drop = {"id": "drop-1", "packages": ["WP-2"], "drop_point": "d" * 40, "clone_generation": 1}
    quarantine.quarantine_clone(PROJECT, LANE, drop)
    record = clones.read(PROJECT, LANE)
    assert record["generation"] == 2 and record["box"].endswith("pm1tl1.g000002") and lane["built"] == ["d" * 40]
    kept = json.loads((clones.quarantine_dir(PROJECT) / "pm1tl1.g000001.json").read_text())
    assert kept["reason"] == "kept when WP-2 was cancelled" and kept.get("odd_check") is None
    assert lane["box"].is_dir() and (lane["box"] / "checkout" / "notes.txt").read_text() == "keep me"
    quarantine.quarantine_clone(PROJECT, LANE, drop)  # --finish again: that generation is done
    assert clones.read(PROJECT, LANE)["generation"] == 2 and lane["built"] == ["d" * 40]


@pytest.mark.parametrize("boundary", ["quarantine-before-metadata", "quarantine-after-metadata"])
def test_finish_resumes_an_interrupted_cancel_at_the_drop_point(lane, monkeypatch, boundary):
    drop = {"id": "drop-1", "packages": ["WP-2", "WP-3"], "drop_point": "d" * 40, "clone_generation": 1}
    _fault_at(monkeypatch, boundary)
    with pytest.raises(Fault):
        quarantine.quarantine_clone(PROJECT, LANE, drop)
    monkeypatch.setattr("yggdrasil.faults.boundary", lambda name: None)
    quarantine.quarantine_clone(PROJECT, LANE, drop)
    assert lane["built"] == ["d" * 40]
    assert sorted(os.listdir(clones.quarantine_dir(PROJECT))) == ["pm1tl1.g000001.json"]
    kept = json.loads((clones.quarantine_dir(PROJECT) / "pm1tl1.g000001.json").read_text())
    assert kept["reason"] == "kept when WP-2 and WP-3 were cancelled"


def test_status_lists_the_live_box_first_then_quarantined_newest_first(lane):
    drop = {"id": "d1", "packages": ["WP-2"], "drop_point": "d" * 40, "clone_generation": 1}
    quarantine.quarantine_clone(PROJECT, LANE, drop)
    quarantine.quarantine_clone(PROJECT, LANE, dict(drop, packages=["WP-3"], clone_generation=2))
    (lane["folder"] / "stray").mkdir()
    lines = quarantine.status_lines(PROJECT, lane["home"])
    g1, g2, g3 = (str(lane["folder"] / clones.box_name(LANE, n)) for n in (1, 2, 3))
    assert lines[0].startswith(g3 + ": live; do not remove; lane ygg/alpha-p1/lane/pm1tl1, generation 3")
    assert lines[1].startswith(g2 + ": quarantined ") and "kept when WP-3 was cancelled" in lines[1]
    assert lines[2].startswith(g1 + ": quarantined ") and "kept when WP-2 was cancelled" in lines[2]
    for line in lines[1:3]:
        assert "may hold work that was not received" in line and "live; do not remove" not in line
        assert "the report saw refs 0, stash 0, commits 0, changed 0, untracked 0, ignored 0" in line
        assert "metadata the report saw: none" in line and "nothing" not in line and "cp -RP" in line
    assert lines[3] == str(lane["folder"] / "stray") + ": unknown folder"
    assert lines[4].startswith("quarantined boxes: 2, 4.0 KiB recorded")


def test_status_says_report_did_not_finish_and_resolves_dependencies_as_text(lane):
    other = lane["tmp"] / "other" / ".git" / "objects"
    relative = os.path.relpath(other, lane["box"] / "checkout" / ".git" / "objects")
    lane["reports"]["value"] = {"result": "odd", "check": "3a", "kind": "outside", "path": "objects/info/alternates",
                                "walk": {"special": [], "ancestry": [], "outside": [
                                    {"path": "objects/info/alternates", "kind": "file", "lines": [relative]},
                                    {"path": "objects/pack", "kind": "link", "target": "../../../keep"}]}}
    clones._replace({"id": LANE}, PROJECT, LANE, {"check": "3a", "kind": "outside", "path": "objects/info/alternates"},
                    None)
    line = next(l for l in quarantine.status_lines(PROJECT, lane["home"]) if l.startswith(str(lane["box"]) + ":"))
    assert "quarantined" in line and "odd:" in line and "report did not finish" in line
    after = line.partition("outside dependencies the walk saw:")[2]
    assert str(other) in after
    assert str(lane["box"] / "keep") + " (inside the box)" in after
    assert json.loads((clones.quarantine_dir(PROJECT) / "pm1tl1.g000001.json").read_text())["odd_check"] == "3a"


def test_status_names_a_missing_comparison_commit(lane, monkeypatch):
    _merged(monkeypatch)
    lane["reports"]["value"] = {"result": "missing", "commit": "a" * 40}
    quarantine.cleanup_step()
    line = quarantine.status_lines(PROJECT, lane["home"])[0]
    assert "report did not finish (comparison commit missing)" in line


def test_report_strings_from_the_box_are_quoted_in_the_listing(lane, monkeypatch):
    _merged(monkeypatch)
    lane["reports"]["value"] = dict(FINISHED, metadata=["given\x1b[31m"])
    quarantine.cleanup_step()
    line = quarantine.status_lines(PROJECT, lane["home"])[0]
    assert "\x1b" not in line and '"given\\033[31m"' in line


def test_every_third_time_out_raises_one_alert(lane, monkeypatch):
    raised = []
    monkeypatch.setattr("yggdrasil.state.alert", lambda kind, message, **fields: raised.append((kind, message)))
    for _ in range(4):
        clones.note_timeout(PROJECT, LANE)
    assert len(clones.read(PROJECT, LANE)["timeouts"]) == 4
    assert raised == [("ygg-clone-replaced", "the checks of the clone of lane ygg/alpha-p1/lane/pm1tl1 ran out of "
                       "time 3 times in the last 24 hours, and the clone was replaced 0 times; the old boxes are kept "
                       "in the lane folder")]


def test_the_clone_replaced_alert_is_a_user_alert():
    from yggdrasil import alertkinds
    assert alertkinds.classify("ygg-clone-replaced") == "user"
    assert alertkinds.next_step("ygg-clone-replaced").startswith("Ask your main session why the lane's clone")


# ---------------------------------------------------------------- merge --repo never a box

def test_a_merge_repo_inside_a_box_is_refused_even_through_a_link(lane):
    assert quarantine.inside_box(lane["box"] / "checkout")
    link = lane["tmp"] / "repo-link"
    os.symlink(str(lane["box"] / "checkout"), str(link))
    assert quarantine.inside_box(link)
    assert not quarantine.inside_box(lane["home"])
    with pytest.raises(util.YggError) as caught:
        quarantine.refuse_box_repo(str(link), "alpha")
    assert "--repo must name the home of programme alpha" in caught.value.message


def test_the_box_walk_resolves_links_before_dotdot_and_stops_at_a_box(lane):
    outside = lane["tmp"] / "outside"
    outside.mkdir()
    deep = lane["tmp"] / "deep"
    (deep / "x").mkdir(parents=True)
    os.symlink(str(lane["box"] / "checkout"), str(deep / "x" / "link"))
    assert quarantine.inside_box(str(deep / "x" / "link") + "/..")  # the link's own parent is the box
    os.symlink(str(lane["folder"]), str(outside / "lanes"))
    assert quarantine.inside_box(str(outside / "lanes" / "pm1tl1.g000001" / "nothing" / ".." / ".."))
    assert not quarantine.inside_box(str(outside / "lanes"))
    assert not quarantine.inside_box(str(lane["home"]) + "/./")


def test_another_spelling_of_a_box_is_found_by_its_inode(lane):
    upper = str(lane["folder"] / "PM1TL1.G000001")
    if not os.path.exists(upper):
        pytest.skip("the volume is case-sensitive")
    assert quarantine.inside_box(upper + "/checkout")
