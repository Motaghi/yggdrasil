"""v1.1 unit tests that are not acceptance criteria: version sources, the demo driver's isolation before its
first child, the merge-check start identity, and teardown's identity rule."""

import importlib.metadata
import inspect
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

import pytest

import yggdrasil
from yggdrasil import cli, mergecheck, util
from yggdrasil.demo import run

ROOT = Path(__file__).resolve().parents[2]


def test_version_sources_agree(capsys):
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert pyproject == yggdrasil.__version__ == importlib.metadata.version("yggdrasil") == "1.1.1"
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out == "ygg 1.1.1\n"


def test_demo_driver_imports_no_config_state_or_tracker():
    probe = ("import sys, yggdrasil.demo.run, yggdrasil.demo.agent; "
             "print(' '.join(sorted(m for m in sys.modules if m.startswith('yggdrasil'))))")
    loaded = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True,
                            check=True).stdout.split()
    for name in ("yggdrasil.config", "yggdrasil.state", "yggdrasil.tracker", "yggdrasil.plugins"):
        assert name not in loaded


class FirstChild(BaseException):
    pass


def test_demo_calls_no_config_state_or_tracker_function_before_its_first_child(tmp_path, monkeypatch):
    from yggdrasil import config, plugins, state, tracker
    # The user-only guard (state.check_user_verb) is the one state call the spec allows; it reads the
    # environment and the working folder only.
    guard = {"check_user_verb", "managed_session", "inside_managed_worktree"}
    calls = []

    def forbidden(name):
        def call(*args, **kwargs):
            calls.append(name)
            raise AssertionError("the demo driver called " + name)
        return call
    for module in (config, state, tracker, plugins):
        for name, value in list(vars(module).items()):
            if inspect.isfunction(value) and value.__module__ == module.__name__ and name not in guard:
                monkeypatch.setattr(module, name, forbidden(module.__name__ + "." + name))
    for name in ("state_root", "files_root"):
        monkeypatch.setattr(util, name, forbidden("util." + name))
    for name in ("YGG_NODE", "YGG_ACTIVATION", "YGG_TEST_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    started = []

    def first_child(self, argv, **kwargs):
        started.append(argv)
        raise FirstChild()
    monkeypatch.setattr(run.Driver, "start", first_child)
    monkeypatch.setattr(run.signal, "signal", lambda number, handler: None)  # keep pytest's own handlers
    with pytest.raises(FirstChild):
        cli.main(["demo", "--fast", "--no-open"])
    assert calls == []
    assert started[0][:2] == ["git", "init"]
    root = next(tmp_path.glob("ygg-demo-*"))
    env = json.loads((root / "demo-env.json").read_text())
    assert env["HOME"] == str(root / "userhome") and env["YGG_CONFIG"] == str(root / "config.yaml")
    assert env["PATH"].split(os.pathsep)[0] == str(root / "bin")


def _sleeper():
    process = subprocess.Popen(["sleep", "60"], start_new_session=True)
    for _ in range(50):
        if util.process_start(process.pid):
            break
        time.sleep(0.05)
    return process


def test_running_merge_check_record_carries_the_job_start_identity(monkeypatch):
    real_popen = subprocess.Popen
    jobs = []

    def popen(argv, **kwargs):
        job = real_popen(["sleep", "60"], stdin=subprocess.DEVNULL, start_new_session=True)
        jobs.append(job)
        return job
    monkeypatch.setattr(mergecheck.subprocess, "Popen", popen)
    try:
        data = mergecheck._start("alpha-p1", "/repo", ("a" * 40, "b" * 40), {})
        assert data["state"] == "running" and data["pid"] == jobs[0].pid
        assert data["process_started_at"] == util.process_start(jobs[0].pid)
        assert data["process_started_at"].startswith("s:")
        published = mergecheck.record("alpha-p1")
        assert published["pid"] == jobs[0].pid and published["process_started_at"] == data["process_started_at"]
    finally:
        for job in jobs:
            job.kill()
            job.wait()


def test_a_start_that_lost_its_claim_starts_no_job(monkeypatch):
    started = []
    monkeypatch.setattr(mergecheck.subprocess, "Popen", lambda *a, **k: started.append(a))
    real_save = mergecheck.save_if_owner

    def claim_then_lose(project, expected, data, **kwargs):
        done = real_save(project, expected, data, **kwargs)
        if data.get("state") == "starting":
            mergecheck.save_record(project, {"id": "alpha-p1-newer", "state": "running"})  # a newer claim
        return done
    monkeypatch.setattr(mergecheck, "save_if_owner", claim_then_lose)
    assert mergecheck._start("alpha-p1", "/repo", ("a" * 40, "b" * 40), {}) is None
    assert started == []


def test_a_job_works_only_once_its_record_names_it(monkeypatch):
    monkeypatch.setattr(mergecheck, "JOB_RECORD_SECONDS", 0.3)
    mergecheck.save_record("alpha-p1", {"id": "alpha-p1-x", "state": "starting"})
    assert mergecheck._named_by_record("alpha-p1", "alpha-p1-x") is False  # never named: gives up
    mergecheck.save_record("alpha-p1", {"id": "alpha-p1-y", "state": "running", "pid": os.getpid()})
    assert mergecheck._named_by_record("alpha-p1", "alpha-p1-x") is False  # superseded
    mergecheck.save_record("alpha-p1", {"id": "alpha-p1-x", "state": "running", "pid": os.getpid()})
    assert mergecheck._named_by_record("alpha-p1", "alpha-p1-x") is True
    ran = []
    monkeypatch.setattr(mergecheck, "run_check", lambda *a: ran.append(a))
    mergecheck.save_record("alpha-p1", {"id": "alpha-p1-z", "state": "starting"})
    assert mergecheck.job_main(["alpha-p1", "/repo", "a" * 40, "b" * 40, "alpha-p1-x"]) == 0
    assert ran == [] and not mergecheck.result_path("alpha-p1-x").exists()


@pytest.mark.parametrize("outcome", ["passed", "failed"])
def test_finished_merge_check_record_drops_pid_and_identity(monkeypatch, outcome):
    saved = []
    monkeypatch.setattr(mergecheck, "save_if_owner",
                        lambda project, expected, data, **kw: saved.append(dict(data)) or True)
    monkeypatch.setattr(mergecheck, "_tidy", lambda check_id: None)
    monkeypatch.setattr(mergecheck, "_failure_effects", lambda project, data: None)
    data = {"id": "alpha-p1-x", "state": "running", "pid": 4242, "process_started_at": "s:1.000001"}
    mergecheck._finish({"id": "alpha-p1"}, data, {"state": outcome})
    assert saved and "pid" not in saved[-1] and "process_started_at" not in saved[-1]


def test_cancelled_merge_check_record_drops_pid_and_identity(monkeypatch):
    saved = []
    running = {"id": "alpha-p1-x", "state": "running", "pid": 4242, "process_started_at": "s:1.000001",
               "revision": 3}
    monkeypatch.setattr(mergecheck, "record", lambda project: dict(running))
    monkeypatch.setattr(mergecheck, "is_git_project", lambda project: True)
    monkeypatch.setattr(mergecheck, "approved_repo_paths", lambda project: ["/repo"])
    monkeypatch.setattr(mergecheck, "last_stage_approved", lambda project: True)
    monkeypatch.setattr(mergecheck, "pair", lambda repo, project: ("a" * 40, "b" * 40))
    monkeypatch.setattr(mergecheck, "packages_merged", lambda project: False)
    monkeypatch.setattr(mergecheck, "_stop_job", lambda data: None)
    monkeypatch.setattr(mergecheck, "save_if_owner",
                        lambda project, expected, data, **kw: saved.append(dict(data)) or True)
    mergecheck._project_step({"id": "alpha-p1", "state": "executing"})
    assert saved[-1]["state"] == "cancelled"
    assert "pid" not in saved[-1] and "process_started_at" not in saved[-1]


def _demo_root(tmp_path, record):
    root = tmp_path / "ygg-demo-unit"
    (root / "state" / "activations").mkdir(parents=True)
    (root / "state" / "activations" / "act-0001.json").write_text(json.dumps(record))
    return root


def test_teardown_never_signals_a_pid_whose_start_identity_differs(tmp_path):
    sleeper = _sleeper()
    try:
        root = _demo_root(tmp_path, {"node": "pm1", "pid": sleeper.pid, "process_start_identity": "s:1"})
        assert run.stop_everything(root, {"PATH": os.environ["PATH"]}, ran_conductor=False,
                                   ygg_argv=["true"]) is None
        time.sleep(0.3)
        assert sleeper.poll() is None
    finally:
        sleeper.kill()
        sleeper.wait()


def test_teardown_never_signals_a_record_without_start_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "STEP4_SECONDS", 1)
    sleeper = _sleeper()
    try:
        root = _demo_root(tmp_path, {"node": "pm1", "pid": sleeper.pid})
        reason = run.stop_everything(root, {"PATH": os.environ["PATH"]}, ran_conductor=False, ygg_argv=["true"])
        assert reason == "process " + str(sleeper.pid) + " has no recorded start identity and was not signalled"
        assert sleeper.poll() is None
    finally:
        sleeper.kill()
        sleeper.wait()


