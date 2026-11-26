"""v1.1.1, the shared Codex sign-in on the launch side: the links in the hermetic home, the refusals and the
reviewer's status text, the key exclusion in the config and on the command line, who gets CODEX_API_KEY, the
sign-in lease, the links' removal at settlement, and the redaction and lane-check patterns."""

import fcntl
import inspect
import json
import os
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from yggdrasil import clones, codexlaunch, codexlease, codexsignin, config, launcher, lanecheck, lifecycle, relay, t3
from yggdrasil import util
from yggdrasil.util import YggError


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A user HOME whose default login folder L is `HOME/.config/yggdrasil/agent-codex` (not created), a state
    root, a TMPDIR outside both, and a launching CODEX_HOME holding a login of its own that must never be read."""
    user = tmp_path / "home"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.delenv("YGG_CONFIG", raising=False)
    monkeypatch.delenv("YGG_PROFILE", raising=False)
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmpdir"))
    (tmp_path / "tmpdir").mkdir()
    source = tmp_path / "codex-home"
    source.mkdir()
    (source / "auth.json").write_text('{"x": 1}')
    monkeypatch.setenv("CODEX_HOME", str(source))
    monkeypatch.setattr(config, "get", lambda key, default=None: default)
    monkeypatch.setattr(config, "profile", lambda: {})
    return user


def _login(user: Path, *, auth: bool = True) -> Path:
    folder = user / ".config" / "yggdrasil" / "agent-codex"
    folder.mkdir(parents=True, mode=0o700)
    os.chmod(folder, 0o700)
    if auth:
        (folder / "auth.json").write_text('{"tokens": {"refresh_token": "r1"}}')
        os.chmod(folder / "auth.json", 0o600)
    return folder


def _refusal(error) -> str:
    return error.value.message


# ---------------------------------------------------------------- the links

def test_a_safe_login_is_linked_with_exact_targets_and_never_copied(home):
    login = _login(home)
    written = codexlaunch.write_home("act-0001")
    folder = written["home"]
    assert written["links"] is True and written["sign_in"] is None
    assert sorted(os.listdir(folder)) == ["auth.json", "auth.lock", "config.toml"]
    assert os.readlink(folder / "auth.json") == str(login) + "/auth.json"
    assert os.readlink(folder / "auth.lock") == str(login) + "/auth.lock"
    lock = os.stat(login / "auth.lock", follow_symlinks=False)
    assert stat.S_ISREG(lock.st_mode) and lock.st_size == 0 and stat.S_IMODE(lock.st_mode) == 0o600
    assert (login / "auth.json").read_text() == '{"tokens": {"refresh_token": "r1"}}'
    assert (folder / "config.toml").read_bytes() != b'{"x": 1}'


def test_no_login_folder_means_no_links_and_the_launching_login_is_never_read(home, monkeypatch):
    read = []
    original = Path.read_bytes

    def spy(self):
        read.append(str(self))
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    written = codexlaunch.write_home("act-0002")
    assert written["links"] is False and written["sign_in"] is None
    assert os.listdir(written["home"]) == ["config.toml"]
    assert not any(p.endswith("auth.json") for p in read)
    assert not (home / ".config" / "yggdrasil" / "agent-codex").exists()  # launches never create L


def test_a_login_folder_without_auth_json_gets_no_links(home):
    login = _login(home, auth=False)
    written = codexlaunch.write_home("act-0003")
    assert written["links"] is False
    assert os.listdir(written["home"]) == ["config.toml"]
    assert not (login / "auth.lock").exists()


def test_an_old_link_is_replaced_by_rename_never_opened(home, tmp_path):
    login = _login(home)
    folder = codexlaunch.home_path("act-0004")
    folder.mkdir(parents=True, mode=0o700)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("keep")
    os.symlink(str(elsewhere), folder / "auth.json")
    codexlaunch.write_home("act-0004")
    assert os.readlink(folder / "auth.json") == str(login) + "/auth.json"
    assert elsewhere.read_text() == "keep"
    assert sorted(os.listdir(folder)) == ["auth.json", "auth.lock", "config.toml"]  # no temporary name left


def test_the_leftover_allowlist_is_config_and_the_two_links(home):
    folder = codexlaunch.home_path("act-0005")
    folder.mkdir(parents=True, mode=0o700)
    (folder / "sessions").mkdir()
    with pytest.raises(YggError) as caught:
        codexlaunch.write_home("act-0005")
    assert "holds sessions" in _refusal(caught)


# ---------------------------------------------------------------- refusals and the reviewer's status

def test_a_linked_auth_json_refuses_a_builder(home, tmp_path):
    login = _login(home, auth=False)
    target = tmp_path / "target.json"
    target.write_text('{"t": 1}')
    os.symlink(str(target), login / "auth.json")
    with pytest.raises(YggError) as caught:
        codexlaunch.write_home("act-0006")
    assert caught.value.code == util.EXIT_REFUSED
    assert "launch precondition: " + str(login) + "/auth.json is not a regular file of yours" in _refusal(caught)
    assert target.read_text() == '{"t": 1}'


def test_a_hard_linked_auth_json_refuses_a_builder(home, tmp_path):
    login = _login(home)
    os.link(login / "auth.json", tmp_path / "second-name")
    with pytest.raises(YggError) as caught:
        codexlaunch.write_home("act-0007")
    assert "/auth.json is not a regular file of yours" in _refusal(caught)


def test_a_linked_auth_lock_refuses_a_builder(home, tmp_path):
    login = _login(home)
    target = tmp_path / "lock-target"
    target.write_text("keep")
    os.symlink(str(target), login / "auth.lock")
    with pytest.raises(YggError) as caught:
        codexlaunch.write_home("act-0008")
    assert "launch precondition: " + str(login) + "/auth.lock is not a regular file of yours" in _refusal(caught)
    assert target.read_text() == "keep"


def test_a_linked_or_shared_login_folder_refuses_a_builder(home, tmp_path):
    real = tmp_path / "realL"
    real.mkdir(mode=0o700)
    (real / "auth.json").write_text("{}")
    parent = home / ".config" / "yggdrasil"
    parent.mkdir(parents=True)
    os.symlink(str(real), parent / "agent-codex")
    with pytest.raises(YggError) as caught:
        codexlaunch.write_home("act-0009")
    assert "launch precondition: " + str(parent / "agent-codex") + " is not a real folder of yours" in \
        _refusal(caught)
    (parent / "agent-codex").unlink()
    login = _login(home)
    os.chmod(login, 0o750)
    with pytest.raises(YggError) as caught:
        codexlaunch.write_home("act-0010")
    assert "is not a real folder of yours" in _refusal(caught)


def test_a_reviewer_is_never_refused_by_the_login_checks(home, tmp_path):
    login = _login(home, auth=False)
    os.symlink(str(tmp_path / "x"), login / "auth.json")
    written = codexlaunch.write_home("act-0011", reviewer=True)
    assert written["links"] is False
    assert written["sign_in"] == "launch precondition: " + str(login) + "/auth.json is not a regular file of yours"
    assert os.listdir(written["home"]) == ["config.toml"]


def test_an_unprobed_reviewer_gets_no_links_and_its_status_text(home):
    _login(home)
    written = codexlaunch.write_home("act-0012", reviewer=True, probed=False)
    assert written["links"] is False and written["sign_in"] == "unprobed: no shared sign-in"
    assert os.listdir(written["home"]) == ["config.toml"]


def test_a_login_folder_inside_the_state_root_refuses_every_codex_launch(home, monkeypatch):
    inside = Path(os.environ["YGG_STATE_ROOT"]) / "codex-login"
    monkeypatch.setattr(config, "get", lambda key, default=None:
                        str(inside) if key == "agent_codex_login_dir" else default)
    inside.mkdir()
    with pytest.raises(YggError) as caught:
        codexlaunch.checked_login(probed=False)
    assert "refused: agent_codex_login_dir" in _refusal(caught) and "lies inside" in _refusal(caught)


def test_a_forbidden_login_folder_that_does_not_exist_gives_no_links_and_no_refusal(home, monkeypatch):
    inside = Path(os.environ["YGG_STATE_ROOT"]) / "codex-login"
    monkeypatch.setattr(config, "get", lambda key, default=None:
                        str(inside) if key == "agent_codex_login_dir" else default)
    assert codexlaunch.checked_login(probed=True) is None
    assert codexlaunch.checked_login(probed=False) is None
    assert not os.path.lexists(inside)
    inside.symlink_to(Path(os.environ["YGG_STATE_ROOT"]) / "nowhere")
    with pytest.raises(YggError) as caught:
        codexlaunch.checked_login(probed=False)
    assert "lies inside" in _refusal(caught)


def test_status_shows_the_reviewers_sign_in_text(home):
    folder = Path(os.environ["YGG_STATE_ROOT"]) / "activations"
    folder.mkdir()
    record = {"role": "reviewer", "cli": "codex", "review": "rv-0001", "project": "alpha-p1",
              "activation": "act-0013", "codex_pin": {"checked": True, "probed": False, "version": "codex-cli 0.171.0"},
              "codex_sign_in": "unprobed: no shared sign-in"}
    (folder / "act-0013.json").write_text(json.dumps(record))
    lines = codexlaunch.unprobed_review_lines({"alpha-p1"})
    assert lines == ["review rv-0001 (act-0013): unprobed, codex-cli 0.171.0",
                     "review rv-0001 (act-0013): unprobed: no shared sign-in"]


def test_preflight_refuses_a_builder_with_an_unsafe_login_before_anything_is_reserved(home, monkeypatch):
    login = _login(home)
    os.chmod(login, 0o755)
    monkeypatch.setattr(launcher.tracker, "programme", lambda name: {"kind": "git-repo"})
    monkeypatch.setattr(codexlaunch, "check_builder", lambda prefix=codexlaunch.LAUNCH: {"probed": True})
    node = {"id": "pm1", "role": "pm", "programme": "alpha"}
    with pytest.raises(YggError) as caught:
        launcher.codex_preflight(node, {"pm1": node}, "codex")
    assert caught.value.code == util.EXIT_REFUSED and "is not a real folder of yours" in _refusal(caught)


# ---------------------------------------------------------------- settlement

def test_drop_links_unlinks_by_name_and_never_follows(home):
    login = _login(home)
    folder = codexlaunch.write_home("act-0014")["home"]
    codexlaunch.drop_links("act-0014")
    assert os.listdir(folder) == ["config.toml"]
    assert (login / "auth.json").read_text() == '{"tokens": {"refresh_token": "r1"}}'
    assert (login / "auth.lock").exists()
    codexlaunch.drop_links("act-0014")  # harmless to repeat
    codexlaunch.drop_links("act-9999")  # no Codex home at all


@pytest.mark.parametrize("already", [False, True])
def test_every_terminal_fact_drops_the_links(home, monkeypatch, already):
    dropped = []
    monkeypatch.setattr(codexlaunch, "drop_links", lambda activation: dropped.append(activation))
    monkeypatch.setattr(lifecycle, "_programme", lambda node_id: "alpha")
    monkeypatch.setattr(lifecycle, "facts", lambda programme, activation:
                        [{"phase": "exited"}] if already else [{"phase": "spawned"}])
    from yggdrasil import journal
    monkeypatch.setattr(journal, "add", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "update_receipt", lambda activation, **fields: {})
    for phase in ("exited", "aborted", "released"):
        lifecycle.commit("pm1tl1", "act-0015", phase)
    lifecycle.commit("pm1tl1", "act-0015", "reserved")
    assert dropped == ["act-0015"] * 3


def test_the_supervisor_drops_the_links_at_its_end_and_releases_the_lease():
    source = inspect.getsource(launcher._supervise)
    loop = source.index("code = _supervise_loop(")
    assert source.index("release_sign_in_lease(lease)", loop) < source.index("lifecycle.commit(data[\"node\"]")
    assert source.index("_drop_login_links(activation)", loop) < source.index("lifecycle.commit(data[\"node\"]")
    refused = source.index("if process is None:")
    assert source.index("release_sign_in_lease(lease)", refused) < source.index("abort_activation(", refused)
    assert source.index("_drop_login_links(activation)", refused) < source.index("abort_activation(", refused)


# ---------------------------------------------------------------- the sign-in lease

def test_the_lease_is_taken_after_the_gate_and_outside_both_locks():
    source = inspect.getsource(launcher._supervise)
    gate = source.index('"spawned", supervisor_pid=')
    step_one = source.index("refusal = pinned_refusal or clone_refusal(data) or hold_reason(node, activation)")
    lease = source.index("lease = take_sign_in_lease(data)")
    creation = source.index("with util.locked(util.CREATION_LOCK):")
    assert gate < step_one < lease < creation
    # Taken at the outer level of the function, so neither the journal lock nor the creation lock is held.
    line = source[source.rindex("\n", 0, lease) + 1:lease]
    assert line == " " * 8
    assert 'data.get("codex_login_links")' in source[step_one:lease]


def test_the_lease_is_shared_not_inherited_and_blocks_an_exclusive_one(home):
    _login(home)
    first = launcher.take_sign_in_lease()
    second = launcher.take_sign_in_lease()
    try:
        assert first is not None and second is not None
        assert not os.get_inheritable(first)
        assert codexsignin.take_lease_exclusive_nb() is None
    finally:
        launcher.release_sign_in_lease(first)
        launcher.release_sign_in_lease(second)
    held = codexsignin.take_lease_exclusive_nb()
    assert held is not None
    os.close(held)


def test_no_login_means_no_lease(home):
    assert launcher.take_sign_in_lease() is None


def _exclusive_free(login: Path) -> bool:
    """Whether logout's exclusive try on this login folder's lease file would succeed now."""
    descriptor = os.open(str(login / ".ygg-sign-in.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)


def _until(check, seconds=30.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def agent():
    """A stand-in for a surviving Codex agent: a live process in its own group with its recorded start."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    started = None
    for _ in range(100):
        started = util.process_start(process.pid)
        if started:
            break
        time.sleep(0.05)
    yield process, started
    if process.poll() is None:
        process.kill()
    process.wait(timeout=30)


