"""v1.1.1 invariant T (trusted code never reads, writes or runs git inside a box): trusted code never follows,
resolves or stats a path inside a box before it has decided the path is not a box, and a relayed call reads
only the files its own request uploaded."""

import os
import stat
from pathlib import Path

import pytest

from yggdrasil import clones, quarantine, util

PROJECT, LANE = "alpha-p1", "pm1tl1"


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    (tmp_path / "state").mkdir()
    return tmp_path


def _box(tmp_path, name="pm1tl1.g000001"):
    box = tmp_path / "home" / ".ygg-worktrees" / PROJECT / "lane" / name
    (box / "checkout" / ".git").mkdir(parents=True)
    info = os.lstat(box)
    return {"kind": "lane", "generation": 1, "box": str(box), "path": str(box / "checkout"),
            "home": str(tmp_path / "home"), "branch": "ygg/alpha-p1/lane/pm1tl1", "state": "ready",
            "box_device": info.st_dev, "box_inode": info.st_ino}


# ---------------------------------------------------------------- clones.open_box

def test_open_box_opens_checkout_without_following_and_stats_git_inside_it(tmp_path, monkeypatch):
    record = _box(tmp_path)
    opened, stats = [], []
    real_open, real_stat = os.open, os.stat

    def tracking_open(path, flags, *args, **kwargs):
        opened.append((str(path), flags, kwargs.get("dir_fd")))
        return real_open(path, flags, *args, **kwargs)

    def tracking_stat(path, *args, **kwargs):
        stats.append((str(path), kwargs.get("dir_fd")))
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "stat", tracking_stat)
    fd = clones.open_box(record)
    os.close(fd)
    assert not any(path == "checkout/.git" for path, _ in stats)
    checkout = [(flags, dir_fd) for path, flags, dir_fd in opened if path == "checkout"]
    assert checkout and checkout[0][1] == fd and checkout[0][0] & os.O_NOFOLLOW and checkout[0][0] & os.O_DIRECTORY
    assert any(path == ".git" and dir_fd not in (None, fd) for path, dir_fd in stats)


@pytest.mark.parametrize("make", ["link", "file"])
def test_open_box_still_finds_an_odd_git_entry(tmp_path, make):
    record = _box(tmp_path)
    git = Path(record["path"]) / ".git"
    git.rmdir()
    if make == "link":
        os.symlink(tmp_path, git)
    else:
        git.write_text("gitdir: /elsewhere\n")
    with pytest.raises(clones.BoxOdd) as caught:
        clones.open_box(record)
    assert caught.value.check == "3a"


# ---------------------------------------------------------------- relayed files (commands)

REQUEST = "a" * 32


def _relayed(monkeypatch, state_root):
    from yggdrasil import relay
    monkeypatch.setenv("YGG_VIA_RELAY", "act-0007")
    monkeypatch.setenv("YGG_RELAY_REQUEST", REQUEST)
    monkeypatch.setenv("YGG_NODE", "pm1tl1")
    monkeypatch.setenv("YGG_ACTIVATION", "act-0007")
    folder = relay.relay_root("act-0007") / "files"
    folder.mkdir(parents=True)
    monkeypatch.setattr("yggdrasil.state.all_nodes", lambda: {"pm1tl1": {"id": "pm1tl1", "programme": "alpha",
                                                                         "role": "tl"}})
    return folder


def _no_filesystem(monkeypatch, outside):
    touched = []
    for module, name in ((os.path, "realpath"), (os, "open"), (os, "stat"), (os, "lstat")):
        real = getattr(module, name)

        def wrapper(path, *args, _real=real, **kwargs):
            if str(path).startswith(str(outside)):
                touched.append(str(path))
            return _real(path, *args, **kwargs)
        monkeypatch.setattr(module, name, wrapper)
    return touched


@pytest.mark.parametrize("reader", ["read_body_file", "managed_input"])
def test_a_relayed_call_refuses_a_file_its_request_did_not_upload_before_touching_it(state_root, monkeypatch,
                                                                                      reader):
    from yggdrasil import commands
    folder = _relayed(monkeypatch, state_root)
    outside = state_root / "agent"
    outside.mkdir()
    (outside / "note.txt").write_text("hi")
    (folder / ("b" * 32 + "-4")).write_text("another request's upload")
    touched = _no_filesystem(monkeypatch, outside)
    for path in (outside / "note.txt", folder / ("b" * 32 + "-4"), folder / (REQUEST + "-x")):
        with pytest.raises(util.YggError) as caught:
            getattr(commands, reader)(str(path))
        assert caught.value.code == util.EXIT_REFUSED and "files its request uploaded" in caught.value.message
    assert touched == []


def test_a_relayed_call_reads_its_own_upload(state_root, monkeypatch):
    from yggdrasil import commands
    folder = _relayed(monkeypatch, state_root)
    (folder / (REQUEST + "-4")).write_text("the body")
    assert commands.read_body_file(str(folder / (REQUEST + "-4"))) == "the body"