def test_teardown_signals_a_recorded_process_with_its_exact_identity(tmp_path):
    sleeper = _sleeper()
    try:
        root = _demo_root(tmp_path, {"node": "pm1", "pid": sleeper.pid,
                                     "process_started_at": util.process_start(sleeper.pid)})
        assert run.stop_everything(root, {"PATH": os.environ["PATH"]}, ran_conductor=False,
                                   ygg_argv=["true"]) is None
        assert sleeper.wait(timeout=5) is not None
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


def test_teardown_fails_while_a_merge_check_is_still_starting(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "STEP4_SECONDS", 0.5)
    root = tmp_path / "ygg-demo-unit"
    (root / "state" / "merge-checks").mkdir(parents=True)
    (root / "state" / "merge-checks" / "alpha-p1.json").write_text(json.dumps({"id": "alpha-p1-x",
                                                                                "state": "starting"}))
    reason = run.stop_everything(root, {"PATH": os.environ["PATH"]}, ran_conductor=False, ygg_argv=["true"])
    assert reason == "merge check alpha-p1-x is still starting, so its job can't be accounted for"


# ---------------------------------------------------------------- ygg demo --cleanup

@pytest.fixture
def temp_folder(tmp_path, monkeypatch):
    tmp = tmp_path / "t"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    monkeypatch.setattr(run.signal, "signal", lambda number, handler: None)
    monkeypatch.setattr(run, "RECOVERY_SETTLE_SECONDS", 0)
    return tmp


def _left_folder(tmp, *, driver_started="s:1.000000", records=()):
    folder = tmp / "ygg-demo-unit"
    folder.mkdir(mode=0o700)
    for name in ("state/activations", "tracker", "home", "userhome"):
        (folder / name).mkdir(parents=True)
    marker = {"root": os.path.realpath(folder), "driver_pid": os.getpid(), "ended": False}
    if driver_started is not None:
        marker["driver_started"] = driver_started
    (folder / "demo-marker.json").write_text(json.dumps(marker))
    for number, record in enumerate(records, start=1):
        (folder / "state" / "activations" / ("act-%04d.json" % number)).write_text(json.dumps(record))
    return folder


def _tree(path):
    return sorted((str(p.relative_to(path)), p.is_dir()) for p in path.rglob("*"))


@pytest.mark.parametrize("where", ["state.safety", "state/activations/act-0009.json", "state/activations/old"])
def test_cleanup_refuses_a_symlink_on_any_path_it_reads(temp_folder, tmp_path, capsys, where):
    outside = tmp_path / "outside"
    (outside / "locks").mkdir(parents=True)
    (outside / "act-0009.json").write_text(json.dumps({"node": "pm9", "pid": os.getpid(),
                                                       "process_started_at": util.process_start(os.getpid())}))
    before = _tree(outside)
    folder = _left_folder(temp_folder)
    target = outside / "act-0009.json" if where.endswith(".json") else outside
    os.symlink(target, folder / where)
    assert run.cleanup(str(folder)) == 1
    error = capsys.readouterr().err
    assert error.startswith("demo: " + str(folder) + " is not a safe demo folder: " + where + " is a symlink")
    assert _tree(outside) == before
    assert not list(folder.glob("cleanup-*"))


def test_cleanup_refuses_a_marker_without_a_readable_driver_start(temp_folder, capsys):
    for started in (None, "Sat Nov 22 01:00:00 2026", "s:soon"):
        folder = _left_folder(temp_folder, driver_started=started)
        assert run.cleanup(str(folder)) == 1
        assert "is not a safe demo folder: its marker has no readable driver start" in capsys.readouterr().err
        run._rmtree(folder)


def test_cleanup_refuses_a_live_recorded_process_older_than_the_demo(temp_folder, capsys):
    sleeper = _sleeper()
    try:
        identity = util.process_start(sleeper.pid)
        later = "s:%d.000000" % (int(float(identity[2:])) + 1000)
        folder = _left_folder(temp_folder, driver_started=later,
                              records=[{"node": "pm1", "pid": sleeper.pid, "process_started_at": identity}])
        assert run.cleanup(str(folder)) == 1
        error = capsys.readouterr().err
        assert "is not a safe demo folder: recorded agent process " + str(sleeper.pid) + " started before the demo" \
            in error
        time.sleep(0.3)
        assert sleeper.poll() is None
        assert folder.exists()
    finally:
        sleeper.kill()
        sleeper.wait()


def test_cleanup_stops_a_recorded_process_and_removes_the_folder(temp_folder, capsys, monkeypatch):
    sleeper = _sleeper()
    try:
        identity = util.process_start(sleeper.pid)
        earlier = "s:%d.000000" % (int(float(identity[2:])) - 10)
        folder = _left_folder(temp_folder, driver_started=earlier,
                              records=[{"node": "pm1", "pid": sleeper.pid, "process_started_at": identity}])
        monkeypatch.setattr(run.sys, "executable", "/usr/bin/true")  # `ygg stop` is not under test here
        assert run.cleanup(str(folder)) == 0
        assert capsys.readouterr().out.splitlines()[-1] == "demo: removed " + str(folder)
        assert sleeper.wait(timeout=5) is not None
        assert not folder.exists()
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


# ---------------------------------------------------------------- the driver's teardown

def _driver(tmp_path, monkeypatch, flow):
    monkeypatch.setattr(run.signal, "signal", lambda number, handler: None)
    driver = run.Driver(fast=True, keep=True, no_open=True)
    root = tmp_path / "ygg-demo-unit"

    def setup():
        (root / "state").mkdir(parents=True)
        driver.root, driver.shown_root = root, str(root)
        (root / "demo-marker.json").write_text(json.dumps({"root": str(root), "ended": False}))
        driver.env = {"PATH": os.environ["PATH"]}
    monkeypatch.setattr(driver, "setup", setup)
    monkeypatch.setattr(driver, "flow", lambda: flow(driver))
    return driver, root


def test_a_fault_after_the_conductor_starts_still_stops_it(tmp_path, monkeypatch, capsys):
    def flow(driver):
        driver.in_flow = True
        driver.conductor = driver.start_long(["sleep", "60"], "conductor.pid", stdout=subprocess.DEVNULL)
        raise OSError("disk full")
    driver, root = _driver(tmp_path, monkeypatch, flow)
    assert driver.run() == 1
    captured = capsys.readouterr()
    assert "demo: failed at step 1: OSError: disk full" in captured.err
    assert captured.out.splitlines()[-1] == "demo: kept " + str(root)
    assert driver.conductor[0].poll() is not None
    assert json.loads((root / "demo-marker.json").read_text())["ended"] is True


def test_a_pid_file_that_cannot_be_written_ends_the_child_through_its_handle(tmp_path, monkeypatch, capsys):
    real_write = run._write

    def write(path, text, mode=0o600):
        if Path(path).name == "conductor.pid":
            raise OSError("read-only file system")
        return real_write(path, text, mode)
    monkeypatch.setattr(run, "_write", write)

    def flow(driver):
        driver.in_flow = True
        driver.start_long(["sleep", "60"], "conductor.pid", stdout=subprocess.DEVNULL)
    driver, root = _driver(tmp_path, monkeypatch, flow)
    assert driver.run() == 1
    assert "demo: failed at step 1: OSError: read-only file system" in capsys.readouterr().err
    assert driver.children and all(child.poll() is not None for child in driver.children)


def test_teardown_runs_with_a_broken_stdout(tmp_path, monkeypatch):
    class Broken:
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    def flow(driver):
        driver.in_flow = True
        driver.conductor = driver.start_long(["sleep", "60"], "conductor.pid", stdout=subprocess.DEVNULL)
        raise run.Failure("step 1", "test fault")
    driver, root = _driver(tmp_path, monkeypatch, flow)
    monkeypatch.setattr(sys, "stdout", Broken())
    monkeypatch.setattr(sys, "stderr", Broken())
    assert driver.run() == 1
    assert driver.conductor[0].poll() is not None


# ---------------------------------------------------------------- the reviewer's prompt section

@pytest.mark.parametrize("shape", ["v1.1", "v1.0"])
def test_review_section_reads_both_record_shapes(monkeypatch, shape):
    from yggdrasil import launcher
    review = {"id": "rv-0001", "package": "WP-1", "project": "alpha-p1", "base": None, "head": "abc1234"}
    found = ({"review": "rv-0001", "review_target": review, "cwd": ""} if shape == "v1.1"
             else {"review": review, "cwd": ""})
    monkeypatch.setattr(launcher, "receipt", lambda activation: found)
    text = launcher.review_section({"role": "reviewer"}, "act-0001", {})
    assert "## Review rv-0001" in text and "You review package WP-1 of alpha-p1" in text
    monkeypatch.setattr(launcher, "receipt", lambda activation: {"review": "rv-0001", "cwd": ""})
    assert launcher.review_section({"role": "reviewer"}, "act-0001", {}) == ""