def _recovered_record(activation, login, process, started, **extra):
    folder = Path(os.environ["YGG_STATE_ROOT"]) / "launch" / activation
    folder.mkdir(parents=True, exist_ok=True)
    return dict({"node": "pm1tl1", "activation": activation, "cli": "codex", "role": "tl",
                 "codex_login_links": True, "codex_login_dir": str(login), "pid": process.pid,
                 "process_started_at": started, "pinned": {"YGG_STATE_ROOT": os.environ["YGG_STATE_ROOT"]}},
                **extra)


def test_recovery_restores_the_lease_of_an_agent_whose_pid_was_recorded(home, agent, monkeypatch):
    """The supervisor died after recording Codex's pid: every recovery pass that finds the agent alive keeps
    exactly one holder, which keeps the lease until the agent is gone."""
    login = _login(home)
    process, started = agent
    data = _recovered_record("act-0031", login, process, started)
    found = [{"phase": "reserved"}, {"phase": "spawned", "supervisor_pid": 1, "supervisor_started_at": "x"}]
    holders = []
    real = codexlaunch.ensure_lease_holder
    monkeypatch.setattr(codexlaunch, "ensure_lease_holder",
                        lambda *args: holders.append(real(*args)) or holders[-1])
    assert _exclusive_free(login)
    assert lifecycle.settle("pm1tl1", "act-0031", data, found) == "running"
    assert holders == [True]
    assert _until(lambda: not _exclusive_free(login))
    for _ in range(3):  # later ticks start no second holder
        assert lifecycle.settle("pm1tl1", "act-0031", data, found) == "running"
    assert holders == [True, False, False, False]
    process.kill()
    process.wait(timeout=30)
    assert _until(lambda: _exclusive_free(login))  # the holder let go once the agent was gone


