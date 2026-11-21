"""`--body-file` for `ygg tracker write` and `ygg send` (release pilots 2 and 3, 20 Nov 2026): a multi-line body
is written to a file and named, because the guard refuses quoted newlines and `$(cat ...)`."""

import os
import subprocess
from pathlib import Path

import pytest
from test_guard_reads_and_hook_skips import _pretool, agent  # noqa: F401  (shared fixture)
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)

from yggdrasil import util

ROLES = Path(__file__).resolve().parents[2] / "src" / "yggdrasil" / "data" / "roles"


def _work_area():
    from yggdrasil import launcher, state
    nodes = state.all_nodes()
    area = launcher.work_area(nodes["tl1"], nodes)
    area.mkdir(parents=True, exist_ok=True)
    return area


# ---------------------------------------------------------------- the path rule

def test_a_managed_caller_may_name_a_body_file_in_its_own_work_area(world, monkeypatch):
    from yggdrasil import commands
    area = _work_area()
    note = area / ".ygg" / "notes" / "checkpoint.md"
    note.parent.mkdir(parents=True)
    note.write_text("line one\nline two\n")
    monkeypatch.setenv("YGG_NODE", "tl1")
    assert commands.read_body_file(str(note)) == "line one\nline two\n"


def test_a_body_file_outside_the_tracker_and_work_area_is_refused(world, monkeypatch, tmp_path):
    from yggdrasil import commands
    outside = tmp_path / "elsewhere.md"
    outside.write_text("secret\n")
    monkeypatch.setenv("YGG_NODE", "tl1")
    with pytest.raises(util.YggError) as error:
        commands.read_body_file(str(outside))
    assert error.value.code == util.EXIT_REFUSED
    assert "body file must be inside your worktree" in error.value.message


def test_a_symbolic_link_in_the_work_area_pointing_outside_is_refused(world, monkeypatch, tmp_path):
    from yggdrasil import commands
    outside = tmp_path / "elsewhere.md"
    outside.write_text("secret\n")
    link = _work_area() / "note.md"
    os.symlink(outside, link)
    monkeypatch.setenv("YGG_NODE", "tl1")
    with pytest.raises(util.YggError) as error:
        commands.read_body_file(str(link))
    assert "body file must be inside your worktree" in error.value.message


@pytest.mark.parametrize("content,text", [(b"\xff\xfe\x00bad", "not UTF-8"), (b"  \n", "empty")])
def test_a_body_file_that_isnt_text_or_is_empty_is_refused(world, content, text, tmp_path):
    from yggdrasil import commands
    path = tmp_path / "body.md"
    path.write_bytes(content)
    with pytest.raises(util.YggError) as error:
        commands.read_body_file(str(path))
    assert error.value.code == util.EXIT_USAGE and text in error.value.message


def test_a_missing_body_file_is_refused(world, tmp_path):
    from yggdrasil import commands
    with pytest.raises(util.YggError) as error:
        commands.read_body_file(str(tmp_path / "missing.md"))
    assert "body file not found" in error.value.message


# ---------------------------------------------------------------- the verbs

def test_tracker_write_records_a_multi_line_body_from_a_file(world, tmp_path):
    from yggdrasil import cli, tracker
    path = tmp_path / "brief.md"
    path.write_text("# Brief\n\nFirst line.\nSecond line.\n")
    assert cli.main(["tracker", "write", "--as", "pm1", "--project", "p1", "--kind", "brief",
                     "--body-file", str(path)]) == 0
    assert "First line.\nSecond line." in tracker.read_document("p1", "brief")


@pytest.mark.parametrize("extra", [["--body", "x", "--body-file", "f.md"], []])
def test_tracker_write_takes_exactly_one_of_body_and_body_file(world, extra):
    from yggdrasil import cli
    argv = ["tracker", "write", "--as", "pm1", "--project", "p1", "--kind", "brief", *extra]
    assert cli.main(argv) == util.EXIT_USAGE  # the parser's error is a usage error