# ---------------------------------------------------------------- cleanup preflight

def test_cleanup_refuses_a_fifo(temp_folder, capsys):
    folder = _left_folder(temp_folder)
    os.mkfifo(folder / "state" / "activations" / "act-0001.json")
    assert run.cleanup(str(folder)) == 1
    assert "is not a safe demo folder: state/activations/act-0001.json is not a regular file" in \
        capsys.readouterr().err


@pytest.mark.parametrize("started", ["s:nan", "s:inf", "s:-inf"])
def test_cleanup_refuses_a_non_finite_driver_start(temp_folder, capsys, started):
    folder = _left_folder(temp_folder, driver_started=started)
    assert run.cleanup(str(folder)) == 1
    assert "is not a safe demo folder: its marker has no readable driver start" in capsys.readouterr().err


def _earlier(seconds=10):
    return "s:%d.000000" % (time.time() - seconds)


@pytest.mark.parametrize("record, reason", [
    ({"activation": "act-0001", "node": "pm1", "cwd": "/tmp"}, "names a working folder outside"),
    ({"activation": "act-0001", "node": "pm1", "agent_nonce": "n1", "supervisor_started_at": "s:2.000000"},
     "has no recorded agent identity"),
    ({"activation": "act-0001", "node": "pm1", "pid": os.getpid()}, "has no recorded agent identity"),
])
def test_cleanup_refuses_an_activation_it_cannot_account_for(temp_folder, capsys, monkeypatch, record, reason):
    folder = _left_folder(temp_folder, driver_started=_earlier(), records=[record])
    assert run.cleanup(str(folder)) == 1
    captured = capsys.readouterr()
    # Nothing was signalled (the folder records no conductor): the untouched folder's heading.
    assert "demo: " + str(folder) + " is not a safe demo folder: activation act-0001 " + reason in captured.err
    assert "failed at cleanup" not in captured.err


def test_cleanup_refuses_an_activation_open_in_the_journal_without_a_recorded_agent(temp_folder, capsys,
                                                                                   monkeypatch):
    folder = _left_folder(temp_folder, driver_started=_earlier())
    journal = folder / "tracker" / "initiatives" / "alpha" / "documents"
    journal.mkdir(parents=True)
    (journal / "journal.md").write_text("---\nrevision: 1\n---\n```yaml\nkind: activation\nkey: act-0007\n"
                                        "phase: spawned\nagent_nonce: n7\n```\n")
    assert run.cleanup(str(folder)) == 1
    assert "is not a safe demo folder: activation act-0007 is open in the journal with no recorded agent" in \
        capsys.readouterr().err


# ---------------------------------------------------------------- merge-check start and recovery

def test_a_job_whose_identity_cannot_be_read_is_published_without_one(monkeypatch):
    real_popen = subprocess.Popen
    jobs = []

    def popen(argv, **kwargs):
        job = real_popen(["sleep", "60"], stdin=subprocess.DEVNULL, start_new_session=True)
        jobs.append(job)
        return job
    monkeypatch.setattr(mergecheck.subprocess, "Popen", popen)
    monkeypatch.setattr(mergecheck.util, "process_start", lambda pid: None)
    try:
        data = mergecheck._start("alpha-p1", "/repo", ("a" * 40, "b" * 40), {})
        published = mergecheck.record("alpha-p1")
        assert data["pid"] == jobs[0].pid and published["state"] == "running"
        assert published["pid"] == jobs[0].pid and published["process_started_at"] is None
        assert jobs[0].poll() is None  # not killed: no retry every tick, no leaked log
    finally:
        for job in jobs:
            job.kill()
            job.wait()


@pytest.mark.parametrize("check, reason", [
    ({"id": "alpha-p1-x", "state": "starting"}, "merge check alpha-p1-x is still starting"),
    ({"id": "alpha-p1-x", "state": "running", "pid": os.getpid()}, "merge check alpha-p1-x has no recorded identity"),
])
def test_cleanup_refuses_a_merge_check_it_cannot_account_for(temp_folder, capsys, check, reason):
    folder = _left_folder(temp_folder, driver_started=_earlier())
    (folder / "state" / "merge-checks").mkdir()
    (folder / "state" / "merge-checks" / "alpha-p1.json").write_text(json.dumps(check))
    assert run.cleanup(str(folder)) == 1
    assert "is not a safe demo folder: " + reason in capsys.readouterr().err
    assert folder.exists()


def test_cleanup_names_an_exit_the_journal_never_recorded(temp_folder, capsys):
    folder = _left_folder(temp_folder, driver_started=_earlier(),
                          records=[{"activation": "act-0001", "node": "pm1", "exited_at": "2026-11-22T01:00:00Z"}])
    journal = folder / "tracker" / "initiatives" / "alpha" / "documents"
    journal.mkdir(parents=True)
    (journal / "journal.md").write_text("---\nrevision: 1\n---\n```yaml\nkind: activation\nkey: act-0001\n"
                                        "phase: spawned\n```\n")
    assert run.cleanup(str(folder)) == 1
    captured = capsys.readouterr()
    assert ("is not a safe demo folder: activation act-0001's record shows its agent ended but the journal "
            "doesn't, so its other processes can't be ruled out; stop them, then remove " + str(folder) +
            " by hand") in captured.err


def test_cleanup_never_runs_ygg_stop(temp_folder, monkeypatch):
    started = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(run.subprocess, "Popen", lambda argv, **kw: started.append(argv) or real_popen(argv, **kw))
    folder = _left_folder(temp_folder, driver_started=_earlier())
    assert run.cleanup(str(folder)) == 0
    assert not any("stop" in argv for argv in started)


# ---------------------------------------------------------------- process identity and strict records in cleanup

def _detached_group(tmp_path):
    """A process group led by a process this test does not parent (so it is reaped when it ends), with one
    member that ignores SIGTERM. Returns (leader pid, member pid)."""
    out = tmp_path / "group-pids"
    leader = ("import os, subprocess, sys, time\n"
              "member = subprocess.Popen([sys.executable, '-c', 'import signal, time; "
              "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
              "time.sleep(0.5)\n"
              "open(sys.argv[1], 'w').write('%d %d' % (os.getpid(), member.pid))\n"
              "time.sleep(60)\n")
    launcher = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], start_new_session=True)"
    subprocess.run([sys.executable, "-c", launcher, leader, str(out)], check=True)
    for _ in range(100):
        if out.exists() and out.read_text().strip():
            break
        time.sleep(0.05)
    leader_pid, member_pid = map(int, out.read_text().split())
    return leader_pid, member_pid


def _end(*pids):
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def test_a_live_pid_whose_start_cannot_be_read_is_unknown_and_refuses(temp_folder, capsys, monkeypatch, tmp_path):
    sleeper = _sleeper()
    try:
        identity = util.process_start(sleeper.pid)
        folder = _left_folder(temp_folder, driver_started=_earlier(),
                              records=[{"node": "pm1", "pid": sleeper.pid, "process_started_at": identity}])
        real = util.process_start
        monkeypatch.setattr(run.util, "process_start", lambda pid: None if pid == sleeper.pid else real(pid))
        assert run.classify(sleeper.pid, identity) == run.UNKNOWN
        assert run.cleanup(str(folder)) == 1
        assert "is not a safe demo folder: recorded agent process " + str(sleeper.pid) + \
            " can't be identified exactly" in capsys.readouterr().err
        monkeypatch.setattr(run, "STEP4_SECONDS", 0.5)
        reason = run.stop_everything(folder, {"PATH": os.environ["PATH"]}, ran_conductor=False, use_ygg_stop=False)
        assert "can't be identified exactly" in reason
        assert sleeper.poll() is None and folder.exists()
    finally:
        sleeper.kill()
        sleeper.wait()


@pytest.mark.parametrize("name, text, reason", [
    ("merge-checks/alpha-p1.json", "{", "state/merge-checks/alpha-p1.json is malformed"),
    ("merge-checks/alpha-p1.json", "[]", "state/merge-checks/alpha-p1.json is malformed"),
    ("merge-checks/alpha-p1.json", json.dumps({"id": "x", "state": "running", "process_started_at": "s:5.000000"}),
     "state/merge-checks/alpha-p1.json is running with no pid"),
    ("merge-checks/alpha-p1.json", json.dumps({"id": "x", "state": "running"}),
     "state/merge-checks/alpha-p1.json is running with no pid"),
    ("merge-checks/alpha-p1.json", json.dumps({"id": "x", "state": "running", "pid": "12",
                                               "process_started_at": "s:5.000000"}),
     "state/merge-checks/alpha-p1.json has a pid that is not a process id"),
    ("merge-checks/alpha-p1.json", json.dumps({"id": "x", "state": "running", "pid": 1,
                                               "process_started_at": "s:5.000000"}),
     "state/merge-checks/alpha-p1.json has a pid that is not a process id"),
    ("merge-checks/alpha-p1.json", json.dumps({"id": "x", "state": "starting", "pid": 4242}),
     "state/merge-checks/alpha-p1.json is starting but holds a pid"),
    ("merge-checks/alpha-p1.json", "null", "state/merge-checks/alpha-p1.json is malformed"),
    ("activations/act-0001.json", "null", "state/activations/act-0001.json is malformed"),
    ("activations/act-0001.json", "not json", "state/activations/act-0001.json is malformed"),
    ("demo-groups.json", "null", "state/demo-groups.json is malformed"),
])
def test_cleanup_refuses_a_record_it_cannot_read_strictly(temp_folder, capsys, name, text, reason):
    folder = _left_folder(temp_folder, driver_started=_earlier())
    (folder / "state" / name).parent.mkdir(parents=True, exist_ok=True)
    (folder / "state" / name).write_text(text)
    assert run.cleanup(str(folder)) == 1
    assert "is not a safe demo folder: " + reason in capsys.readouterr().err
    assert folder.exists()