def test_adoption_of_an_unrecorded_agent_starts_the_holder_too():
    source = inspect.getsource(lifecycle.settle)
    adopted = source.index('"adopted_at": util.iso()')
    assert source.index("codexlaunch.ensure_lease_holder(activation, data, live[0]", adopted) > adopted
    running = source.index('if agent == "alive":')
    assert source.index("codexlaunch.ensure_lease_holder(activation, data, data.get(\"pid\")", running) < \
        source.index('return "running"', running)


def test_a_live_supervisor_keeps_the_holder_lock_so_no_holder_starts(home, agent, monkeypatch):
    login = _login(home)
    process, started = agent
    data = _recovered_record("act-0032", login, process, started)
    spawned = []
    monkeypatch.setattr(codexlaunch.subprocess, "Popen", lambda *args, **kwargs: spawned.append(args))
    held = launcher.take_sign_in_lease(data)
    try:
        claim, lease = held
        assert claim is not None and lease is not None
        assert not os.get_inheritable(claim) and not os.get_inheritable(lease)
        assert not _exclusive_free(login)
        assert codexlaunch.ensure_lease_holder("act-0032", data, process.pid, started) is False
        assert spawned == []
    finally:
        launcher.release_sign_in_lease(held)
    assert _exclusive_free(login)
    assert codexlaunch.ensure_lease_holder("act-0032", data, process.pid, started) is True
    assert len(spawned) == 1