def test_send_takes_its_body_from_a_file_too(world, tmp_path):
    from yggdrasil import cli, state
    path = tmp_path / "message.md"
    path.write_text("Please re-read the brief.\nThen carry on.\n")
    assert cli.main(["send", "--from", "pm1", "--to", "tl1", "--kind", "work", "--body-file", str(path)]) == 0
    bodies = [m.get("body") for m in state.inbox("tl1")]
    assert any("Then carry on." in str(body) for body in bodies)


# ---------------------------------------------------------------- the guard

def test_the_guard_allows_a_body_file_and_still_refuses_a_quoted_newline(agent):
    code, message = _pretool("ygg tracker write --as tl1 --project p1 --package WP-1 --kind checkpoint "
                             "--body-file .ygg/notes/checkpoint.md", agent)
    assert (code, message) == (0, "")
    code, _ = _pretool('ygg tracker write --as tl1 --project p1 --kind checkpoint --body "one\ntwo"', agent)
    assert code == 2


# ---------------------------------------------------------------- notes are never committed

def test_the_notes_folder_is_excluded_from_every_worktree(tmp_path):
    from yggdrasil import launcher
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["git", "-C", str(home), "init", "-q", "-b", "main"], check=True)
    launcher.exclude_worktrees(str(home))
    launcher.exclude_worktrees(str(home))
    lines = (home / ".git" / "info" / "exclude").read_text().splitlines()
    assert lines.count("/.ygg-worktrees/") == 1 and lines.count("/.ygg/") == 1
    (home / ".ygg" / "notes").mkdir(parents=True)
    (home / ".ygg" / "notes" / "pid.md").write_text("plan\n")
    status = subprocess.run(["git", "-C", str(home), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert ".ygg" not in status


# ---------------------------------------------------------------- the role prompts

def test_role_prompts_send_multi_line_bodies_through_a_file():
    pm = (ROLES / "pm.md").read_text()
    assert "--kind pid --body-file .ygg/notes/pid.md" in pm and "--kind pid --body <" not in pm
    assert "--body-file .ygg/notes/WP-<n>.md" in pm and "--kind work-package --body <" not in pm
    assert "--body ready" in pm  # one-liners keep --body
    assert "--body-file .ygg/notes/checkpoint.md" in (ROLES / "tl.md").read_text()
    shared = (ROLES / "shared.md").read_text()
    assert "--body-file .ygg/notes/<name>.md" in shared and "never committed" in shared


def test_a_managed_caller_may_not_send_a_tracker_file_as_a_body(world, monkeypatch):
    """The tracker holds other projects' and programmes' records: a body is sent on, so naming one would disclose
    it (security review, 20 Nov 2026). Only the caller's own work area counts."""
    from yggdrasil import commands, tracker
    commands.project_create("p2", "alpha", "pm2")
    other = tracker.project_dir("p2") / "project.json"
    assert other.is_file()
    monkeypatch.setenv("YGG_NODE", "tl1")
    with pytest.raises(util.YggError) as error:
        commands.read_body_file(str(other))
    assert error.value.code == util.EXIT_REFUSED and "body file must be inside your worktree" in error.value.message


def test_the_user_may_name_any_readable_file(world, tmp_path):
    from yggdrasil import commands, tracker
    elsewhere = tmp_path / "mine.md"
    elsewhere.write_text("the user's own text\n")
    assert commands.read_body_file(str(elsewhere)) == "the user's own text\n"
    assert commands.read_body_file(str(tracker.project_dir("p1") / "project.json"))


# ---------------------------------------------------------------- review v1-bodyfile2

def test_a_file_replaced_by_a_link_after_the_check_is_refused(world, monkeypatch, tmp_path):
    from yggdrasil import commands
    secret = tmp_path / "secret.md"
    secret.write_text("secret\n")
    note = _work_area() / "note.md"
    note.write_text("checked\n")
    real_open = os.open

    def swapping_open(path, flags, *args):
        if str(path).endswith("note.md"):
            os.unlink(note)
            os.symlink(secret, note)  # the race: the checked file becomes a link to elsewhere
        return real_open(path, flags, *args)

    monkeypatch.setenv("YGG_NODE", "tl1")
    monkeypatch.setattr(os, "open", swapping_open)
    with pytest.raises(util.YggError) as error:
        commands.read_body_file(str(note))
    assert "body file must be inside your worktree" in error.value.message


def test_a_folder_replaced_by_a_link_after_the_check_is_refused(world, monkeypatch, tmp_path):
    from yggdrasil import commands
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "note.md").write_text("secret\n")
    notes = _work_area() / "notes"
    notes.mkdir()
    (notes / "note.md").write_text("checked\n")
    real_open = os.open

    def swapping_open(path, flags, *args):
        if str(path).endswith("note.md"):
            os.rename(notes, notes.with_name("notes-moved"))
            os.symlink(elsewhere, notes)  # the race: the checked folder becomes a link to elsewhere
        return real_open(path, flags, *args)

    monkeypatch.setenv("YGG_NODE", "tl1")
    monkeypatch.setattr(os, "open", swapping_open)
    with pytest.raises(util.YggError) as error:
        commands.read_body_file(str(notes / "note.md"))
    assert error.value.message.endswith(commands.BODY_FILE_CHANGED)


def test_a_managed_session_without_a_node_is_refused(world, monkeypatch, tmp_path):
    from yggdrasil import commands
    path = tmp_path / "body.md"
    path.write_text("text\n")
    monkeypatch.delenv("YGG_NODE", raising=False)
    monkeypatch.setenv("YGG_ACTIVATION", "act-0001")
    with pytest.raises(util.YggError) as error:
        commands.read_body_file(str(path))
    assert error.value.code == util.EXIT_REFUSED and "no node identity" in error.value.message


def test_a_tracker_inside_the_work_area_is_still_refused(world, monkeypatch):
    from yggdrasil import commands
    inner = _work_area() / "tracker-copy"
    (inner / "projects" / "p2").mkdir(parents=True)
    record = inner / "projects" / "p2" / "project.json"
    record.write_text('{"id": "p2"}\n')
    monkeypatch.setattr(commands, "_tracker_root", lambda: inner)  # nodes still read the real tracker
    monkeypatch.setenv("YGG_NODE", "tl1")
    with pytest.raises(util.YggError) as error:
        commands.read_body_file(str(record))
    assert error.value.code == util.EXIT_REFUSED and commands.BODY_FILE_TRACKER in error.value.message


# ---------------------------------------------------------------- security review on 117a33b

def test_a_hard_link_to_a_tracker_file_inside_the_work_area_is_refused(world, monkeypatch):
    from yggdrasil import commands, tracker
    record = tracker.project_dir("p1") / "project.json"
    link = _work_area() / "note.md"
    os.link(record, link)
    monkeypatch.setenv("YGG_NODE", "tl1")
    with pytest.raises(util.YggError) as error:
        commands.read_body_file(str(link))
    assert error.value.code == util.EXIT_REFUSED and commands.BODY_FILE_LINKS in error.value.message


def test_a_tracker_spelled_with_different_case_is_still_refused(world, monkeypatch):
    from yggdrasil import commands
    inner = _work_area() / "tracker-copy"
    (inner / "projects" / "p2").mkdir(parents=True)
    (inner / "projects" / "p2" / "project.json").write_text('{"id": "p2"}\n')
    spelled = _work_area() / "TRACKER-COPY" / "projects" / "p2" / "project.json"
    if not spelled.exists():
        pytest.skip("the test folder's filesystem is case-sensitive")
    monkeypatch.setattr(commands, "_tracker_root", lambda: inner)  # nodes still read the real tracker
    monkeypatch.setenv("YGG_NODE", "tl1")
    with pytest.raises(util.YggError) as error:
        commands.read_body_file(str(spelled))
    assert error.value.code == util.EXIT_REFUSED and commands.BODY_FILE_TRACKER in error.value.message



def test_a_hard_link_is_refused_for_the_user_too(world, tmp_path):
    from yggdrasil import commands
    original = tmp_path / "original.md"
    original.write_text("text\n")
    linked = tmp_path / "linked.md"
    os.link(original, linked)
    with pytest.raises(util.YggError) as error:
        commands.read_body_file(str(linked))
    assert commands.BODY_FILE_LINKS in error.value.message
