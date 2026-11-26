"""v1.1.1: commit-time checks in a registered lane clone.

util.git_env gains `home_objects` and a write-free mode; lanecheck maps a registered clone to its home through
the registry; the hook calls that need a commit only the home holds read the home's objects for one git
process, the others never do; every hook verb gives the same verdict with the state, safety and tracker roots
read-only."""

import json
import os
import stat
import subprocess
import sys

import pytest

from yggdrasil import lanecheck, util

IDENT = ("-c", "user.name=Example Builder", "-c", "user.email=builder@example.invalid", "-c", "core.hooksPath=/dev/null")
LANE = "ygg/alpha-p1/lane/pm1tl1"


def _git(repo, *args, env=None, input=None):
    return subprocess.run(["git", "-C", str(repo), *IDENT, *args], capture_output=True, text=True, check=True,
                          env=env, input=input).stdout.strip()


def _commit(repo, name, text, message):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _assemble(home, box, head):
    """A box the way assembly makes one (spec "Lane clones" step 3): no alternates, one pack of the lane's history."""
    box.mkdir(mode=0o700)
    (box / ".git").mkdir(mode=0o500)
    (box / "tmp" / "cache").mkdir(parents=True)
    checkout = box / "checkout"
    subprocess.run(["git", "init", "-q", "--template=", str(checkout)], check=True)
    pack = subprocess.run(["git", "-C", str(home), "pack-objects", "-q", "--revs", "--stdout", "--delta-base-offset"],
                          input=(head + "\n").encode(), capture_output=True, check=True).stdout
    subprocess.run(["git", "-C", str(checkout), "index-pack", "--strict", "--stdin"], input=pack,
                   capture_output=True, check=True)
    _git(checkout, "update-ref", "refs/heads/" + LANE, head)
    _git(checkout, "update-ref", "refs/ygg/given", head)
    _git(checkout, "symbolic-ref", "HEAD", "refs/heads/" + LANE)
    _git(checkout, "read-tree", "-m", "-u", "HEAD")
    return checkout


@pytest.fixture
def tree(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setenv("YGG_STATE_ROOT", str(state))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "files"))
    (tmp_path / "files").mkdir()
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    monkeypatch.delenv("YGG_RELAY", raising=False)
    monkeypatch.delenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    base = _commit(home, "README.md", "x\n", "L0")
    _git(home, "branch", LANE, base)
    newer_main = _commit(home, "m.txt", "main\n", "newer main")
    box = home / ".ygg-worktrees" / "alpha-p1" / "lane" / "pm1tl1.g000001"
    box.parent.mkdir(parents=True)
    checkout = _assemble(home, box, base)
    folder = state / "lane-clones" / "alpha-p1"
    folder.mkdir(parents=True)
    record = {"kind": "lane", "generation": 1, "box": str(box), "path": str(checkout), "home": str(home),
              "branch": LANE, "state": "ready", "given_head": base, "given_ref": base}
    (folder / "pm1tl1.json").write_text(json.dumps(record))
    lanecheck._CLONES.clear()
    yield {"home": home, "box": box, "clone": checkout, "base": base, "main": newer_main, "state": state,
           "tmp": tmp_path}
    lanecheck._CLONES.clear()


# ---------------------------------------------------------------- util.git_env

def test_git_env_drops_an_inherited_alternates_value(monkeypatch):
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/elsewhere/objects")
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in util.git_env()


