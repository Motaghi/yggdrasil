"""The leftover-demo scan at the start of `ygg demo`: it reports only real, private demo folders, reads their
marker without following symlinks or blocking, and prints the folder quoted for the shell."""

import json
import os
import shlex
import threading

import pytest

from yggdrasil.demo import run


def _leftover(tmp, name="ygg-demo-abc_123", marker=True):
    folder = tmp / name
    folder.mkdir(mode=0o700)
    if marker:
        (folder / "demo-marker.json").write_text(json.dumps({
            "root": os.path.realpath(folder), "driver_pid": os.getpid(), "driver_started": "s:1", "ended": False})
            + "\n")
    return folder


def _scan(tmp, seconds=5.0):
    """left_behind(tmp) in a thread, so a scan that blocks fails the test instead of hanging it."""
    result = {}
    thread = threading.Thread(target=lambda: result.update(found=run.left_behind(str(tmp))), daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "left_behind blocked"
    return result["found"]


def test_a_genuine_leftover_is_still_reported(tmp_path):
    folder = _leftover(tmp_path)
    assert _scan(tmp_path) == [str(folder)]


def test_a_symlinked_entry_is_skipped(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real = _leftover(elsewhere)
    scanned = tmp_path / "scanned"
    scanned.mkdir()
    (scanned / "ygg-demo-link").symlink_to(real, target_is_directory=True)
    assert _scan(scanned) == []


def test_a_fifo_marker_is_skipped_without_hanging(tmp_path):
    folder = _leftover(tmp_path, marker=False)
    os.mkfifo(folder / "demo-marker.json")
    assert _scan(tmp_path) == []


def test_a_fifo_entry_is_skipped_without_hanging(tmp_path):
    os.mkfifo(tmp_path / "ygg-demo-fifo")
    assert _scan(tmp_path) == []


def test_a_symlinked_marker_is_skipped(tmp_path):
    folder = _leftover(tmp_path, marker=False)
    elsewhere = tmp_path / "marker.json"
    elsewhere.write_text(json.dumps({"root": os.path.realpath(folder), "driver_pid": None, "ended": False}) + "\n")
    (folder / "demo-marker.json").symlink_to(elsewhere)
    assert _scan(tmp_path) == []


def test_an_oversized_marker_is_skipped(tmp_path):
    folder = _leftover(tmp_path, marker=False)
    marker = {"root": os.path.realpath(folder), "driver_pid": None, "ended": False,
              "padding": "x" * (run.MARKER_LIMIT + 1)}
    (folder / "demo-marker.json").write_text(json.dumps(marker))
    assert _scan(tmp_path) == []


@pytest.mark.parametrize("name", ["ygg-demo-a b", "ygg-demo-a\x1b[2Jb", "ygg-demo-a;touch x;", "ygg-demo-a\nb",
                                  "ygg-demo-"])
def test_a_name_mkdtemp_would_not_make_is_skipped(tmp_path, name):
    _leftover(tmp_path, name=name)
    assert _scan(tmp_path) == []


def test_a_foreign_owner_entry_is_skipped(tmp_path, monkeypatch):
    _leftover(tmp_path)
    monkeypatch.setattr(run.os, "getuid", lambda: os.stat(tmp_path).st_uid + 1)
    assert _scan(tmp_path) == []


def test_the_leftover_line_quotes_the_folder(tmp_path, monkeypatch, capsys):
    temp = tmp_path / "temp dir"
    temp.mkdir()
    folder = _leftover(temp)

    class Stop(Exception):
        pass

    def no_new_folder(**_):
        raise Stop

    monkeypatch.setattr(run.tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(run.tempfile, "mkdtemp", no_new_folder)
    with pytest.raises(Stop):
        run.Driver(fast=True, keep=True, no_open=True).setup()
    quoted = shlex.quote(str(folder))
    assert quoted != str(folder)
    assert ("demo: an earlier demo was left in " + quoted + "; to stop it and remove it, run: ygg demo --cleanup "
            + quoted) in capsys.readouterr().out.splitlines()


@pytest.mark.parametrize("mode", [0o750, 0o755, 0o770, 0o777, 0o500])
def test_a_folder_whose_mode_cleanup_would_refuse_is_skipped(tmp_path, mode):
    folder = _leftover(tmp_path)
    folder.chmod(mode)
    try:
        assert _scan(tmp_path) == []
    finally:
        folder.chmod(0o700)


def test_a_marker_owned_by_someone_else_is_skipped(tmp_path, monkeypatch):
    folder = _leftover(tmp_path)
    real_fstat = os.fstat
    marker_inode = os.stat(folder / "demo-marker.json").st_ino

    def fstat(descriptor):
        found = real_fstat(descriptor)
        if found.st_ino != marker_inode:
            return found
        values = list(found)
        values[4] = found.st_uid + 1  # st_uid
        return os.stat_result(values)
    monkeypatch.setattr(run.os, "fstat", fstat)
    assert _scan(tmp_path) == []


def _with_pid(folder, pid, started="s:1"):
    (folder / "demo-marker.json").write_text(json.dumps({
        "root": os.path.realpath(folder), "driver_pid": pid, "driver_started": started, "ended": False}) + "\n")


@pytest.mark.parametrize("pid", [None, "123", 12.0, True, 0, 1, -5, 2 ** 31, 3000000000, 10 ** 30, [123]])
def test_a_marker_without_a_valid_driver_pid_is_not_a_demo(tmp_path, pid):
    folder = _leftover(tmp_path)
    _with_pid(folder, pid)
    assert _scan(tmp_path) == []


def test_a_live_driver_is_not_reported(tmp_path):
    from yggdrasil import util
    folder = _leftover(tmp_path)
    _with_pid(folder, os.getpid(), util.process_start(os.getpid()))
    assert _scan(tmp_path) == []


@pytest.mark.parametrize("error", [OSError(5, "I/O error"), OverflowError("pid out of range"), ValueError("bad")])
def test_a_liveness_probe_that_raises_skips_the_folder(tmp_path, monkeypatch, error):
    _leftover(tmp_path)

    def broken(pid, started):
        raise error
    monkeypatch.setattr(run, "same", broken)
    assert _scan(tmp_path) == []


@pytest.mark.parametrize("error", [OSError(5, "I/O error"), OverflowError("pid out of range")])
def test_the_liveness_probe_reads_an_unexpected_kill_error_as_unknown(monkeypatch, error):
    def kill(pid, number):
        raise error
    monkeypatch.setattr(run.os, "kill", kill)
    assert run._alive(4242) is None
    assert run.classify(4242, "s:1") == run.UNKNOWN


def test_an_out_of_range_pid_is_unknown_to_cleanup_not_a_traceback():
    assert run.classify(10 ** 30, "s:1") == run.UNKNOWN
    assert run.classify(3000000000, "s:1") == run.UNKNOWN