def test_the_holder_gets_the_recorded_folder_and_absolute_configuration(home, agent, monkeypatch, tmp_path):
    """Recovery run from another folder with relative configuration paths: the holder is given the folder recorded
    at launch and the launch's pinned absolute paths, never the conductor's relative ones."""
    custom = tmp_path / "custom-login"
    custom.mkdir(mode=0o700)
    process, started = agent
    pinned = {"YGG_STATE_ROOT": os.environ["YGG_STATE_ROOT"], "YGG_CONFIG": str(tmp_path / "abs" / "config.yaml"),
              "YGG_PROFILE": str(tmp_path / "abs" / "profile.yaml")}
    data = _recovered_record("act-0033", custom, process, started, pinned=pinned)
    monkeypatch.setenv("YGG_CONFIG", "./settings.yaml")
    monkeypatch.setenv("YGG_PROFILE", "profiles/p.yaml")
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(codexlaunch.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)))
    assert codexlaunch.ensure_lease_holder("act-0033", data, process.pid, started) is True
    argv, kwargs = calls[0]
    assert argv[-4:] == [str(codexlaunch.holder_lock_path("act-0033")), str(custom), str(process.pid), started]
    assert kwargs["env"]["YGG_CONFIG"] == pinned["YGG_CONFIG"]
    assert kwargs["env"]["YGG_PROFILE"] == pinned["YGG_PROFILE"]
    assert Path(kwargs["cwd"]).is_absolute()