def test_cleanup_refuses_a_malformed_pid_file(temp_folder, capsys):
    folder = _left_folder(temp_folder, driver_started=_earlier())
    (folder / "conductor.pid").write_text("not-a-pid\n")
    assert run.cleanup(str(folder)) == 1
    assert "is not a safe demo folder: conductor.pid is malformed" in capsys.readouterr().err


def test_recovery_refuses_a_group_member_that_outlives_its_leader(temp_folder, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(run, "STEP4_SECONDS", 3)
    leader, member = _detached_group(tmp_path)
    try:
        identity = util.process_start(leader)
        earlier = "s:%d.000000" % (int(float(identity[2:])) - 10)
        folder = _left_folder(temp_folder, driver_started=earlier,
                              records=[{"node": "pm1", "pid": leader, "process_started_at": identity}])
        assert run.cleanup(str(folder)) == 1
        captured = capsys.readouterr()
        assert "demo: failed at cleanup: process group " + str(leader) + " still has members" in captured.err
        assert captured.out.splitlines()[-1] == "demo: kept " + str(folder)
        assert util.pid_alive(member) and folder.exists()
        listed = json.loads((folder / "state" / "demo-groups.json").read_text())
        assert listed == {str(leader): identity}  # written before the first signal to the group
        # A second recovery reads the listed group and refuses while it has members, signalling nothing.
        assert run.cleanup(str(folder)) == 1
        assert "is not a safe demo folder: process group " + str(leader) + " listed in state/demo-groups.json " \
            "still has members" in capsys.readouterr().err
        assert util.pid_alive(member)
    finally:
        _end(leader, member)


def test_a_dead_pid_without_identity_does_not_block_teardown(tmp_path):
    finished = subprocess.Popen(["true"])
    finished.wait()
    root = _demo_root(tmp_path, {"node": "pm1", "pid": finished.pid})
    assert run.stop_everything(root, {"PATH": os.environ["PATH"]}, ran_conductor=False, use_ygg_stop=False) is None


# ---------------------------------------------------------------- unknown pairs and vanishing processes in teardown

def test_an_unknown_pair_is_skipped_and_the_others_are_still_signalled(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "STEP4_SECONDS", 1)
    unknown, known = _sleeper(), _sleeper()
    try:
        root = _demo_root(tmp_path, {"node": "pm1", "pid": unknown.pid,
                                     "process_started_at": util.process_start(unknown.pid)})
        (root / "state" / "activations" / "act-0002.json").write_text(json.dumps({
            "node": "pm1tl1", "pid": known.pid, "process_started_at": util.process_start(known.pid)}))
        real = util.process_start
        monkeypatch.setattr(run.util, "process_start", lambda pid: None if pid == unknown.pid else real(pid))
        reason = run.stop_everything(root, {"PATH": os.environ["PATH"]}, ran_conductor=False, use_ygg_stop=False)
        assert "process " + str(unknown.pid) + " can't be identified exactly" in reason
        assert known.wait(timeout=5) is not None  # still signalled
        assert unknown.poll() is None  # never signalled
    finally:
        for process in (unknown, known):
            if process.poll() is None:
                process.kill()
                process.wait()


def test_a_process_that_ends_while_it_is_read_is_gone_not_unknown(monkeypatch):
    answers = iter([True, False])  # alive when first asked; ended by the time its start is read
    monkeypatch.setattr(run, "_alive", lambda pid: next(answers))
    monkeypatch.setattr(run.util, "process_start", lambda pid: None)
    assert run.classify(4242, "s:1.000000") == run.GONE
    monkeypatch.setattr(run, "_alive", lambda pid: True)
    assert run.classify(4242, "s:1.000000") == run.UNKNOWN  # alive and unreadable stays unknown


def test_a_leaderless_or_reused_group_is_never_signalled(tmp_path, monkeypatch):
    leader, member = _detached_group(tmp_path)
    reused = _sleeper()  # a live group leader whose identity is not the recorded one: a reused id
    sent = []
    real_killpg = os.killpg
    monkeypatch.setattr(run.os, "killpg", lambda pgid, number: sent.append((pgid, number)) or real_killpg(pgid, number))
    try:
        identity = util.process_start(leader)
        _end(leader)
        for _ in range(50):
            if run.classify(leader, identity) == run.GONE:
                break
            time.sleep(0.1)
        groups = {leader: identity, reused.pid: "s:1.000000"}
        reason = run.settle(None, groups, seconds=0.5)
        assert reason == "process group " + str(leader) + " still has members"
        assert all(number == 0 for _, number in sent)  # probes only
        assert util.pid_alive(member) and reused.poll() is None
        assert reused.pid not in groups  # a reused id is dropped, never probed into a signal
    finally:
        _end(member)
        reused.kill()
        reused.wait()


def test_a_surviving_owned_child_fails_teardown(tmp_path):
    child = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        assert run.settle(None, {}, owned=[child], seconds=0.3) == "process " + str(child.pid) + " still running"
    finally:
        child.kill()
        child.wait()


def test_a_live_leader_of_a_signalled_group_fails_teardown(tmp_path):
    leader = _sleeper()
    try:
        groups = {leader.pid: util.process_start(leader.pid)}
        assert run.settle(None, groups, seconds=0.3) == "process " + str(leader.pid) + " still running"
    finally:
        leader.kill()
        leader.wait()


# ---------------------------------------------------------------- starting claims and changed processes in teardown

def test_every_starting_claim_refuses_even_old_with_the_conductor_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "STEP4_SECONDS", 0.5)
    root = tmp_path / "ygg-demo-unit"
    (root / "state" / "merge-checks").mkdir(parents=True)
    claim = root / "state" / "merge-checks" / "alpha-p1.json"
    claim.write_text(json.dumps({"id": "alpha-p1-x", "state": "starting"}))
    finished = subprocess.Popen(["true"])
    finished.wait()
    (root / "conductor.pid").write_text("%d s:1.000000\n" % finished.pid)
    os.utime(claim, (1, 1))
    reason = run.stop_everything(root, {"PATH": os.environ["PATH"]}, ran_conductor=False, use_ygg_stop=False)
    assert reason == "merge check alpha-p1-x is still starting, so its job can't be accounted for"


def test_a_process_that_changes_after_its_group_is_written_is_never_signalled(tmp_path, monkeypatch):
    leader = _sleeper()
    sent = []
    try:
        root = _demo_root(tmp_path, {"node": "pm1", "pid": leader.pid,
                                     "process_started_at": util.process_start(leader.pid)})
        answers = iter([run.EXACT, run.DIFFERENT])
        monkeypatch.setattr(run, "classify", lambda pid, started: next(answers, run.DIFFERENT))
        monkeypatch.setattr(run, "send", lambda pid, number: sent.append(pid) or True)
        log = run.SignalLog(run.PathFolder(root))
        assert run.signal_exact(leader.pid, util.process_start(leader.pid), 15, log) is False
        assert sent == [] and log.changed == [leader.pid]
        assert str(leader.pid) in json.loads((root / "state" / "demo-groups.json").read_text())  # written first
        assert "changed while it was being stopped" in run.settle(None, {}, changed=log.changed, seconds=0)
    finally:
        leader.kill()
        leader.wait()


@pytest.mark.parametrize("driver_pid", [None, "4242", 1, 0, True])
def test_recovery_refuses_a_marker_without_a_valid_driver_pid(temp_folder, capsys, driver_pid):
    folder = _left_folder(temp_folder, driver_started=_earlier())
    marker = json.loads((folder / "demo-marker.json").read_text())
    marker["driver_pid"] = driver_pid
    (folder / "demo-marker.json").write_text(json.dumps(marker))
    assert run.cleanup(str(folder)) == 1
    assert "is not a safe demo folder: its marker has no valid driver pid" in capsys.readouterr().err


def test_teardown_refuses_an_unfinished_activation_without_an_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "STEP4_SECONDS", 0.5)
    root = _demo_root(tmp_path, {"activation": "act-0001", "node": "pm1", "pid": None})
    reason = run.stop_everything(root, {"PATH": os.environ["PATH"]}, ran_conductor=False, use_ygg_stop=False)
    assert reason == "activation act-0001 has no recorded agent identity"