def test_git_env_home_objects_carries_exactly_one_path(monkeypatch):
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/elsewhere/objects:/more")
    assert util.git_env(home_objects="/h/.git/objects")["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == "/h/.git/objects"


def test_git_env_home_objects_is_c_quoted_when_needed():
    assert util.git_env(home_objects="/a:b/.git/objects")["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == '"/a:b/.git/objects"'
    assert util.git_env(home_objects='/a"b/o')["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == '"/a\\"b/o"'
    with pytest.raises(ValueError):
        util.git_env(home_objects="relative/objects")


def test_c_quoted_path_is_read_by_git(tmp_path):
    home = tmp_path / "a:b"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    sha = _commit(home, "f.txt", "f\n", "f")
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    found = subprocess.run(["git", "-C", str(other), "cat-file", "-e", sha + "^{commit}"],
                           env=util.git_env(home_objects=str(home / ".git" / "objects")))
    assert found.returncode == 0
    assert not (other / ".git" / "objects" / "info" / "alternates").exists()


def test_write_free_git_env_never_writes_the_global_config(tmp_path, monkeypatch):
    state = tmp_path / "s"
    monkeypatch.setenv("YGG_STATE_ROOT", str(state))
    monkeypatch.setenv("YGG_RELAY", str(tmp_path / "relay"))
    assert util.git_env()["GIT_CONFIG_GLOBAL"] == os.devnull
    assert not state.exists()
    (state / "git").mkdir(parents=True)
    (state / "git" / "global.gitconfig").write_text("")
    assert util.git_env()["GIT_CONFIG_GLOBAL"] == str(state / "git" / "global.gitconfig")


def test_write_free_switch_for_the_tender(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "s"))
    monkeypatch.delenv("YGG_RELAY", raising=False)
    util.write_free(True)
    try:
        assert util.git_env()["GIT_CONFIG_GLOBAL"] == os.devnull
        assert not (tmp_path / "s").exists()
    finally:
        util.write_free(False)


def test_without_relay_git_env_is_as_before(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "s"))
    monkeypatch.delenv("YGG_RELAY", raising=False)
    assert util.git_env()["GIT_CONFIG_GLOBAL"] == str(tmp_path / "s" / "git" / "global.gitconfig")


# ---------------------------------------------------------------- the clone mapping

def test_where_maps_a_registered_clone_to_its_home(tree):
    root, project, lane = lanecheck._where(str(tree["clone"]))
    assert (project, lane) == ("alpha-p1", "pm1tl1")
    assert lanecheck.clone_home(root) == str(tree["home"])
    assert lanecheck._objects(root) == str(tree["home"] / ".git" / "objects")
    assert lanecheck._main_ref(root) == tree["main"]  # main is read in the home, which the clone lacks


def test_a_detached_clone_is_still_mapped(tree, monkeypatch):
    monkeypatch.setenv("YGG_PROJECT", "alpha-p1")  # an agent's pinned identity names its project
    _git(tree["clone"], "checkout", "-q", "--detach")
    root, project, lane = lanecheck._where(str(tree["clone"]))
    assert lane == "pm1tl1" and lanecheck.clone_home(root) == str(tree["home"])


def test_a_worktree_lane_is_not_mapped(tree):
    worktree = tree["tmp"] / "wt"
    _git(tree["home"], "worktree", "add", "-q", str(worktree), LANE)
    root, project, lane = lanecheck._where(str(worktree))
    assert (project, lane) == ("alpha-p1", "pm1tl1")
    assert lanecheck.clone_home(root) is None and lanecheck._CLONES == {}


def test_a_closed_record_does_not_map(tree):
    path = tree["state"] / "lane-clones" / "alpha-p1" / "pm1tl1.json"
    path.write_text(json.dumps(dict(json.loads(path.read_text()), state="closed")))
    root, _, _ = lanecheck._where(str(tree["clone"]))
    assert lanecheck.clone_home(root) is None


def _recording(monkeypatch):
    calls = []
    real = subprocess.run

    def run(argv, *args, **kwargs):
        env = kwargs.get("env") or {}
        calls.append((list(argv), env.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")))
        return real(argv, *args, **kwargs)
    monkeypatch.setattr(lanecheck.subprocess, "run", run)
    return calls


def _sub(argv):
    words = [w for w in argv[1:] if not w.startswith("-") and "=" not in w]
    return argv[argv.index("-C") + 2] if "-C" in argv else words[0]


def test_yes_calls_carry_home_objects_and_no_calls_do_not(tree, monkeypatch):
    calls = _recording(monkeypatch)
    root, _, _ = lanecheck._where(str(tree["clone"]))
    objects = str(tree["home"] / ".git" / "objects")
    assert all(alt is None for _, alt in calls)  # rev-parse --show-toplevel and symbolic-ref: the clone's own state
    del calls[:]
    assert lanecheck._ok("-C", root, "cat-file", "-e", tree["main"] + "^{commit}", objects=True)  # a "yes" call
    assert calls[-1][1] == objects
    assert not lanecheck._ok("-C", root, "cat-file", "-e", tree["main"] + "^{commit}")  # the clone alone lacks it
    assert calls[-1][1] is None
    del calls[:]
    lanecheck.home_hooks_path(lanecheck._refs(root))
    lanecheck._git("-C", root, "diff", "--cached", "--raw", "-z")
    assert calls and all(alt is None for _, alt in calls)
    del calls[:]
    assert lanecheck._is_ancestor(root, tree["base"], tree["main"])
    assert calls[-1][1] == objects


def test_yes_calls_in_the_home_never_carry_the_variable(tree, monkeypatch):
    calls = _recording(monkeypatch)
    lanecheck._where(str(tree["clone"]))
    del calls[:]
    assert lanecheck._is_ancestor(str(tree["home"]), tree["base"], tree["main"])
    assert calls[-1][1] is None


def test_freeze_reads_another_lanes_accepted_commit_and_writes_no_alternates(tree, monkeypatch):
    """CB-49's "yes" call: an accepted commit the clone does not hold still freezes its file at pre-commit."""
    home = tree["home"]
    _git(home, "checkout", "-q", "-b", "ygg/alpha-p1/lane/pm1tl2", tree["base"])
    accepted = _commit(home, "shared.txt", "two\n", "WP-3")
    _git(home, "checkout", "-q", "main")

    class Issue:
        id, records = "WP-3", []

        def get(self, key, default=None):
            return {"state": "accepted", "lane": "pm1tl2"}.get(key, default)
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [Issue()])
    monkeypatch.setattr(lanecheck, "_accepted_sha", lambda issue: accepted)
    monkeypatch.setattr(lanecheck, "denylist", lambda: [])
    monkeypatch.setattr(lanecheck, "_repo_entry", lambda project, repo: None)
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: {})
    clone = tree["clone"]
    (clone / "shared.txt").write_text("one\n")
    _git(clone, "add", "shared.txt")
    problems = lanecheck.staged_problems(str(clone))
    assert lanecheck.frozen_message("shared.txt", "WP-3") in problems
    assert not (clone / ".git" / "objects" / "info" / "alternates").exists()
    assert subprocess.run(["git", "-C", str(clone), "cat-file", "-e", accepted]).returncode != 0


def test_reference_transaction_in_a_clone_checks_added_commits(tree, monkeypatch):
    monkeypatch.setattr(lanecheck, "denylist", lambda: ["zebra-orchid-7"])
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    monkeypatch.setattr(lanecheck, "_repo_entry", lambda project, repo: None)
    clone = tree["clone"]
    old = _git(clone, "rev-parse", "HEAD")
    new = _commit(clone, "a.txt", "zebra-orchid-7\n", "x")
    problems = lanecheck.transaction_problems(old + " " + new + " refs/heads/" + LANE + "\n", str(clone))
    assert any(new[:12] in p and "zebra-orchid-7" in p for p in problems), problems


def test_reference_transaction_in_a_worktree_lane_is_as_before(tree, monkeypatch):
    monkeypatch.setattr(lanecheck, "denylist", lambda: ["zebra-orchid-7"])
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    worktree = tree["tmp"] / "wt"
    _git(tree["home"], "worktree", "add", "-q", str(worktree), LANE)
    old = _git(worktree, "rev-parse", "HEAD")
    new = _commit(worktree, "a.txt", "zebra-orchid-7\n", "x")
    assert lanecheck.transaction_problems(old + " " + new + " refs/heads/" + LANE + "\n", str(worktree)) == []


def test_unread_mail_refuses_pre_commit_in_a_clone_only(tree, monkeypatch):
    monkeypatch.setenv("YGG_NODE", "pm1tl1")
    monkeypatch.delenv("YGG_ACTIVATION", raising=False)
    inbox = tree["state"] / "inboxes"
    inbox.mkdir()
    (inbox / "pm1tl1.json").write_text(json.dumps([{"msg_id": "m-0011"}]))
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    root, _, _ = lanecheck._where(str(tree["clone"]))
    assert lanecheck.clone_unread_refusal(root) == "read your inbox first (1 unread): ygg inbox pm1tl1"
    assert lanecheck.staged_problems(str(tree["clone"])) == ["read your inbox first (1 unread): ygg inbox pm1tl1"]
    worktree = tree["tmp"] / "wt"
    _git(tree["home"], "worktree", "add", "-q", str(worktree), LANE)
    assert lanecheck.clone_unread_refusal(str(worktree)) is None


# ---------------------------------------------------------------- every hook verb with read-only roots

def _hook(clone, env, *args, stdin=""):
    result = subprocess.run([sys.executable, "-m", "yggdrasil", "lane-check", *args], cwd=str(clone), env=env,
                            capture_output=True, text=True, input=stdin, timeout=60)
    return result.returncode, result.stderr


def _listing(root):
    found = []
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(folder, name)
            found.append((os.path.relpath(path, root), os.lstat(path).st_mtime_ns))
    return sorted(found)


def test_every_hook_verb_gives_the_same_verdict_with_read_only_roots(tree):
    state, clone = tree["state"], tree["clone"]
    safety = state.parent / (state.name + ".safety")
    safety.mkdir(mode=0o700)
    files = tree["tmp"] / "files"
    (state / "git").mkdir()
    (state / "git" / "global.gitconfig").write_text("")
    relay = tree["tmp"] / "relay"
    (relay / "in").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("YGG_", "GIT_"))}
    env.update(YGG_STATE_ROOT=str(state), YGG_FILES_ROOT=str(files), YGG_RELAY=str(relay), YGG_NODE="pm1tl1",
               YGG_PROJECT="alpha-p1", PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    message = tree["tmp"] / "msg"
    message.write_text("x\n")
    (clone / "a.txt").write_text("sk-ant-" + "a" * 20 + "\n")
    _git(clone, "add", "a.txt")
    head = _git(clone, "rev-parse", "HEAD")
    transaction = head + " " + "1" * 40 + " refs/heads/" + LANE + "\n"
    verbs = [(("pre-commit",), ""), (("pre-merge-commit",), ""), (("commit-msg", str(message)), ""),
             (("reference-transaction", "prepared"), transaction),
             (("pre-push", "origin", str(tree["home"])), "refs/heads/x " + head + " refs/heads/x " + "0" * 40 + "\n")]
    writable = [_hook(clone, env, *args, stdin=stdin) for args, stdin in verbs]
    before = {root: _listing(root) for root in (state, safety, files)}
    modes = {}
    for root in (state, safety, files):
        for folder, dirs, names in os.walk(root):
            for name in [folder] + [os.path.join(folder, n) for n in dirs + names]:
                modes[name] = os.lstat(name).st_mode
    try:
        for path, mode in modes.items():
            os.chmod(path, stat.S_IMODE(mode) & ~0o222)
        read_only = [_hook(clone, env, *args, stdin=stdin) for args, stdin in verbs]
    finally:
        for path, mode in sorted(modes.items(), key=lambda item: -len(item[0])):
            os.chmod(path, stat.S_IMODE(mode))
    assert read_only == writable
    assert writable[0][0] == 1 and "secret-shaped" in writable[0][1]
    assert writable[2][0] == 0
    assert {root: _listing(root) for root in (state, safety, files)} == before


def test_unread_mail_refuses_pre_push_in_a_clone_with_the_guards_text(tree, monkeypatch, capsys):
    monkeypatch.setenv("YGG_NODE", "pm1tl1")
    monkeypatch.delenv("YGG_ACTIVATION", raising=False)
    inbox = tree["state"] / "inboxes"
    inbox.mkdir()
    (inbox / "pm1tl1.json").write_text(json.dumps([{"msg_id": "m-0011"}, {"msg_id": "m-0012", "acked_at": "x"}]))
    monkeypatch.chdir(tree["clone"])
    monkeypatch.setattr(lanecheck.sys, "stdin", __import__("io").StringIO(""))
    assert lanecheck.main(["pre-push", "origin", str(tree["home"])]) == 1
    assert "DENIED: read your inbox first (1 unread): ygg inbox pm1tl1" in capsys.readouterr().err


def test_a_clone_with_no_unread_mail_is_not_refused_for_it(tree, monkeypatch):
    monkeypatch.setenv("YGG_NODE", "pm1tl1")
    inbox = tree["state"] / "inboxes"
    inbox.mkdir()
    (inbox / "pm1tl1.json").write_text(json.dumps([{"msg_id": "m-0011", "acked_at": "2026-11-24T00:00:00Z"}]))
    root, _, _ = lanecheck._where(str(tree["clone"]))
    assert lanecheck.clone_unread_refusal(root) is None