def test_the_holder_process_leases_the_recorded_folder_whatever_the_configuration(home, agent, tmp_path):
    """A real holder, started with a relative YGG_CONFIG naming another login folder and from another folder:
    it leases the folder on its command line, not the default nor the configured one."""
    _login(home)  # the default folder also holds a login; it must stay free
    custom = tmp_path / "custom-login"
    custom.mkdir(mode=0o700)
    (custom / "auth.json").write_text("{}")
    os.chmod(custom / "auth.json", 0o600)
    (tmp_path / "elsewhere.yaml").write_text("agent_codex_login_dir: " + str(tmp_path / "other") + "\n")
    process, started = agent
    lock = codexlaunch.holder_lock_path("act-0034")
    lock.parent.mkdir(parents=True)
    environment = dict(os.environ, YGG_CONFIG="./elsewhere.yaml")
    holder = subprocess.Popen([sys.executable, "-I", "-m", "yggdrasil.codexlease", str(lock), str(custom),
                               str(process.pid), started], cwd=str(tmp_path), env=environment)
    try:
        assert _until(lambda: not _exclusive_free(custom))
        assert _exclusive_free(home / ".config" / "yggdrasil" / "agent-codex")
        assert not (tmp_path / "other").exists()
        process.kill()
        assert holder.wait(timeout=30) == 0
        assert _exclusive_free(custom)
    finally:
        if holder.poll() is None:
            holder.kill()


def test_a_second_holder_of_the_same_activation_exits_at_once(home, tmp_path, monkeypatch):
    login = _login(home)
    lock = codexlaunch.holder_lock_path("act-0035")
    lock.parent.mkdir(parents=True)
    claim = codexlaunch.claim_holder(lock)
    try:
        monkeypatch.setattr(codexlease, "_gone", lambda pid, started: pytest.fail("a second holder kept going"))
        assert codexlease.hold(lock, login, 12345, "start") == 0
        assert _exclusive_free(login)
    finally:
        os.close(claim)


def test_the_holder_takes_the_lease_back_after_a_logout_lets_go(home, monkeypatch):
    login = _login(home)
    lock = codexlaunch.holder_lock_path("act-0036")
    lock.parent.mkdir(parents=True)
    exclusive = codexsignin.take_lease_exclusive_nb()
    states = iter([False, False, False, True])
    seen = []

    def gone(pid, started):
        nonlocal exclusive
        if exclusive is not None and len(seen) == 1:
            os.close(exclusive)  # the logout ends
            exclusive = None
        seen.append(_exclusive_free(login))
        return next(states)

    monkeypatch.setattr(codexlease, "_gone", gone)
    monkeypatch.setattr(codexlease, "POLL_SECONDS", 0)
    assert codexlease.hold(lock, login, 12345, "start") == 0
    assert seen[-1] is False  # held again after the logout let go
    assert _exclusive_free(login)