def test_a_ygg_stop_that_times_out_is_owned_ended_and_keeps_the_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "STOP_SECONDS", 1)
    monkeypatch.setattr(run, "STEP4_SECONDS", 1)
    root = _demo_root(tmp_path, {"activation": "act-0001", "node": "pm1", "exited_at": "2026-11-22T01:00:00Z"})
    owned = []
    reason = run.stop_everything(root, {"PATH": os.environ["PATH"]}, ran_conductor=True, owned=owned,
                                 ygg_argv=[sys.executable, "-c", "import time; time.sleep(200)"])
    assert reason == "ygg stop did not finish in 1 s"
    assert len(owned) == 1 and owned[0].poll() is not None
    assert str(owned[0].pid) in json.loads((root / "state" / "demo-groups.json").read_text())


def test_recovery_leaves_a_listed_group_with_a_live_recorded_leader_to_the_stop_steps(temp_folder, capsys):
    leader = _sleeper()
    try:
        identity = util.process_start(leader.pid)
        earlier = "s:%d.000000" % (int(float(identity[2:])) - 10)
        folder = _left_folder(temp_folder, driver_started=earlier,
                              records=[{"node": "pm1", "pid": leader.pid, "process_started_at": identity}])
        (folder / "state" / "demo-groups.json").write_text(json.dumps({str(leader.pid): identity}))
        assert run.cleanup(str(folder)) == 0
        assert leader.wait(timeout=5) is not None and not folder.exists()
    finally:
        if leader.poll() is None:
            leader.kill()
            leader.wait()


def test_a_failed_group_write_still_stops_the_drivers_own_children(tmp_path, monkeypatch, capsys):
    def flow(driver):
        driver.in_flow = True
        driver.conductor = driver.start_long(["sleep", "60"], "conductor.pid", stdout=subprocess.DEVNULL)
        raise run.Failure("step 1", "test fault")
    driver, root = _driver(tmp_path, monkeypatch, flow)
    monkeypatch.setattr(run.PathFolder, "write_state", lambda self, name, text: (_ for _ in ()).throw(OSError("full")))
    assert driver.run() == 1
    assert driver.conductor[0].poll() is not None
    assert "demo: failed at cleanup: the list of process groups the demo signalled can't be written" in capsys.readouterr().err


# ---------------------------------------------------------------- demo pace

def test_agent_checks_read_status_in_process(monkeypatch):
    from yggdrasil import config, status
    from yggdrasil.demo import agent
    monkeypatch.setattr(config, "validate", lambda **kwargs: None)
    monkeypatch.setattr(status, "build_status", lambda: {"projects": [{"id": "alpha-p1", "state": "executing"}]})
    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    assert agent._status() == {"projects": [{"id": "alpha-p1", "state": "executing"}]}
    monkeypatch.setattr(status, "build_status", lambda: (_ for _ in ()).throw(RuntimeError("unreadable")))
    assert agent._status() == {}  # an unreadable check holds the step, as a failed `ygg status` did


@pytest.mark.parametrize("fast, confirm", [(True, 5), (False, 0.5)])
def test_demo_config_paces_the_conductor_and_sets_the_confirm_window_per_mode(fast, confirm):
    import yaml
    values = yaml.safe_load(run.config_text(fast=fast))
    assert (values["tick_seconds"], values["settle_delay_seconds"], values["launch_confirm_seconds"]) == \
        (0.3, 0.2, confirm)
    assert values["alert_forwarder"] == "jsonl" and values["dashboard_refresh_seconds"] == 2


def test_agent_check_validates_config_like_ygg_status(monkeypatch):
    from yggdrasil import config, status
    from yggdrasil.demo import agent
    calls = []
    monkeypatch.setattr(config, "validate", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(status, "build_status", lambda: {"projects": []})
    assert agent._status() == {"projects": []}
    assert calls == [{"check_plugins": True, "scan_tracker": True}]  # cli.main's call for `ygg status`


def test_agent_check_holds_on_a_config_made_invalid_mid_run(tmp_path, monkeypatch):
    """The demo config turns invalid while an agent runs: the next check reads it afresh and holds the step."""
    from yggdrasil.demo import agent
    path = tmp_path / "config.yaml"
    path.write_text("tracker: files\n")
    monkeypatch.setenv("YGG_CONFIG", str(path))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "tracker"))
    monkeypatch.setenv("YGG_TRACKER", "files")
    monkeypatch.delenv("YGG_PROFILE", raising=False)
    first = agent._status()
    assert isinstance(first.get("projects"), list)  # valid: the check reads
    path.write_text("tracker: files\ndashboard_refresh_seconds: 0\n")  # an invalid value
    assert agent._status() == {}
    path.write_text("tracker: files\nnot_a_setting: 1\n")  # an unknown key
    assert agent._status() == {}
    assert agent._holds({"package_exists": "WP-1"}, {}) is False  # the step holds


def test_agent_check_holds_when_status_cannot_be_serialised(monkeypatch):
    from yggdrasil import config, status
    from yggdrasil.demo import agent
    monkeypatch.setattr(config, "validate", lambda **kwargs: None)
    monkeypatch.setattr(status, "build_status", lambda: {"projects": [object()]})
    assert agent._status() == {}


def test_agent_check_logs_why_it_held(monkeypatch, capsys):
    from yggdrasil import config, status
    from yggdrasil.demo import agent
    monkeypatch.setattr(config, "validate", lambda **kwargs: None)
    monkeypatch.setattr(status, "build_status", lambda: (_ for _ in ()).throw(KeyError("projects")))
    assert agent._status() == {}
    assert "status check held the step: KeyError('projects')" in capsys.readouterr().err


@pytest.mark.parametrize("tick, age, stale", [(0.3, 2.0, False), (0.3, 3.5, True), (15, 40.0, False),
                                              (15, 46.0, True)])