# ---------------------------------------------------------------- relayed --repo (cli)

def test_a_relayed_repo_inside_a_box_is_refused_before_it_is_resolved(state_root, monkeypatch):
    from yggdrasil import cli
    record = _box(state_root)
    clones.write(PROJECT, LANE, record)
    repo = str(Path(record["path"]) / "sub")
    resolved = []
    real = os.path.realpath
    monkeypatch.setattr(os.path, "realpath", lambda p, *a, **k: resolved.append(str(p)) or real(p, *a, **k))
    monkeypatch.setenv("YGG_PROGRAMME", "alpha")
    monkeypatch.setattr("yggdrasil.state.all_nodes", lambda: {})
    monkeypatch.setattr("yggdrasil.tracker.programme", lambda name: {"id": name})
    monkeypatch.setattr("yggdrasil.tracker.programme_home", lambda programme: record["home"])
    with pytest.raises(util.YggError) as caught:
        cli._relayed_repo(repo)
    assert "--repo must name the home of programme alpha" in caught.value.message
    assert not any(p.startswith(record["box"]) for p in resolved)


# ---------------------------------------------------------------- lanecheck: registry paths as strings

def test_lanecheck_matches_a_clone_by_its_registry_path_never_by_resolving_inside_the_box(state_root):
    from yggdrasil import lanecheck
    record = _box(state_root)
    elsewhere = state_root / "elsewhere"
    (elsewhere / ".git").mkdir(parents=True)
    checkout = Path(record["path"])
    os.rename(checkout, state_root / "old-checkout")
    os.symlink(elsewhere, checkout)  # an agent made its checkout a link to another repository
    clones.write(PROJECT, LANE, record)
    assert lanecheck._record_in(str(elsewhere), PROJECT, LANE) is None
    found = lanecheck._record_in(str(checkout), PROJECT, LANE)
    assert found is not None and found["_name"] == LANE


def test_lanecheck_resolves_only_the_folders_above_the_box(state_root):
    from yggdrasil import lanecheck
    record = _box(state_root)
    os.symlink(state_root / "home", state_root / "home-link")
    linked = dict(record, box=str(state_root / "home-link" / Path(record["box"]).relative_to(state_root / "home")))
    linked["path"] = linked["box"] + "/checkout"
    clones.write(PROJECT, LANE, linked)
    found = lanecheck._record_in(os.path.realpath(record["path"]), PROJECT, LANE)
    assert found is not None and found["_name"] == LANE


# ---------------------------------------------------------------- t3: superseded clone paths

def test_t3_area_keeps_a_superseded_clone_path_as_text(state_root, monkeypatch):
    from yggdrasil import t3
    old = _box(state_root)
    new = _box(state_root, "pm1tl1.g000002")
    clones.write(PROJECT, LANE, dict(new, generation=2))
    util.ensure_dir(clones.quarantine_dir(PROJECT))
    util.write_json(clones.quarantine_dir(PROJECT) / "pm1tl1.g000001.json",
                    {"lane": old["branch"], "project": PROJECT, "team_lead": LANE, "box": "pm1tl1.g000001",
                     "path": old["box"], "generation": 1})
    os.rename(old["path"], state_root / "moved")
    os.symlink(state_root / "moved", old["path"])
    resolved = []
    real = os.path.realpath
    monkeypatch.setattr(os.path, "realpath", lambda p, *a, **k: resolved.append(str(p)) or real(p, *a, **k))
    assert t3._area(old["path"]) == os.path.normpath(old["path"])
    assert not any(p.startswith(old["box"] + "/") for p in resolved)


# ---------------------------------------------------------------- quarantine.inside_box: a relative path

def test_inside_box_checks_the_working_folder_before_resolving_it(state_root, monkeypatch):
    record = _box(state_root)
    clones.write(PROJECT, LANE, record)
    inner = Path(record["path"]) / "sub"
    inner.mkdir()
    monkeypatch.chdir(inner)
    resolved = []
    real = os.path.realpath
    monkeypatch.setattr(os.path, "realpath", lambda p, *a, **k: resolved.append(str(p)) or real(p, *a, **k))
    assert quarantine.inside_box("x")
    assert not any(p.startswith(record["box"]) for p in resolved)
    assert not stat.S_ISLNK(os.lstat(record["box"]).st_mode)


def test_lanecheck_finds_a_clone_spelled_with_other_letter_case(state_root):
    from yggdrasil import lanecheck
    record = _box(state_root)
    upper = str(state_root / "HOME") + record["path"][len(str(state_root / "home")):]
    if not os.path.isdir(upper):
        pytest.skip("case-sensitive volume")
    clones.write(PROJECT, LANE, dict(record, path=upper, box=upper[:-len("/checkout")]))
    found = lanecheck._record_in(record["path"], PROJECT, LANE)
    assert found is not None and found["_name"] == LANE