def test_the_holder_module_needs_four_arguments(home):
    result = subprocess.run([sys.executable, "-I", "-m", "yggdrasil.codexlease", "act-1", "1", "x"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1, result.stderr


# ---------------------------------------------------------------- the config and command line

def test_the_hermetic_config_keeps_the_keys_from_the_agents_commands(home, tmp_path, monkeypatch):
    source = Path(os.environ["CODEX_HOME"])
    (source / "config.toml").write_text(
        'model_provider = "hub"\n[model_providers.hub]\nname = "hub"\nbase_url = "http://127.0.0.1:1/v1"\n'
        'env_key = "HUB_KEY"\nwire_api = "responses"\nenv_http_headers = { "X-Org" = "HUB_ORG", "X-Proj" = "HUB_P" }\n')
    written = codexlaunch.write_home("act-0016")
    data = tomllib.loads((written["home"] / "config.toml").read_text())
    assert data["cli_auth_credentials_store"] == "file"
    assert data["shell_environment_policy"] == {
        "exclude": ["CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "HUB_KEY", "HUB_ORG", "HUB_P"]}
    assert written["excluded"] == data["shell_environment_policy"]["exclude"]
    reviewer = tomllib.loads(codexlaunch.config_text(reviewer=True))
    assert reviewer["shell_environment_policy"]["exclude"][:3] == ["CODEX_API_KEY", "OPENAI_API_KEY",
                                                                   "CODEX_ACCESS_TOKEN"]


def test_the_default_provider_excludes_the_three_codex_keys(home):
    data = tomllib.loads(codexlaunch.config_text())
    assert data["shell_environment_policy"] == {"exclude": ["CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"]}


def test_the_builder_command_line_repeats_the_store_and_the_exclusion(home):
    data = {"node": "pm1tl1", "activation": "act-0017", "role": "tl", "cli": "codex", "model": "m",
            "box": "/b", "relay": "/r", "codex_pin": {"path": "/opt/codex"},
            "codex_exclude": ["CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "HUB_KEY"]}
    argv = launcher.codex_builder_command(data)
    values = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-c"]
    assert 'cli_auth_credentials_store="file"' in values
    assert 'shell_environment_policy.exclude=["CODEX_API_KEY","OPENAI_API_KEY","CODEX_ACCESS_TOKEN","HUB_KEY"]' \
        in values
    excluded = next(v for v in values if v.startswith("shell_environment_policy.exclude="))
    assert tomllib.loads("x = " + excluded.split("=", 1)[1])["x"] == data["codex_exclude"]
    assert "--strict-config" in argv


# ---------------------------------------------------------------- CODEX_API_KEY

DATA = {"node": "pm1tl1", "activation": "act-0018", "programme": "alpha", "project": "alpha-p1", "model": "m",
        "effort": "", "pinned": {}}


def test_a_codex_builder_gets_codex_api_key(home, monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "k-b-1")
    environment = launcher.child_env(dict(DATA, cli="codex", role="tl"))
    assert environment["CODEX_API_KEY"] == "k-b-1"


def test_a_claude_session_never_gets_codex_api_key_whatever_the_overlays(home, monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "k-c-1")
    monkeypatch.setattr(config, "get", lambda key, default=None:
                        ["CODEX_API_KEY"] if key == "agent_env_passthrough" else default)
    monkeypatch.setattr(config, "profile", lambda: {"launch_env": {"CODEX_API_KEY": "k-c-2"}})
    environment = launcher.child_env(dict(DATA, cli="claude-code", role="tl"))
    assert "CODEX_API_KEY" not in environment


@pytest.mark.parametrize("probed", [True, False])
def test_a_reviewer_gets_codex_api_key_only_on_a_probed_version(home, monkeypatch, probed):
    monkeypatch.setenv("CODEX_API_KEY", "k-r-1")
    monkeypatch.setattr(config, "profile", lambda: {"launch_env": {"CODEX_API_KEY": "k-r-2"}})
    data = dict(DATA, cli="codex", role="reviewer", activation="", codex_pin={"probed": probed, "checked": True})
    environment = launcher.child_env(data)
    assert environment.get("CODEX_API_KEY") == ("k-r-2" if probed else None)


def test_the_session_environment_for_doctor_follows_passthrough_and_launch_env(home, monkeypatch):
    monkeypatch.setenv("HUB_KEY", "hk-1")
    monkeypatch.setenv("CODEX_API_KEY", "k-1")
    environment = launcher.codex_session_env()
    assert "HUB_KEY" not in environment and environment["CODEX_API_KEY"] == "k-1"
    monkeypatch.setattr(config, "get", lambda key, default=None:
                        ["HUB_KEY"] if key == "agent_env_passthrough" else default)
    assert launcher.codex_session_env()["HUB_KEY"] == "hk-1"
    monkeypatch.delenv("HUB_KEY")
    monkeypatch.setattr(config, "profile", lambda: {"launch_env": {"HUB_KEY": "hk-2"}})
    assert launcher.codex_session_env()["HUB_KEY"] == "hk-2"
    reviewer = launcher.codex_session_env({"role": "reviewer", "codex_pin": {"probed": False}})
    assert "CODEX_API_KEY" not in reviewer
    assert not (Path(os.environ["YGG_STATE_ROOT"]) / "reviews").exists()


def test_a_relayed_ygg_never_gets_codex_api_key(home, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_API_KEY", "k-relay-1")
    server = relay.Server.__new__(relay.Server)
    server.data = dict(DATA, cli="codex", role="tl")
    server.activation = "act-0018"
    server.sequence = 0
    environment = server._environment("r-1", tmp_path / "req")
    assert "CODEX_API_KEY" not in environment


def test_no_t3_process_gets_codex_api_key(home, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_API_KEY", "k-t3-1")
    binary = tmp_path / "t3"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setattr(t3, "binary", lambda: str(binary))
    seen = []

    def record_run(*args, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, "", "")

    class Recorded:
        def __init__(self, *args, **kwargs):
            seen.append(kwargs["env"])

    monkeypatch.setattr(t3.subprocess, "run", record_run)
    monkeypatch.setattr(t3.subprocess, "Popen", Recorded)
    t3._exec(("--version",))
    t3._spawn(("thread", "list"))
    assert len(seen) == 2 and not any("CODEX_API_KEY" in e for e in seen)
    assert t3.CALL_ENV_DENIED.match("CODEX_API_KEY") and not t3.CALL_ENV_DENIED.match("CODEX_API_KEY_X")


def test_the_sandboxed_helper_environment_holds_no_key(home, monkeypatch, tmp_path):
    for name, value in (("CODEX_API_KEY", "k-h-1"), ("OPENAI_API_KEY", "k-h-2"), ("CODEX_ACCESS_TOKEN", "k-h-3"),
                        ("HUB_KEY", "k-h-4"), ("HUB_ORG", "k-h-5")):
        monkeypatch.setenv(name, value)
    for test_mode in ("1", None):
        if test_mode:
            monkeypatch.setenv("YGG_TEST_MODE", test_mode)
        else:
            monkeypatch.delenv("YGG_TEST_MODE", raising=False)
        environment = clones.tender_env({"box": str(tmp_path / "box")})
        assert not set(environment) & {"CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "HUB_KEY", "HUB_ORG"}
        assert not any(v.startswith("k-h-") for v in environment.values())
    assert "tender_env(record)" in inspect.getsource(clones.run_tender)


# ---------------------------------------------------------------- redaction and the lane check

JWT = "eyJ" + "hbGciOiJSUzI1NiJ9" + "." + "eyJzdWIiOiJ1c2VyLTEyMyJ9" + "." + "c2lnbmF0dXJlLXBhcnQ"
LEGACY = "sk-" + "Ab1" * 11
REFRESH = '"refresh_token": "' + "rt_" + "x" * 30 + '"'


def test_redaction_removes_a_json_web_token():
    assert util.redact("token " + JWT + " end") == "token [REDACTED] end"
    assert util.redact("eyJhbGciOiJIUzI1NiJ9." + "a" * 9) == "eyJhbGciOiJIUzI1NiJ9." + "a" * 9  # too short


def test_redaction_removes_a_legacy_openai_key():
    assert util.redact("key=" + LEGACY) == "key=[REDACTED]"
    assert util.redact("sk-" + "a" * 31) == "sk-" + "a" * 31
    assert util.redact("task-" + "a" * 40) == "task-" + "a" * 40


def test_redaction_removes_a_refresh_token_value_only():
    assert util.redact("{" + REFRESH + "}") == '{"refresh_token": "[REDACTED]"}'
    escaped = '{\\"refresh_token\\":\\"' + "y" * 25 + '\\"}'
    assert util.redact(escaped) == '{\\"refresh_token\\":\\"[REDACTED]\\"}'
    assert util.redact('{"refresh_token": "r1"}') == '{"refresh_token": "r1"}'


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


class _NoConfig:
    @staticmethod
    def get(key, default=None):
        return default


ADDS = "adds a token-shaped string (a JSON Web Token, an sk- key or a refresh_token); remove it"


def _lane_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    monkeypatch.setattr(lanecheck, "config", _NoConfig)
    monkeypatch.setattr(lanecheck, "denylist", lambda: [])
    monkeypatch.setattr(lanecheck, "_repo_entry", lambda project, repo_: None)
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    return repo


def test_the_lane_check_refuses_a_commit_holding_a_copied_auth_json(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    monkeypatch.setattr(lanecheck, "config", _NoConfig)
    monkeypatch.setattr(lanecheck, "denylist", lambda: [])
    monkeypatch.setattr(lanecheck, "_repo_entry", lambda project, repo_: None)
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "L0")
    copied = {"OPENAI_API_KEY": None, "auth_mode": "chatgpt",
              "tokens": {"id_token": JWT, "access_token": JWT, "refresh_token": "rt_" + "z" * 40,
                         "account_id": "acct"}, "last_refresh": "2026-11-26T08:00:00Z"}
    (repo / "notes").mkdir()
    (repo / "notes" / "auth.json").write_text(json.dumps(copied, indent=2))
    _git(repo, "add", ".")
    problems = lanecheck.staged_problems(str(repo))
    assert "notes/auth.json: line 5 " + ADDS in problems
    # Each shape alone is enough.
    for text in (JWT, LEGACY, "{" + REFRESH + "}"):
        (repo / "notes" / "auth.json").write_text(text + "\n")
        _git(repo, "add", ".")
        assert "notes/auth.json: line 1 " + ADDS in lanecheck.staged_problems(str(repo))


def test_the_lease_spans_state_roots_that_share_one_login_folder(home, tmp_path):
    """A supervisor of another state root (same HOME, so the same login folder) holds the lease in its own
    process: logout's exclusive try from this state root fails until that process lets go."""
    _login(home)
    other = tmp_path / "state2"
    other.mkdir()
    ready, release = tmp_path / "held", tmp_path / "release"
    holder = ("import os, sys, time\n"
              "from yggdrasil import launcher\n"
              "lease = launcher.take_sign_in_lease()\n"
              "open(sys.argv[1], 'w').write('none' if lease is None else 'held')\n"
              "while not os.path.exists(sys.argv[2]):\n"
              "    time.sleep(0.05)\n")
    environment = dict(os.environ, YGG_STATE_ROOT=str(other))
    process = subprocess.Popen([sys.executable, "-c", holder, str(ready), str(release)], env=environment,
                               cwd=str(tmp_path))
    try:
        for _ in range(600):
            if ready.exists() and ready.read_text():
                break
            assert process.poll() is None
            time.sleep(0.05)
        assert ready.read_text() == "held"
        assert os.environ["YGG_STATE_ROOT"] != str(other)
        assert codexsignin.take_lease_exclusive_nb() is None
    finally:
        release.write_text("go")
        process.wait(timeout=30)
    held = codexsignin.take_lease_exclusive_nb()
    assert held is not None
    os.close(held)


def test_a_reviewer_is_refused_too_when_the_login_folder_lies_in_a_forbidden_place(home, monkeypatch):
    inside = Path(os.environ["YGG_STATE_ROOT"]) / "codex-login"
    monkeypatch.setattr(config, "get", lambda key, default=None:
                        str(inside) if key == "agent_codex_login_dir" else default)
    inside.mkdir()
    for probed in (True, False):
        with pytest.raises(YggError) as caught:
            codexlaunch.write_home("act-0%d09" % probed, reviewer=True, probed=probed)
        assert "lies inside" in _refusal(caught)


def test_a_reviewer_on_a_version_probed_elsewhere_is_told_the_platform(home, monkeypatch):
    _login(home)
    monkeypatch.setattr(codexlaunch, "current_platform", lambda: "linux")
    written = codexlaunch.write_home("act-0071", reviewer=True, probed=False, version="codex-cli 0.170.0")
    assert written["links"] is False
    assert written["sign_in"] == ("unprobed: no shared sign-in; Codex's shared sign-in is not probed on linux; "
                                  "use a model provider with its own key")
    written = codexlaunch.write_home("act-0072", reviewer=True, probed=False, version="codex-cli 0.171.0")
    assert written["sign_in"] == "unprobed: no shared sign-in"


def test_the_launcher_passes_the_reviewers_version_to_the_home():
    source = inspect.getsource(launcher)
    assert 'codexlaunch.write_home(activation, reviewer=node["role"] == "reviewer", probed=probed,' in source
    assert 'version=(codex_pin or {}).get("version"))' in source


# The example token jwt.io shows, split so that no scanner reads this file as holding it.
JWT_IO = ("eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwi"
          "aWF0IjoxNTE2MjM5MDIyfQ" + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
GITHUB = "gh" + "p_" + "A1b2C3d4E5" * 4


def test_an_edit_to_a_file_already_holding_a_token_passes_the_lane_check(tmp_path, monkeypatch):
    repo = _lane_repo(tmp_path, monkeypatch)
    (repo / "tests").mkdir()
    (repo / "tests" / "fixture.py").write_text("TOKEN = '" + JWT_IO + "'\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "L0")
    (repo / "tests" / "fixture.py").write_text("# the jwt.io example\nTOKEN = '" + JWT_IO + "'\nOTHER = 1\n")
    _git(repo, "add", ".")
    assert lanecheck.staged_problems(str(repo)) == []


def test_a_commit_adding_a_token_is_refused_with_its_line_and_never_shows_it(tmp_path, monkeypatch):
    repo = _lane_repo(tmp_path, monkeypatch)
    (repo / "README.md").write_text("x\n")
    (repo / "fixture.txt").write_text(JWT_IO + "\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "L0")
    # An existing file: the old token stays and a different one is added on line 3.
    (repo / "fixture.txt").write_text(JWT_IO + "\nplain\n" + JWT + "\n")
    (repo / "README.md").write_text("x\ny\nkey: " + LEGACY + "\n")
    # A new file: every match counts.
    (repo / "new.json").write_text("{\n  " + REFRESH + "\n}\n")
    _git(repo, "add", ".")
    problems = lanecheck.staged_problems(str(repo))
    assert sorted(problems) == sorted(["fixture.txt: line 3 " + ADDS, "README.md: line 3 " + ADDS,
                                       "new.json: line 2 " + ADDS])
    shown = "\n".join(problems)
    for token in (JWT_IO, JWT, LEGACY, "rt_" + "x" * 30):
        for piece in [token[i:i + 8] for i in range(0, len(token) - 8, 4)]:
            assert piece not in shown
    assert "rt_" not in shown and "eyJ" not in shown and "sk-" not in shown.replace("an sk- key", "")


def test_a_new_file_holding_the_example_token_is_refused(tmp_path, monkeypatch):
    repo = _lane_repo(tmp_path, monkeypatch)
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "L0")
    (repo / "fixture.txt").write_text("one\ntwo\n" + JWT_IO + "\n")
    _git(repo, "add", ".")
    assert lanecheck.staged_problems(str(repo)) == ["fixture.txt: line 3 " + ADDS]


def test_an_older_secret_shape_is_refused_in_the_whole_file_as_before(tmp_path, monkeypatch):
    repo = _lane_repo(tmp_path, monkeypatch)
    (repo / "config.txt").write_text("token = " + GITHUB + "\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "L0")
    (repo / "config.txt").write_text("token = " + GITHUB + "\nother = 1\n")
    _git(repo, "add", ".")
    assert lanecheck.staged_problems(str(repo)) == ["config.txt: content holds a secret-shaped token"]


def test_the_commit_range_check_judges_each_commit_against_its_parent(tmp_path, monkeypatch):
    repo = _lane_repo(tmp_path, monkeypatch)
    (repo / "fixture.txt").write_text(JWT_IO + "\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "L0")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "fixture.txt").write_text("a\n" + JWT_IO + "\n")
    _git(repo, "commit", "-q", "-am", "keep the fixture")
    (repo / "other.txt").write_text("b\n" + JWT + "\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add a token")
    tip = _git(repo, "rev-parse", "HEAD")
    assert lanecheck.range_problems(str(repo), base, tip) == [tip[:12] + ": other.txt: line 2 " + ADDS]