def test_dashboard_stale_threshold_is_three_ticks_and_never_under_three_seconds(monkeypatch, tick, age, stale):
    import datetime
    from yggdrasil import config, dashboard
    now = datetime.datetime(2026, 11, 23, 1, 0, 0, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(dashboard.util, "now", lambda: now)
    monkeypatch.setattr(dashboard.util, "read_text", lambda path: (now - datetime.timedelta(seconds=age)).isoformat())
    monkeypatch.setattr(config, "get", lambda key, default=None: tick if key == "tick_seconds" else default)
    assert dashboard._stale() is stale


@pytest.mark.parametrize("platform, tty, display, expected", [
    ("linux", True, {}, False), ("linux", True, {"DISPLAY": ":0"}, True), ("linux", True, {"WAYLAND_DISPLAY": "w"}, True),
    ("linux", False, {"DISPLAY": ":0"}, False), ("darwin", True, {}, True), ("darwin", False, {}, False)])
def test_the_dashboard_opens_only_with_a_terminal_and_a_display(monkeypatch, platform, tty, display, expected):
    class Out:
        def isatty(self):
            return tty
    monkeypatch.setattr(run.sys, "platform", platform)
    monkeypatch.setattr(run.sys, "stdout", Out())
    for name in ("DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in display.items():
        monkeypatch.setenv(name, value)
    assert run.can_open_browser() is expected


# ---------------------------------------------------------------- removal fails closed

def _stuck_unlink(monkeypatch, name="stuck.txt"):
    """Make removing one file fail, as a file the user can't delete would."""
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if str(path).endswith(name):
            raise PermissionError(13, "Permission denied")
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", unlink)


def test_removal_deletes_a_link_never_its_target(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    outside.chmod(0o600)
    root = tmp_path / "ygg-demo-unit"
    (root / "sub").mkdir(parents=True)
    os.symlink(outside, root / "sub" / "link")
    assert run._rmtree(root) == []
    assert not root.exists()
    assert outside.read_text() == "keep" and stat.S_IMODE(outside.stat().st_mode) == 0o600


def test_a_failed_removal_is_reported_and_nothing_is_chmodded(tmp_path, monkeypatch):
    root = tmp_path / "ygg-demo-unit"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "stuck.txt").write_text("x")
    parent_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    chmods = []
    monkeypatch.setattr(os, "chmod", lambda *a, **k: chmods.append(a))
    _stuck_unlink(monkeypatch)
    errors = run._rmtree(root)
    assert errors and "stuck.txt" in errors[0] and root.exists()
    assert chmods == [] and stat.S_IMODE(tmp_path.stat().st_mode) == parent_mode


def _finishing_driver(tmp_path, monkeypatch, keep=False):
    driver, root = _driver(tmp_path, monkeypatch, lambda driver: setattr(driver, "in_flow", True))
    driver.keep = keep
    return driver, root


def test_a_finished_demo_removes_its_whole_folder(tmp_path, monkeypatch, capsys):
    driver, root = _finishing_driver(tmp_path, monkeypatch)
    assert driver.run() == 0
    assert capsys.readouterr().out.splitlines()[-1] == "demo: removed " + str(root)
    assert not root.exists()


def test_a_demo_folder_that_cannot_be_fully_removed_fails_and_says_so(tmp_path, monkeypatch, capsys):
    driver, root = _finishing_driver(tmp_path, monkeypatch)
    parent_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    real_setup = driver.setup

    def setup():
        real_setup()
        (root / "stuck.txt").write_text("x")
    monkeypatch.setattr(driver, "setup", setup)
    _stuck_unlink(monkeypatch)
    assert driver.run() == 1
    captured = capsys.readouterr()
    assert "demo: failed at cleanup: " + str(root) + " was only partly removed; remove it by hand. Errors:" \
        in captured.err
    assert "stuck.txt" in captured.err
    assert captured.out.splitlines()[-1] == "demo: partly removed " + str(root) + "; remove it by hand"
    assert "demo: removed" not in captured.out and "demo: kept" not in captured.out
    assert root.exists() and stat.S_IMODE(tmp_path.stat().st_mode) == parent_mode


def test_a_recovered_folder_that_cannot_be_fully_removed_fails_and_says_so(temp_folder, capsys, monkeypatch):
    folder = _left_folder(temp_folder, driver_started=_earlier())
    (folder / "userhome" / "stuck.txt").write_text("x")
    parent_mode = stat.S_IMODE(temp_folder.stat().st_mode)
    _stuck_unlink(monkeypatch)
    assert run.cleanup(str(folder)) == 1
    captured = capsys.readouterr()
    assert "demo: failed at cleanup: " + str(folder) + " was only partly removed; remove it by hand. Errors:" \
        in captured.err
    assert captured.out.splitlines()[-1] == "demo: partly removed " + str(folder) + "; remove it by hand"
    assert folder.exists() and stat.S_IMODE(temp_folder.stat().st_mode) == parent_mode



@pytest.mark.parametrize("suffix", ["/", "//", "/."])
def test_cleanup_refuses_a_symlinked_folder_however_it_is_spelled(temp_folder, capsys, suffix):
    target = _left_folder(temp_folder)
    link = temp_folder / "ygg-demo-link"
    os.symlink(target, link)
    before = _tree(target)
    assert run.cleanup(str(link) + suffix) == 1
    assert "is not a safe demo folder: it is a symlink" in capsys.readouterr().err
    assert target.is_dir() and _tree(target) == before


def test_every_removal_error_is_reported(temp_folder, capsys, monkeypatch):
    folder = _left_folder(temp_folder, driver_started=_earlier())
    (folder / "userhome" / "stuck-one.txt").write_text("x")
    (folder / "tracker" / "stuck-two.txt").write_text("x")
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if "stuck-" in str(path):
            raise PermissionError(13, "Permission denied")
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", unlink)
    assert run.cleanup(str(folder)) == 1
    error = capsys.readouterr().err
    assert "stuck-one.txt" in error and "stuck-two.txt" in error


# ---------------------------------------------------------------- removal and browser: behaviour, not mechanism

def _modes(*roots):
    """Every path under `roots` (links themselves, not their targets) with its mode."""
    found = {}
    for root in roots:
        for path in [root, *root.rglob("*")]:
            found[str(path)] = stat.S_IMODE(os.lstat(path).st_mode)
    return found


def _no_mode_changes(monkeypatch):
    calls = []
    for name in ("chmod", "fchmod", "lchmod"):
        monkeypatch.setattr(os, name, lambda *a, _n=name, **k: calls.append((_n, a)), raising=False)
    return calls


@pytest.mark.parametrize("branch", ["path", "dir_fd"])
def test_a_failed_removal_changes_no_mode_inside_or_outside(tmp_path, monkeypatch, branch):
    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    (outside / "inner" / "file.txt").write_text("keep")
    (outside / "inner" / "file.txt").chmod(0o640)
    root = tmp_path / "ygg-demo-unit"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "stuck.txt").write_text("x")
    (root / "a" / "b" / "stuck.txt").chmod(0o444)
    (root / "a" / "plain.txt").write_text("x")
    os.symlink(outside, root / "a" / "folder-link")
    before = _modes(outside)
    calls = _no_mode_changes(monkeypatch)
    _stuck_unlink(monkeypatch)
    if branch == "path":
        errors = run._rmtree(root)
    else:
        parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            errors = run._rmtree(root.name, dir_fd=parent)
        finally:
            os.close(parent)
    assert errors and calls == []
    assert _modes(outside) == before
    left = _modes(root)  # what removal left inside keeps its modes too
    assert left[str(root / "a" / "b" / "stuck.txt")] == 0o444


def test_removing_a_link_to_an_outside_folder_through_dir_fd_leaves_it_whole(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    (outside / "deeper").mkdir(parents=True)
    for name in ("one.txt", "deeper/two.txt"):
        (outside / name).write_text(name)
    before = _modes(outside)
    contents = {str(p): p.read_text() for p in outside.rglob("*.txt")}
    root = tmp_path / "ygg-demo-unit"
    (root / "state").mkdir(parents=True)
    os.symlink(outside, root / "state" / "folder-link")
    calls = _no_mode_changes(monkeypatch)
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert run._rmtree(root.name, dir_fd=parent) == []
    finally:
        os.close(parent)
    assert not root.exists() and calls == []
    assert _modes(outside) == before
    assert {str(p): p.read_text() for p in outside.rglob("*.txt")} == contents


@pytest.mark.parametrize("allowed", [False, True])
def test_show_dashboard_opens_a_browser_only_when_allowed(monkeypatch, capsys, allowed):
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda target: opened.append(target))
    monkeypatch.setattr(run, "can_open_browser", lambda: allowed)
    driver = run.Driver(fast=True, keep=False, no_open=False)
    driver.dashboard_line = "demo: dashboard file /x/alpha.html (refreshes every 2 s)"
    driver.dashboard_target = "file:///x/alpha.html"
    driver.show_dashboard()
    assert capsys.readouterr().out.strip() == driver.dashboard_line
    assert opened == (["file:///x/alpha.html"] if allowed else [])


def test_at_the_default_pace_no_activation_ends_before_one_pace(monkeypatch, tmp_path):
    from yggdrasil.demo import agent
    slept = []
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setattr(agent, "_holds", lambda when, record: False)  # nothing to do yet
    monkeypatch.setattr(agent, "_activation", lambda: {})
    monkeypatch.setattr(agent.sys, "stdin", None)
    monkeypatch.setenv("YGG_NODE", "pm1")
    monkeypatch.setenv("YGG_DEMO_ROOT", str(tmp_path))
    monkeypatch.setenv("YGG_DEMO_PACE", "3")
    assert agent.main() == 0 and slept == [3.0]
    (tmp_path / "agents").mkdir(exist_ok=True)
    (tmp_path / "agents" / "pm1.done").write_text("".join(s["id"] + "\n" for s in agent.script.STEPS["pm1"]))
    slept.clear()
    assert agent.main() == 0 and slept == [3.0]  # every step done: it still waits one pace
    monkeypatch.setenv("YGG_DEMO_PACE", "0")
    slept.clear()
    assert agent.main() == 0 and slept == []  # --fast: no wait




# ---------------------------------------------------------------- the demo succeeds only when every agent ended cleanly

NORMAL_ALERTS = [{"kind": "ygg-pid-ready"}, {"kind": "ygg-stage-ready"}, {"kind": "ygg-stage-approval"},
                 {"kind": "ygg-merge-ready"}, {"kind": "ygg-project-closed"},
                 {"kind": "ygg-rehydrate-skipped", "count": 2}]


def _journal_text(entries):
    import yaml
    return "---\nschema: ygg.journal.v1\n---\n\n" + "".join(
        "```yaml\n" + yaml.safe_dump(entry, sort_keys=False) + "```\n\n" for entry in entries)


def _world(root, *, agents=("pm1", "pm1tl1", "pm1su1"), receipt=None, journal=None, unrecorded=(),
           alerts=NORMAL_ALERTS):
    """A finished demo's records: one activation per agent, each with its receipt and its journal entries, two
    worktree closes, and the normal alerts. `receipt` / `journal` change the last agent's receipt and exit entry;
    `unrecorded` names agents the journal records with no receipt written."""
    (root / "state" / "activations").mkdir(parents=True, exist_ok=True)
    entries = [{"kind": "worktree-close", "actor": "ygg-app", "project": "alpha-p1", "at": "2026-11-23T06:59:00Z",
                "phase": phase} for phase in ("removing", "removed")]
    for number, node in enumerate(list(agents) + list(unrecorded), start=1):
        key = "act-%04d" % number
        last = node == agents[-1] and node not in unrecorded
        record = {"activation": key, "node": node, "outcome": "confirmed", "exited_at": "2026-11-23T07:00:00Z",
                  "exit_code": 0, "programme": "alpha", "project": "alpha-p1"}
        exit_entry = {"kind": "activation", "actor": "ygg-app", "key": key, "node": node, "phase": "exited",
                      "exit_code": 0}
        if last:
            record.update(receipt or {})
            exit_entry.update(journal or {})
        entries += [{"kind": "activation", "actor": "ygg-app", "key": key, "node": node, "phase": "reserved"},
                    {"kind": "activation", "actor": "ygg-app", "key": key, "node": node, "phase": "spawned"},
                    exit_entry]
        if node not in unrecorded:
            (root / "state" / "activations" / (key + ".json")).write_text(json.dumps(record))
    journal_path = root / "tracker" / "initiatives" / "alpha" / "documents" / "journal.md"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(_journal_text(entries))
    _write_alerts(root, alerts)


def _alert_line(row):
    if not isinstance(row, dict):
        return json.dumps(row)
    return json.dumps(dict({"id": "al-000000000001", "ts": "2026-11-23T07:00:00Z", "message": "m"}, **row))


def _write_alerts(root, rows, mode="w"):
    with open(root / "state" / "alerts.jsonl", mode) as handle:
        handle.write("".join(_alert_line(row) + "\n" for row in rows))


def _listing_fails(monkeypatch, suffix):
    real = os.listdir

    def listdir(path="."):
        if str(path).endswith(suffix):
            raise PermissionError(13, "Permission denied")
        return real(path)
    monkeypatch.setattr(run.os, "listdir", listdir)


@pytest.mark.parametrize("change, expected", [
    ({"receipt": {"exit_code": 1}, "journal": {"exit_code": 1}}, "pm1su1's agent ended with an error (exit 1)"),
    ({"receipt": {"outcome": "uncertain", "exited_at": None, "exit_code": None}},
     "pm1su1 was held because Yggdrasil couldn't confirm its agent started"),
    ({"receipt": {"interrupted": True}, "journal": {"ambiguous": True}},
     "pm1su1's agent was stopped while it ran, so its work is held for checking"),
    ({"receipt": {"outcome": "aborted", "exited_at": None, "exit_code": None}, "journal": {"phase": "aborted"}},
     "pm1su1's agent was stopped before it started"),
    ({"unrecorded": ("pm1rv1",)}, "pm1rv1's agent left no record"),
    ({"alerts": NORMAL_ALERTS + [[]]}, "the alert log has a line that isn't a valid alert"),
    ({"alerts": NORMAL_ALERTS + [None]}, "the alert log has a line that isn't a valid alert"),
    ({"alerts": NORMAL_ALERTS + [{"kind": "ygg-review-fallback"}]},
     "Yggdrasil raised an alert the demo doesn't expect (ygg-review-fallback)"),
    ({"alerts": NORMAL_ALERTS + [{"kind": "ygg-stop-held", "node": "pm1"}]},
     "pm1's agent was stopped while it ran, so its work is held for checking"),
    ({"alerts": [{"kind": "ygg-rehydrate-skipped", "count": 3}]}, "Yggdrasil skipped journal entries it couldn't read"),
    ({"alerts": [{"kind": "ygg-stage-ready", "escalated": True}]}, "a stage was left open after its PM's last turn"),
])
def test_anything_short_of_a_clean_end_is_found(tmp_path, change, expected):
    _world(tmp_path, **change)
    problems, _ = run.agent_findings(tmp_path, final=True)
    assert expected in problems


def test_a_clean_end_has_no_findings(tmp_path):
    _world(tmp_path)
    assert run.agent_findings(tmp_path, final=True) == ([], False)


def test_findings_never_print_activation_ids_or_alert_text(tmp_path):
    _world(tmp_path, receipt={"exit_code": 1}, unrecorded=("pm1rv1",), alerts=NORMAL_ALERTS + [
        {"kind": "ygg-uncertain", "node": "pm1", "message": "Activation act-0001 of pm1 PRIVATE DETAIL"},
        {"kind": "Not A Kind; drop all", "node": "../x"}])
    problems, _ = run.agent_findings(tmp_path, final=True)
    text = "; ".join(problems)
    assert "act-" not in text and "PRIVATE" not in text and "drop all" not in text and "../x" not in text
    assert "pm1 was held because Yggdrasil couldn't confirm its agent started" in problems
    assert "Yggdrasil raised an alert the demo doesn't expect (of an unknown kind)" in problems


def test_a_record_listing_error_is_a_finding_not_an_empty_list(tmp_path, monkeypatch):
    _world(tmp_path)
    _listing_fails(monkeypatch, "state/activations")
    problems, _ = run.agent_findings(tmp_path, final=True)
    assert problems == ["the agents' records can't be listed"]


def test_a_running_agent_is_open_and_a_half_written_alert_waits_for_the_final_read(tmp_path):
    _world(tmp_path, receipt={"exited_at": None, "exit_code": None}, journal={"phase": "spawned"})
    with open(tmp_path / "state" / "alerts.jsonl", "a") as handle:
        handle.write('{"kind": "ygg-sta')
    early, still_open = run.agent_findings(tmp_path, final=False)
    assert still_open and early == ["pm1su1's agent had not ended"]
    final, _ = run.agent_findings(tmp_path, final=True)
    assert "the alert log has a line that isn't a valid alert" in final


def _flowing_driver(tmp_path, monkeypatch, **world):
    """The real flow and finish over a scripted world: ygg commands succeed without running, status walks the
    project from plan to closed, the conductor is a sleep, and the records are those of `_world`."""
    monkeypatch.setattr(run.signal, "signal", lambda number, handler: None)
    real_sleep = time.sleep
    monkeypatch.setattr(run.time, "sleep", lambda seconds: real_sleep(min(seconds, 0.05)))
    driver = run.Driver(fast=True, keep=False, no_open=True)
    root = tmp_path / "ygg-demo-unit"

    def setup():
        root.mkdir()
        driver.root, driver.shown_root = root, str(root)
        (root / "demo-marker.json").write_text(json.dumps({"root": str(root), "ended": False}))
        driver.env = {"PATH": os.environ["PATH"]}
        _world(root, **world)
    states = iter(["pid-ready", "ready-to-merge"])

    def start_long(argv, pid_file, *, stdout):
        return real_start(["sleep", "60"], pid_file, stdout=subprocess.DEVNULL)
    real_start = driver.start_long
    monkeypatch.setattr(driver, "setup", setup)
    monkeypatch.setattr(driver, "ygg_ok", lambda *args, where: subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(driver, "setup_dashboard",
                        lambda: setattr(driver, "dashboard_line", "demo: dashboard not in this release"))
    monkeypatch.setattr(driver, "start_long", start_long)
    monkeypatch.setattr(driver, "status", lambda where: {"projects": [
        {"id": run.script.PROJECT, "state": next(states, "closed"), "stage": {}}]})
    monkeypatch.setattr(driver, "reviews", lambda: {"r1": {"package": "WP-1", "state": "approved"},
                                                    "r2": {"package": "WP-2", "state": "approved"}})
    return driver, root


def test_a_clean_run_through_the_real_flow_finishes(tmp_path, monkeypatch, capsys):
    driver, root = _flowing_driver(tmp_path, monkeypatch)
    assert driver.run() == 0
    captured = capsys.readouterr()
    assert driver.agents_checked
    assert "demo: 7/7" in captured.out and "demo: finished in" in captured.out and not root.exists()
    assert captured.err == ""


@pytest.mark.parametrize("world, listing, expected", [
    ({"unrecorded": ("pm1rv1",)}, None, "pm1rv1's agent left no record"),
    ({}, "state/activations", "the agents' records can't be listed"),
    ({"receipt": {"interrupted": True}, "journal": {"ambiguous": True}}, None,
     "pm1su1's agent was stopped while it ran, so its work is held for checking"),
    ({"alerts": NORMAL_ALERTS + [None]}, None, "the alert log has a line that isn't a valid alert"),
    ({"alerts": NORMAL_ALERTS + [{"kind": "ygg-review-held", "node": "pm1"}]}, None,
     "Yggdrasil raised an alert the demo doesn't expect (ygg-review-held)"),
], ids=["missing-receipt", "listing-error", "held-stop-exit-0", "non-object-row", "unlisted-kind"])
def test_the_real_flow_fails_at_step_7_and_keeps_the_folder(tmp_path, monkeypatch, capsys, world, listing, expected):
    driver, root = _flowing_driver(tmp_path, monkeypatch, **world)
    if listing:
        _listing_fails(monkeypatch, listing)
    assert driver.run() == 1
    captured = capsys.readouterr()
    assert "demo: failed at step 7: not every agent ended cleanly: " in captured.err and expected in captured.err
    assert "demo: finished" not in captured.out
    assert captured.out.splitlines()[-1] == "demo: kept " + str(root) and root.exists()
    assert "act-" not in captured.err


def test_an_alert_written_after_the_first_read_fails_the_run(tmp_path, monkeypatch, capsys):
    driver, root = _flowing_driver(tmp_path, monkeypatch)
    real_stop = run.stop_everything

    def stop_everything(folder, env, **kwargs):
        assert driver.agents_checked  # the first read found nothing
        _write_alerts(root, [{"kind": "ygg-launch-refused", "node": "pm1rv1"}], mode="a")
        return real_stop(folder, env, **kwargs)
    monkeypatch.setattr(run, "stop_everything", stop_everything)
    assert driver.run() == 1
    captured = capsys.readouterr()
    assert "demo: failed at step 7: not every agent ended cleanly: Yggdrasil refused to start pm1rv1's agent" \
        in captured.err
    assert "demo: finished" not in captured.out and root.exists()


# ---------------------------------------------------------------- corrupted, duplicated or contradictory evidence

JOURNAL = Path("tracker/initiatives/alpha/documents/journal.md")


def _append(root, relative, text):
    with open(root / relative, "a") as handle:
        handle.write(text)


@pytest.mark.parametrize("tail", [
    "```yaml\nkind: [unclosed\n```\n",
    "```yaml\nkind: activation\nkey: act-0009\n",
    "a line that is not a block\n",
    "```yaml\n- a list, not an entry\n```\n",
], ids=["does-not-parse", "unfinished-tail", "stray-text", "not-a-mapping"])
def test_a_journal_block_that_cannot_be_read_fails(tmp_path, tail):
    _world(tmp_path)
    _append(tmp_path, JOURNAL, tail)
    problems, _ = run.agent_findings(tmp_path, final=True)
    assert "the programme journal has an entry that can't be read" in problems


def test_a_journal_with_trailing_blank_lines_reads_cleanly(tmp_path):
    _world(tmp_path)
    _append(tmp_path, JOURNAL, "\n\n  \n")
    assert run.agent_findings(tmp_path, final=True) == ([], False)


def test_two_records_for_one_activation_fail(tmp_path):
    _world(tmp_path)
    record = json.loads((tmp_path / "state/activations/act-0003.json").read_text())
    (tmp_path / "state/activations/act-0009.json").write_text(json.dumps(record))
    problems, _ = run.agent_findings(tmp_path, final=True)
    assert "pm1su1 has two records for one run" in problems


def test_a_record_naming_another_run_than_its_file_fails(tmp_path):
    _world(tmp_path)
    (tmp_path / "state/activations/act-0003.json").rename(tmp_path / "state/activations/act-0009.json")
    problems, _ = run.agent_findings(tmp_path, final=True)
    assert "pm1su1's record names a different run than its file" in problems


def _second_exit(root, **fields):
    entry = {"kind": "activation", "actor": "ygg-app", "key": "act-0003", "node": "pm1su1", "phase": "exited",
             "exit_code": 0}
    entry.update(fields)
    _append(root, JOURNAL, _journal_text([entry]).split("---\n\n", 1)[1])


@pytest.mark.parametrize("fields, expected", [
    ({"ambiguous": True}, "pm1su1's agent was stopped while it ran, so its work is held for checking"),
    ({"exit_code": 1}, "pm1su1's run has two different exit records in the programme journal"),
    ({"capacity": True}, "pm1su1's run has two different exit records in the programme journal"),
])
def test_every_journal_exit_is_judged(tmp_path, fields, expected):
    _world(tmp_path)
    _second_exit(tmp_path, **fields)
    problems, _ = run.agent_findings(tmp_path, final=True)
    assert expected in problems


def test_identical_journal_exits_pass(tmp_path):
    _world(tmp_path)
    _second_exit(tmp_path)
    assert run.agent_findings(tmp_path, final=True) == ([], False)


@pytest.mark.parametrize("closes_at, project, count, clean", [
    ("2026-11-23T06:59:00Z", "alpha-p1", 2, True),
    ("2026-11-23T07:00:00Z", "alpha-p1", 2, True),  # the same second as the alert
    ("2026-11-23T07:00:01Z", "alpha-p1", 2, False),  # journaled after the alert
    ("2026-11-23T06:59:00Z", "beta-p1", 2, False),  # another project's closes
    ("2026-11-23T06:59:00Z", "alpha-p1", 3, False),  # more skipped than closes
    ("not a time", "alpha-p1", 2, False),
])
def test_journal_replay_skips_are_allowed_only_for_earlier_closes_of_the_demo_project(tmp_path, closes_at, project,
                                                                                      count, clean):
    _world(tmp_path, alerts=[{"kind": "ygg-rehydrate-skipped", "count": count, "ts": "2026-11-23T07:00:00Z"}])
    text = (tmp_path / JOURNAL).read_text().replace("2026-11-23T06:59:00Z", closes_at)
    (tmp_path / JOURNAL).write_text(text.replace("project: alpha-p1", "project: " + project))
    problems, _ = run.agent_findings(tmp_path, final=True)
    assert problems == ([] if clean else ["Yggdrasil skipped journal entries it couldn't read"])


def test_an_activation_of_another_project_in_its_record_fails(tmp_path):
    _world(tmp_path, receipt={"project": "beta-p1"})
    problems, _ = run.agent_findings(tmp_path, final=True)
    assert "pm1su1's run belongs to another programme or project than the demo's" in problems


@pytest.mark.parametrize("journal", ["tracker/_ygg/journal.md", "tracker/initiatives/beta/documents/journal.md"])
def test_an_activation_journaled_outside_the_demo_programme_fails(tmp_path, journal):
    _world(tmp_path)
    (tmp_path / journal).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / journal).write_text(_journal_text([{"kind": "activation", "actor": "ygg-app", "key": "act-0003",
                                                    "node": "pm1su1", "phase": "reserved"}]))
    problems, _ = run.agent_findings(tmp_path, final=True)
    assert "pm1su1's run belongs to another programme or project than the demo's" in problems


def test_a_run_with_no_activation_of_the_demo_project_fails(tmp_path):
    _world(tmp_path, agents=("pm1",), receipt={"programme": "beta", "project": "beta-p1"})
    problems, _ = run.agent_findings(tmp_path, final=True)
    assert "no agent of the demo's project ran" in problems


@pytest.mark.parametrize("reason, plain", [
    ("activation act-0002 has no recorded agent identity", "an agent's run has no recorded agent identity"),
    ("state/activations/act-0003.json can't be read (PermissionError: [Errno 13] Permission denied: "
     "'/tmp/x/state/activations/act-0003.json')", "an agent's record can't be read (PermissionError)"),
    ("state/merge-checks/mc-1a2b.json is starting but holds a pid", "a merge check's record is starting but holds a pid"),
    ("merge check mc-1a2b is still starting, so its job can't be accounted for",
     "a merge check is still starting, so its job can't be accounted for"),
    ("state/demo-groups.json can't be written (OSError: [Errno 28] No space left on device)",
     "the list of process groups the demo signalled can't be written (OSError)"),
    ("process 4242 still running", "process 4242 still running"),  # nothing to change
])
def test_teardown_reasons_name_the_file_role_not_its_path(reason, plain):
    assert run.plain_reason(reason) == plain


def test_a_cleanup_failure_line_names_no_activation(tmp_path, monkeypatch, capsys):
    driver, root = _flowing_driver(tmp_path, monkeypatch)
    monkeypatch.setattr(run, "stop_everything",
                        lambda folder, env, **kwargs: "activation act-0002 has no recorded agent identity")
    assert driver.run() == 1
    captured = capsys.readouterr()
    assert "demo: failed at cleanup: an agent's run has no recorded agent identity" in captured.err
    assert "act-" not in captured.err and root.exists()


def test_a_journal_entry_that_repeats_a_key_fails(tmp_path):
    """A repeated key would let a later value hide an earlier one, such as an exit code of 1 followed by 0."""
    _world(tmp_path)
    _append(tmp_path, JOURNAL, "```yaml\nkind: activation\nactor: ygg-app\nkey: act-0003\nnode: pm1su1\n"
                               "phase: exited\nexit_code: 1\nexit_code: 0\n```\n")
    problems, _ = run.agent_findings(tmp_path, final=True)
    assert "the programme journal has an entry that can't be read" in problems


@pytest.mark.parametrize("key_line", ["", "key: \n", "key: 7\n"], ids=["missing", "empty", "not-text"])
def test_an_activation_entry_without_its_key_fails(tmp_path, key_line):
    _world(tmp_path)
    _append(tmp_path, JOURNAL, "```yaml\nkind: activation\nactor: ygg-app\n" + key_line +
            "node: pm1su1\nphase: exited\nexit_code: 1\n```\n")
    problems, _ = run.agent_findings(tmp_path, final=True)
    assert "the programme journal has an activation entry without its key" in problems


def test_the_demo_codex_sessions_get_a_keyless_provider_route(tmp_path, monkeypatch):
    """The demo's Codex reviewer is the scripted stand-in on an unprobed version: its own user home names a
    provider with no key, so its sessions have a sign-in route and raise no missing-sign-in alert."""
    from yggdrasil import codexlaunch, codexsignin
    data = tomllib.loads(run.CODEX_CONFIG)
    table = data["model_providers"][data["model_provider"]]
    assert "env_key" not in table and "env_http_headers" not in table and "http_headers" not in table
    assert table["base_url"].startswith("http://127.0.0.1:")
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(run.CODEX_CONFIG)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    name, kept = codexlaunch.user_provider()
    assert name == data["model_provider"]
    assert codexsignin.route({}, kept, False, version="an unknown Codex version")[0] == "provider"
