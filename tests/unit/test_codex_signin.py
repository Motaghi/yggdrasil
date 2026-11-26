"""Yggdrasil's own Codex sign-in: the login folder and its checks, the sign-in lease, the route doctor and the alert
report, and the three `ygg codex` verbs."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from yggdrasil import alertkinds, cli, codexsignin, config, doctor, relay, state
from yggdrasil.util import YggError


def _clear():
    for cached in (config.file_layers, config.profile, config.effective):
        getattr(cached, "cache_clear", lambda: None)()  # a test may have replaced the profile reader


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A home, a user config named by YGG_CONFIG, a temp folder of its own and a fake `codex` that records its
    arguments, its environment and its standard input, and acts as FAKE_CODEX_MODE says."""
    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
    (root / "tmpdir").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(root / "tmpdir"))
    monkeypatch.setenv("YGG_CONFIG", str(root / "config.yaml"))
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    for name in ("YGG_NODE", "YGG_ACTIVATION", "YGG_PROFILE", "YGG_TEST_CODEX_VERSION", "YGG_TEST_PLATFORM",
                 "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_HOME", "YGG_RELAY", "YGG_VIA_RELAY"):
        monkeypatch.delenv(name, raising=False)
    (root / "config.yaml").write_text("schema: ygg.config.v1\n")
    if any(str(root).startswith(r + "/") for r in ("/tmp", "/private/tmp")):
        monkeypatch.setattr(codexsignin, "FIXED_ROOTS", ())  # pytest's own temp folder is under /tmp here
    fake = root / "bin" / "codex"
    fake.parent.mkdir()
    fake.write_text("#!" + sys.executable + "\n" + FAKE)
    fake.chmod(0o755)
    monkeypatch.setenv("YGG_CODEX_BIN", str(fake))
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(root / "codex-calls.jsonl"))
    monkeypatch.chdir(root)
    _clear()
    yield root
    _clear()


FAKE = r'''
import json, os, sys
mode = os.environ.get("FAKE_CODEX_MODE", "")
data = sys.stdin.read() if "--with-api-key" in sys.argv else ""
with open(os.environ["FAKE_CODEX_RECORD"], "a") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ), "stdin": data}) + "\n")
home = os.environ.get("CODEX_HOME", "")
if sys.argv[1:] == ["login", "--device-auth"]:
    with open(os.path.join(home, "auth.json"), "w") as handle:
        handle.write('{"tokens": {"refresh_token": "r1"}}')
elif sys.argv[1:] == ["login", "--with-api-key"]:
    with open(os.path.join(home, "stdin.txt"), "w") as handle:
        handle.write(data)
elif sys.argv[1:] == ["login", "status"]:
    print("Logged in using ChatGPT")
elif sys.argv[1:] == ["logout"]:
    print("Not logged in")
    sys.exit(1)
sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
'''


def _calls(world):
    path = world / "codex-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _login_dir(world) -> Path:
    return world / "home" / ".config" / "yggdrasil" / "agent-codex"


def _plant(world, *, login=True, mode=0o700) -> Path:
    folder = _login_dir(world)
    folder.mkdir(parents=True)
    folder.chmod(mode)
    if login:
        (folder / "auth.json").write_text('{"tokens": {"refresh_token": "r1"}}')
        (folder / "auth.json").chmod(0o600)
    return folder


def _set_dir(world, value):
    (world / "config.yaml").write_text("schema: ygg.config.v1\nagent_codex_login_dir: " + str(value) + "\n")
    _clear()


def _ygg(capfd, *argv):
    code = cli.main(list(argv))
    out, err = capfd.readouterr()
    return code, out, err


# ---------------------------------------------------------------- the setting

def test_the_default_login_folder_is_shipped_and_expanded_without_resolving(world, monkeypatch):
    linked = world / "linked-home"
    linked.symlink_to(world / "home")
    monkeypatch.setenv("HOME", str(linked))
    _clear()
    assert config.get("agent_codex_login_dir") == "~/.config/yggdrasil/agent-codex"
    assert codexsignin.login_dir() == linked / ".config" / "yggdrasil" / "agent-codex"
    assert codexsignin.is_default(codexsignin.login_dir())


def test_a_relative_folder_in_the_users_config_is_made_absolute_but_not_resolved(world):
    (world / "real").mkdir()
    (world / "conf").symlink_to(world / "real")
    (world / "conf" / "config.yaml").write_text("agent_codex_login_dir: sub/login\n")
    os.environ["YGG_CONFIG"] = str(world / "conf" / "config.yaml")
    _clear()
    # The config path itself is resolved like every YGG_CONFIG; the setting's own text is never resolved further.
    assert codexsignin.login_dir() == world / "real" / "sub" / "login"
    assert not codexsignin.is_default(codexsignin.login_dir())


def test_the_setting_is_global_trusted_only_and_a_path_key():
    assert config.SCHEMA["agent_codex_login_dir"] == config.GLOBAL
    assert "agent_codex_login_dir" in config.TRUSTED_ONLY_KEYS
    assert "agent_codex_login_dir" in config.PATH_KEYS


# ---------------------------------------------------------------- the location check

def test_a_folder_outside_every_root_passes(world):
    codexsignin.check_location(_login_dir(world))


def test_a_folder_inside_the_state_root_is_refused(world):
    root = Path(os.environ["YGG_STATE_ROOT"])
    with pytest.raises(YggError) as caught:
        codexsignin.check_location(root / "codex-login")
    assert caught.value.code == 3
    assert caught.value.message == ("refused: agent_codex_login_dir " + str(root / "codex-login") + " lies inside " +
                                    os.path.realpath(root))


def test_a_link_on_the_way_into_a_root_is_followed_one_component_at_a_time(world):
    root = Path(os.environ["YGG_STATE_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    (world / "up").symlink_to(root)
    with pytest.raises(YggError) as caught:
        codexsignin.check_location(world / "up" / "login")
    assert "lies inside " + os.path.realpath(root) in caught.value.message
    assert not (root / "login").exists()


def test_a_link_whose_target_climbs_out_of_a_root_is_still_refused(world):
    root = Path(os.environ["YGG_STATE_ROOT"])
    (root / "inner").mkdir(parents=True)
    (world / "hop").symlink_to(root / "inner" / ".." / ".." / "home")
    with pytest.raises(YggError) as caught:
        codexsignin.check_location(world / "hop" / "login")
    assert "lies inside" in caught.value.message


def test_the_safety_root_and_the_tracker_are_refused(world):
    from yggdrasil import safety, tracker
    for root in (safety.root(), tracker.root()):
        with pytest.raises(YggError) as caught:
            codexsignin.check_location(Path(root) / "login")
        assert "lies inside" in caught.value.message


def test_tmpdir_and_tmp_are_refused(world, monkeypatch):
    with pytest.raises(YggError) as caught:
        codexsignin.check_location(world / "tmpdir" / "login")
    assert "lies inside " + str(world / "tmpdir") in caught.value.message
    monkeypatch.setattr(codexsignin, "FIXED_ROOTS", ("/tmp", "/private/tmp"))
    with pytest.raises(YggError) as caught:
        codexsignin.check_location(Path("/tmp/ygg-login"))
    assert "lies inside" in caught.value.message


def test_a_programme_home_is_refused(world, monkeypatch):
    from yggdrasil import tracker
    home = world / "repo"
    home.mkdir()
    monkeypatch.setattr(tracker, "programmes", lambda: [{"id": "alpha", "home": str(home)}])
    with pytest.raises(YggError) as caught:
        codexsignin.check_location(home / ".ygg-worktrees" / "login")
    assert "lies inside " + str(home) in caught.value.message


def test_a_link_loop_is_refused(world):
    (world / "a").symlink_to(world / "b")
    (world / "b").symlink_to(world / "a")
    with pytest.raises(YggError) as caught:
        codexsignin.check_location(world / "a" / "login")
    assert caught.value.code == 3


# ---------------------------------------------------------------- the folder and file checks

def test_open_dir_accepts_a_private_folder_of_yours(world):
    folder = _plant(world)
    descriptor = codexsignin.open_dir(folder, "launch precondition: ")
    try:
        assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
        assert not os.get_inheritable(descriptor)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("case", ["link", "group", "file", "missing"])
def test_open_dir_refuses_anything_but_a_private_real_folder(world, case):
    folder = _login_dir(world)
    folder.parent.mkdir(parents=True)
    if case == "link":
        (world / "elsewhere").mkdir(mode=0o700)
        folder.symlink_to(world / "elsewhere")
    elif case == "group":
        folder.mkdir()
        folder.chmod(0o750)
    elif case == "file":
        folder.write_text("x")
    with pytest.raises(YggError) as caught:
        codexsignin.open_dir(folder, "launch precondition: ")
    assert caught.value.code == 3
    assert caught.value.message == "REFUSED: launch precondition: " + str(folder) + " is not a real folder of yours"


def test_the_login_file_is_ok_only_as_a_regular_file_of_yours_with_one_link(world):
    folder = _plant(world, login=False)
    descriptor = codexsignin.open_dir(folder, "")
    try:
        assert codexsignin.login_file_ok(descriptor) is False
        assert codexsignin.check_login_file(descriptor, folder, "p: ") is False
        (folder / "auth.json").write_text("{}")
        assert codexsignin.login_file_ok(descriptor) is True
        assert codexsignin.check_login_file(descriptor, folder, "p: ") is True
        os.link(folder / "auth.json", world / "second-name")
        assert codexsignin.login_file_ok(descriptor) is False
        with pytest.raises(YggError) as caught:
            codexsignin.check_login_file(descriptor, folder, "launch precondition: ")
        assert caught.value.message == ("REFUSED: launch precondition: " + str(folder) +
                                        "/auth.json is not a regular file of yours")
        (folder / "auth.json").unlink()
        (world / "target.json").write_text('{"t": 1}')
        (folder / "auth.json").symlink_to(world / "target.json")
        assert codexsignin.login_file_ok(descriptor) is False
        with pytest.raises(YggError):
            codexsignin.check_login_file(descriptor, folder, "")
        assert (world / "target.json").read_text() == '{"t": 1}'
    finally:
        os.close(descriptor)


def test_ensure_file_creates_a_private_lock_and_tightens_an_existing_one(world):
    folder = _plant(world)
    descriptor = codexsignin.open_dir(folder, "")
    try:
        codexsignin.ensure_file(descriptor, "auth.lock", folder, "launch precondition: ")
        info = os.lstat(folder / "auth.lock")
        assert stat.S_ISREG(info.st_mode) and info.st_mode & 0o777 == 0o600 and info.st_size == 0
        (folder / "auth.lock").write_text("keep")
        (folder / "auth.lock").chmod(0o644)
        codexsignin.ensure_file(descriptor, "auth.lock", folder, "launch precondition: ")
        assert (folder / "auth.lock").read_text() == "keep"  # never truncated
        assert os.lstat(folder / "auth.lock").st_mode & 0o777 == 0o600
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("case", ["link", "fifo", "hardlink", "folder"])
def test_ensure_file_refuses_an_unsafe_lock_without_touching_its_target(world, case):
    folder = _plant(world)
    target = world / "lock-target"
    target.write_text("keep")
    mode = target.stat().st_mode
    if case == "link":
        (folder / "auth.lock").symlink_to(target)
    elif case == "fifo":
        os.mkfifo(folder / "auth.lock")
    elif case == "hardlink":
        os.link(target, folder / "auth.lock")
    else:
        (folder / "auth.lock").mkdir()
    descriptor = codexsignin.open_dir(folder, "")
    try:
        with pytest.raises(YggError) as caught:
            codexsignin.ensure_file(descriptor, "auth.lock", folder, "launch precondition: ")
    finally:
        os.close(descriptor)
    assert caught.value.message == ("REFUSED: launch precondition: " + str(folder) +
                                    "/auth.lock is not a regular file of yours")
    assert target.read_text() == "keep"
    assert target.stat().st_mode == mode


# ---------------------------------------------------------------- the lease

def test_a_shared_lease_is_taken_on_a_login_and_blocks_an_exclusive_one(world):
    folder = _plant(world)
    shared = codexsignin.take_lease_shared(timeout=0.2)
    assert shared is not None
    try:
        assert not os.get_inheritable(shared)
        assert stat.S_ISREG(os.lstat(folder / ".ygg-sign-in.lock").st_mode)
        second = codexsignin.take_lease_shared(timeout=0.2)
        assert second is not None  # every session shares it
        os.close(second)
        assert codexsignin.take_lease_exclusive_nb() is None
    finally:
        os.close(shared)
    exclusive = codexsignin.take_lease_exclusive_nb()
    assert exclusive is not None
    try:
        assert codexsignin.take_lease_shared(timeout=0.2) is None  # waits, then gives up
    finally:
        os.close(exclusive)


def test_no_shared_lease_without_a_login_or_a_folder(world):
    assert codexsignin.take_lease_shared(timeout=0.1) is None
    folder = _plant(world, login=False)
    assert codexsignin.take_lease_shared(timeout=0.1) is None
    assert not (folder / ".ygg-sign-in.lock").exists()


def test_an_unsafe_lease_file_gives_no_shared_lease_and_refuses_a_logout(world):
    folder = _plant(world)
    (world / "elsewhere").write_text("keep")
    (folder / ".ygg-sign-in.lock").symlink_to(world / "elsewhere")
    assert codexsignin.take_lease_shared(timeout=0.1) is None
    with pytest.raises(YggError):
        codexsignin.take_lease_exclusive_nb()
    assert (world / "elsewhere").read_text() == "keep"


# ---------------------------------------------------------------- the route

HUB = {"name": "hub", "base_url": "http://127.0.0.1:18080/v1", "env_key": "HUB_KEY", "wire_api": "responses"}


def test_a_provider_key_route_names_only_the_provider(world):
    _plant(world)
    assert codexsignin.route({"HUB_KEY": "hk-1", "CODEX_API_KEY": "k-1"}, HUB, True) == ("provider key", "hub")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_missing_or_blank_provider_key_is_no_route_even_with_other_keys(world, value):
    _plant(world)
    environment = {"CODEX_API_KEY": "k-1"}
    if value is not None:
        environment["HUB_KEY"] = value
    assert codexsignin.route(environment, HUB, True) == ("none", "set HUB_KEY for the conductor")


def test_a_provider_without_a_key_is_its_own_route(world):
    assert codexsignin.route({}, {"name": "local", "base_url": "http://127.0.0.1:1/v1"}, True) == \
        ("provider", "local")


def test_the_default_provider_prefers_the_api_key_then_the_login(world):
    _plant(world)
    assert codexsignin.route({"CODEX_API_KEY": "k-2"}, None, True) == ("api key", "")
    assert codexsignin.route({"CODEX_API_KEY": "   "}, None, True) == ("yggdrasil login", str(_login_dir(world)))


def test_no_login_folder_names_every_way_to_sign_in(world):
    route, detail = codexsignin.route({}, None, True)
    assert route == "none"
    assert detail == "run ygg codex login, set CODEX_API_KEY, or use a model provider with its own key"
    assert codexsignin.next_step(route, detail) == detail
    assert "ygg codex login" in codexsignin.next_step(route, detail)


def test_a_login_folder_without_the_login_says_to_log_in_again(world):
    folder = _plant(world, login=False)
    (folder / "config.toml").write_text(codexsignin.CONFIG_TEXT)
    route, detail = codexsignin.route({}, None, True)
    assert route == "none"
    assert detail == "the login is not in " + str(folder) + "/auth.json; run ygg codex login again"


def test_a_missing_custom_folder_may_be_an_unmounted_volume(world):
    custom = world / "volume" / "login"
    _set_dir(world, custom)
    assert codexsignin.route({}, None, True) == ("none", str(custom) + " does not exist (unmounted or removed?)")


def test_a_folder_inside_a_root_is_no_route_with_the_refusal_as_detail(world):
    root = Path(os.environ["YGG_STATE_ROOT"])
    _set_dir(world, root / "codex-login")
    route, detail = codexsignin.route({}, None, True)
    assert route == "none"
    assert detail.startswith("refused: agent_codex_login_dir " + str(root / "codex-login") + " lies inside ")


def test_an_unsafe_login_is_no_route(world):
    folder = _plant(world, mode=0o750)
    route, detail = codexsignin.route({}, None, True)
    assert (route, detail) == ("none", str(folder) + " is not a real folder of yours")


def test_an_unprobed_version_has_no_shared_sign_in_on_the_default_provider(world):
    _plant(world)
    route, detail = codexsignin.route({"CODEX_API_KEY": "k-1"}, None, False, version="codex-cli 0.171.0")
    assert route == "none"
    assert detail == ("Codex 0.171.0 is not probed for the shared sign-in; use a model provider with its own key, "
                      "or install codex-cli 0.170.0")
    assert codexsignin.route({"HUB_KEY": "hk"}, HUB, False) == ("provider key", "hub")


# ---------------------------------------------------------------- the alert and status

def _sign_in_alerts():
    return [a for a in state.alerts() if a.get("kind") == "ygg-codex-sign-in"]


def test_one_log_alert_per_next_step_and_a_route_clears_it(world):
    assert alertkinds.classify("ygg-codex-sign-in") == "log"
    assert codexsignin.status_lines() == []
    codexsignin.note_route("none", codexsignin.NEXT_LOGIN)
    codexsignin.note_route("none", codexsignin.NEXT_LOGIN)
    alerts = _sign_in_alerts()
    assert len(alerts) == 1 and alerts[0]["class"] == "log"
    assert "ygg codex login" in alerts[0]["message"]
    assert codexsignin.status_lines() == ["codex sign-in: none (" + codexsignin.NEXT_LOGIN + ")"]
    codexsignin.note_route("none", "set HUB_KEY for the conductor")
    assert len(_sign_in_alerts()) == 2
    codexsignin.note_route("api key", "")
    assert codexsignin.status_lines() == []
    codexsignin.note_route("none", "set HUB_KEY for the conductor")
    assert len(_sign_in_alerts()) == 3
    assert alertkinds.show(True, True).count("ygg-codex-sign-in") == 0


def test_a_launch_of_an_unprobed_reviewer_is_reported_as_not_probed(world, monkeypatch):
    _plant(world)
    monkeypatch.setenv("CODEX_API_KEY", "k-rv-2")
    monkeypatch.setattr(config, "profile", lambda: {})
    data = {"node": "pm1rv1", "activation": "act-9", "cli": "codex", "role": "reviewer", "pinned": {},
            "codex_pin": {"probed": False, "checked": True, "version": "codex-cli 0.171.0"}}
    assert "CODEX_API_KEY" not in codexsignin.session_env(data)
    codexsignin.note_launch(data)
    alerts = _sign_in_alerts()
    assert len(alerts) == 1 and "not probed" in alerts[0]["message"]
    assert "k-rv-2" not in json.dumps(alerts)


def test_a_launch_with_a_route_clears_the_record(world, monkeypatch):
    _plant(world)
    monkeypatch.setattr(config, "profile", lambda: {})
    codexsignin.note_route("none", codexsignin.NEXT_LOGIN)
    codexsignin.note_launch({"node": "pm1tl1", "activation": "act-1", "cli": "codex", "role": "tl", "pinned": {},
                             "codex_pin": {"probed": True}, "codex_login_links": True})
    assert codexsignin.status_lines() == []


def test_a_failing_route_never_stops_a_launch(world, monkeypatch):
    monkeypatch.setattr(codexsignin, "route", lambda *a, **k: 1 / 0)
    codexsignin.note_launch({"node": "x", "activation": "y", "cli": "codex", "role": "tl"})


# ---------------------------------------------------------------- doctor

def _hub_home(world):
    home = world / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "hub"\n[model_providers.hub]\nname = "hub"\n'
                                      'base_url = "http://127.0.0.1:18080/v1"\nenv_key = "HUB_KEY"\n'
                                      'wire_api = "responses"\n')
    os.environ["CODEX_HOME"] = str(home)


def test_doctor_judges_a_provider_key_by_what_reaches_the_session(world, monkeypatch, capfd):
    _plant(world)
    _hub_home(world)
    monkeypatch.setenv("HUB_KEY", "hk-1")
    monkeypatch.setenv("CODEX_API_KEY", "k-1")
    code, out, _ = _ygg(capfd, "doctor", "--json")
    assert code == 0
    sign_in = json.loads(out)["codex_sign_in"]
    assert sign_in["route"] == "none" and "HUB_KEY" in sign_in["detail"]
    (world / "config.yaml").write_text("schema: ygg.config.v1\nagent_env_passthrough: [HUB_KEY]\n")
    _clear()
    code, out, _ = _ygg(capfd, "doctor", "--json")
    assert json.loads(out)["codex_sign_in"] == {"route": "provider key", "detail": "hub"}
    assert "hk-1" not in out and "k-1" not in out


def test_doctor_reads_a_provider_key_set_by_the_profile(world, monkeypatch, capfd):
    _plant(world)
    _hub_home(world)
    profile = world / "profile.yaml"
    profile.write_text("schema: ygg.config.v1\nname: p\nlaunch_env:\n  HUB_KEY: hk-2\n")
    monkeypatch.setenv("YGG_PROFILE", str(profile))
    _clear()
    code, out, _ = _ygg(capfd, "doctor", "--json")
    assert json.loads(out)["codex_sign_in"]["route"] == "provider key"
    assert "hk-2" not in out


def test_doctor_text_names_the_route_and_never_a_token(world, monkeypatch, capfd):
    _plant(world)
    monkeypatch.setenv("CODEX_API_KEY", "   ")
    code, out, _ = _ygg(capfd, "doctor")
    assert code == 0
    assert "codex sign-in: yggdrasil login\n" in out
    assert "r1" not in out
    monkeypatch.setenv("CODEX_API_KEY", "k-2")
    assert doctor.report()["codex_sign_in"] == {"route": "api key", "detail": ""}


def test_doctor_notes_an_unprobed_version(world, monkeypatch):
    monkeypatch.setenv("YGG_TEST_CODEX_VERSION", "codex-cli 0.171.0")
    sign_in = doctor.report()["codex_sign_in"]
    assert sign_in["route"] == "none"
    assert sign_in["detail"].count("is not probed for the shared sign-in") == 1
    _hub_home(world)
    monkeypatch.setenv("HUB_KEY", "hk")
    (world / "config.yaml").write_text("schema: ygg.config.v1\nagent_env_passthrough: [HUB_KEY]\n")
    _clear()
    sign_in = doctor.report()["codex_sign_in"]
    assert sign_in["route"] == "provider key"
    assert sign_in["detail"].startswith("hub; Codex 0.171.0 is not probed")


# ---------------------------------------------------------------- the verbs

def test_login_creates_the_private_folder_with_file_storage_and_hides_the_key(world, monkeypatch, capfd):
    monkeypatch.setenv("CODEX_API_KEY", "k-shell-1")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "t-shell-1")
    code, out, err = _ygg(capfd, "codex", "login", "--device-auth")
    assert code == 0
    folder = _login_dir(world)
    calls = _calls(world)
    assert len(calls) == 1 and calls[0]["argv"] == ["login", "--device-auth"]
    assert calls[0]["env"]["CODEX_HOME"] == str(folder)
    assert "CODEX_API_KEY" not in calls[0]["env"] and "CODEX_ACCESS_TOKEN" not in calls[0]["env"]
    assert os.lstat(folder).st_mode & 0o777 == 0o700 and stat.S_ISDIR(os.lstat(folder).st_mode)
    assert (folder / "config.toml").read_text() == 'cli_auth_credentials_store = "file"\n'
    assert (folder / "auth.json").read_text() == '{"tokens": {"refresh_token": "r1"}}'
    assert "r1" not in out + err and "not probed" not in err
    assert sorted(os.listdir(folder)) == ["auth.json", "config.toml"]


def test_login_with_an_api_key_passes_standard_input_through(world, monkeypatch, capfd):
    read, write = os.pipe()
    os.write(write, b"k-row2-123\n")
    os.close(write)
    saved = os.dup(0)
    os.dup2(read, 0)
    try:
        code, out, err = _ygg(capfd, "codex", "login", "--with-api-key")
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(read)
    assert code == 0
    assert (_login_dir(world) / "stdin.txt").read_text() == "k-row2-123\n"
    assert "k-row2-123" not in out + err


def test_login_refuses_a_linked_folder_and_never_runs_codex(world, capfd):
    folder = _login_dir(world)
    folder.parent.mkdir(parents=True)
    (world / "elsewhere").mkdir()
    folder.symlink_to(world / "elsewhere")
    code, _, err = _ygg(capfd, "codex", "login", "--device-auth")
    assert code == 3 and "is not a real folder of yours" in err
    assert _calls(world) == []
    assert os.listdir(world / "elsewhere") == []


def _not_ours(folder) -> str:
    return ("refused: agent_codex_login_dir " + str(folder) + " is not empty and is not a Yggdrasil Codex login "
            "folder; choose a new folder")


def _snapshot(folder):
    found = {}
    for path in sorted([folder] + list(folder.rglob("*"))):
        info = os.lstat(path)
        found[str(path)] = (stat.S_IFMT(info.st_mode), info.st_mode & 0o7777,
                            path.read_bytes() if stat.S_ISREG(info.st_mode) else os.readlink(path)
                            if stat.S_ISLNK(info.st_mode) else None)
    return found


def test_login_tightens_an_existing_empty_folder(world, capfd):
    folder = _plant(world, login=False, mode=0o755)
    code, _, _ = _ygg(capfd, "codex", "login", "--device-auth")
    assert code == 0
    assert os.lstat(folder).st_mode & 0o777 == 0o700
    assert (folder / "config.toml").read_text() == codexsignin.CONFIG_TEXT


def test_login_refuses_a_codex_home_of_the_users_own_and_changes_nothing(world, capfd):
    folder = _plant(world, login=True, mode=0o755)
    (folder / "config.toml").write_text('model = "o3"\napproval_policy = "on-request"\n')
    (folder / "config.toml").chmod(0o644)
    (folder / "hooks.json").write_text("{}\n")
    (folder / "skills").mkdir(mode=0o755)
    before = _snapshot(folder)
    code, out, err = _ygg(capfd, "codex", "login", "--device-auth")
    assert code == 3
    assert _not_ours(folder) in err and "REFUSED: refused" not in err
    assert _calls(world) == []
    assert _snapshot(folder) == before


@pytest.mark.parametrize("case", ["linked config", "other text", "no config", "config folder"])
def test_login_refuses_any_folder_not_set_up_by_yggdrasil(world, capfd, case):
    folder = _plant(world, login=False)
    if case == "linked config":
        (world / "other.toml").write_text(codexsignin.CONFIG_TEXT)
        (folder / "config.toml").symlink_to(world / "other.toml")
    elif case == "other text":
        (folder / "config.toml").write_text(codexsignin.CONFIG_TEXT + 'model = "o3"\n')
    elif case == "no config":
        (folder / "notes.txt").write_text("mine\n")
    else:
        (folder / "config.toml").mkdir()
    before = _snapshot(folder)
    code, _, err = _ygg(capfd, "codex", "login", "--device-auth")
    assert code == 3 and _not_ours(folder) in err
    assert _calls(world) == []
    assert _snapshot(folder) == before
    if case == "linked config":
        assert (world / "other.toml").read_text() == codexsignin.CONFIG_TEXT


def test_login_again_in_a_folder_an_earlier_login_set_up(world, capfd):
    code, _, _ = _ygg(capfd, "codex", "login", "--device-auth")
    assert code == 0
    folder = _login_dir(world)
    (folder / "auth.lock").write_text("")
    (folder / ".ygg-sign-in.lock").write_text("")
    code, _, err = _ygg(capfd, "codex", "login", "--device-auth")
    assert code == 0 and "refused" not in err
    assert len(_calls(world)) == 2
    assert (folder / "config.toml").read_text() == codexsignin.CONFIG_TEXT


def test_login_creates_parents_only_for_the_default_folder(world, capfd):
    custom = world / "unmounted" / "login"
    _set_dir(world, custom)
    code, _, err = _ygg(capfd, "codex", "login", "--device-auth")
    assert code == 3 and "does not exist (unmounted or removed?)" in err
    assert not (world / "unmounted").exists() and _calls(world) == []
    (world / "unmounted").mkdir()
    code, _, _ = _ygg(capfd, "codex", "login", "--device-auth")
    assert code == 0
    assert _calls(world)[0]["env"]["CODEX_HOME"] == str(custom)
    assert not _login_dir(world).exists()


def test_login_refuses_a_folder_inside_the_state_root(world, capfd):
    _set_dir(world, Path(os.environ["YGG_STATE_ROOT"]) / "codex-login")
    code, _, err = _ygg(capfd, "codex", "login", "--device-auth")
    assert code == 3
    assert "refused: agent_codex_login_dir" in err and "lies inside" in err
    assert "REFUSED: refused" not in err
    assert _calls(world) == []


def test_status_warns_on_an_unprobed_version_and_passes_output_and_code(world, monkeypatch, capfd):
    _plant(world)
    monkeypatch.setenv("YGG_TEST_CODEX_VERSION", "codex-cli 0.171.0")
    code, out, err = _ygg(capfd, "codex", "status")
    assert code == 0 and "Logged in using ChatGPT" in out
    assert "ygg: this Codex version is not probed for the shared sign-in" in err
    assert _calls(world)[0]["argv"] == ["login", "status"]
    assert _calls(world)[0]["env"]["CODEX_HOME"] == str(_login_dir(world))


def test_logout_passes_the_exit_code_and_takes_the_lease(world, monkeypatch, capfd):
    folder = _plant(world)
    monkeypatch.setenv("YGG_TEST_CODEX_VERSION", "codex-cli 0.170.0")
    code, out, err = _ygg(capfd, "codex", "logout")
    assert code == 1 and "Not logged in" in out and "not probed" not in err
    assert _calls(world)[0]["argv"] == ["logout"]
    assert stat.S_ISREG(os.lstat(folder / ".ygg-sign-in.lock").st_mode)


def test_logout_is_refused_while_a_session_holds_the_lease(world, capfd):
    folder = _plant(world)
    lease = codexsignin.take_lease_shared(timeout=0.2)
    try:
        code, _, err = _ygg(capfd, "codex", "logout")
    finally:
        os.close(lease)
    assert code == 3
    assert "refused: Codex sessions are running; stop them or let them end, then log out" in err
    assert _calls(world) == []
    assert (folder / "auth.json").read_text() == '{"tokens": {"refresh_token": "r1"}}'


def test_logout_without_a_folder_does_nothing(world, capfd):
    code, out, _ = _ygg(capfd, "codex", "logout")
    assert code == 0 and "no Yggdrasil Codex login" in out
    assert _calls(world) == [] and not _login_dir(world).exists()


def test_the_codex_verbs_are_the_users_alone(world, monkeypatch, capfd):
    assert state.USER_VERBS["codex"] is False
    assert not relay.is_relayable(["codex", "logout"])
    monkeypatch.setenv("YGG_NODE", "pm1tl1")
    for argv in (("codex", "login", "--device-auth"), ("codex", "status"), ("codex", "logout")):
        code, _, err = _ygg(capfd, *argv)
        assert code == 3 and "user-only" in err
    assert _calls(world) == []


def test_the_launchers_own_session_environment_is_used_when_it_has_one(world, monkeypatch):
    from yggdrasil import launcher
    seen = []
    monkeypatch.setattr(launcher, "codex_session_env", lambda data=None: seen.append(data) or {"CODEX_API_KEY": "k"},
                        raising=False)
    assert codexsignin.session_env() == {"CODEX_API_KEY": "k"}
    assert seen == [None]
    assert doctor.report()["codex_sign_in"] == {"route": "api key", "detail": ""}


def test_every_codex_launch_reports_its_route():
    import inspect
    from yggdrasil import launcher
    source = inspect.getsource(launcher.launch)
    assert 'if cli == "codex":\n            from . import codexsignin\n' in source
    assert "codexsignin.note_launch(dict(receipt(activation), **codex_fields))" in source


# ---------------------------------------------------------------- `..` is walked as written

def test_a_dotdot_in_the_setting_is_kept_through_config_loading(world):
    (world / "conf").mkdir()
    (world / "conf" / "config.yaml").write_text("agent_codex_login_dir: hop/../login\n")
    os.environ["YGG_CONFIG"] = str(world / "conf" / "config.yaml")
    _clear()
    assert config.get("agent_codex_login_dir") == str(world / "conf" / "hop" / ".." / "login")
    assert str(codexsignin.login_dir()) == str(world / "conf") + "/hop/../login"
    os.environ["YGG_CONFIG"] = str(world / "config.yaml")
    _set_dir(world, "/safe/hop/../login")
    assert str(codexsignin.login_dir()) == "/safe/hop/../login"


def test_a_dotdot_after_a_link_into_a_programme_home_is_refused(world, monkeypatch):
    from yggdrasil import tracker
    home = world / "repo"
    (home / "inner").mkdir(parents=True)
    monkeypatch.setattr(tracker, "programmes", lambda: [{"id": "alpha", "home": str(home)}])
    (world / "safe").mkdir()
    (world / "safe" / "hop").symlink_to(home / "inner")
    login = Path(str(world / "safe") + "/hop/../login")
    assert not os.path.normpath(str(login)).startswith(str(home))  # collapsing the text would hide the home
    with pytest.raises(YggError) as caught:
        codexsignin.check_location(login)
    assert caught.value.message == ("refused: agent_codex_login_dir " + str(login) + " lies inside " + str(home))
    _set_dir(world, login)
    assert codexsignin.route({}, None, True)[0] == "none"
    assert cli.main(["codex", "login", "--device-auth"]) == 3 and _calls(world) == [] and not (home / "login").exists()


def test_a_dotdot_after_a_link_into_a_box_is_refused(world, monkeypatch):
    from yggdrasil import quarantine
    box = world / "lanes" / "pm1tl1.g000001"
    (box / "checkout").mkdir(parents=True)
    monkeypatch.setattr(quarantine, "_box_roots", lambda: {str(box)})
    (world / "safe").mkdir()
    (world / "safe" / "hop").symlink_to(box / "checkout")
    login = Path(str(world / "safe") + "/hop/../login")
    with pytest.raises(YggError) as caught:
        codexsignin.check_location(login)
    assert caught.value.message.endswith(" lies inside " + str(box))


def test_a_dotdot_that_climbs_out_through_a_safe_link_is_allowed(world):
    (world / "elsewhere" / "deep").mkdir(parents=True)
    (world / "safe").mkdir()
    (world / "safe" / "hop").symlink_to(world / "elsewhere" / "deep")
    codexsignin.check_location(Path(str(world / "safe") + "/hop/../login"))


# ---------------------------------------------------------------- the location check comes first

def test_doctor_reports_a_forbidden_location_even_with_a_key(world, monkeypatch):
    root = Path(os.environ["YGG_STATE_ROOT"])
    _set_dir(world, root / "login")
    monkeypatch.setenv("CODEX_API_KEY", "k-1")
    assert doctor.report()["codex_sign_in"]["route"] == "api key"  # nothing there yet: no links, nothing to refuse
    (root / "login").mkdir(parents=True)
    sign_in = doctor.report()["codex_sign_in"]
    assert sign_in["route"] == "none"
    assert sign_in["detail"].startswith("refused: agent_codex_login_dir " + str(root / "login") + " lies inside ")
    assert "k-1" not in json.dumps(sign_in)
    assert codexsignin.route({"HUB_KEY": "hk"}, HUB, True)[0] == "none"
    assert codexsignin.route({"CODEX_API_KEY": "k"}, None, True, granted={"links": False})[0] == "none"


# ---------------------------------------------------------------- what the session was granted

def test_doctor_judges_an_unsafe_lock_as_no_login(world):
    folder = _plant(world)
    (world / "lock-target").write_text("keep")
    (folder / "auth.lock").symlink_to(world / "lock-target")
    assert codexsignin.route({}, None, True) == ("none", str(folder) + "/auth.lock is not a regular file of yours")
    assert doctor.report()["codex_sign_in"]["route"] == "none"
    assert (world / "lock-target").read_text() == "keep"
    assert os.path.islink(folder / "auth.lock")


def test_doctor_names_an_unsafe_lease_file_but_keeps_the_login(world):
    folder = _plant(world)
    os.mkfifo(folder / ".ygg-sign-in.lock")
    route, detail = codexsignin.route({}, None, True)
    assert route == "yggdrasil login"
    assert str(folder) + "/.ygg-sign-in.lock is not a regular file of yours" in detail
    assert stat.S_ISFIFO(os.lstat(folder / ".ygg-sign-in.lock").st_mode)


def test_a_reviewer_given_no_links_never_clears_the_alert(world, monkeypatch):
    folder = _plant(world)
    monkeypatch.setattr(config, "profile", lambda: {})
    (folder / "auth.lock").symlink_to(world / "elsewhere")
    reason = "launch precondition: " + str(folder) + "/auth.lock is not a regular file of yours"
    codexsignin.note_route("none", codexsignin.NEXT_LOGIN)
    data = {"node": "pm1rv1", "activation": "act-3", "cli": "codex", "role": "reviewer", "pinned": {},
            "codex_pin": {"probed": True, "checked": True, "version": "codex-cli 0.170.0"},
            "codex_login_links": False, "codex_sign_in": reason}
    codexsignin.note_launch(data)
    assert codexsignin.status_lines() == ["codex sign-in: none (" + reason + ")"]
    assert _sign_in_alerts()[-1]["next_step"] == reason


def test_a_session_given_no_links_and_no_reason_is_no_route(world, monkeypatch):
    _plant(world)
    granted = {"links": False, "sign_in": None}
    assert codexsignin.route({}, None, True, granted=granted) == ("none", codexsignin.NEXT_LOGIN)
    assert codexsignin.route({}, None, True, granted={"links": True}) == ("yggdrasil login", str(_login_dir(world)))
    assert codexsignin.route({"CODEX_API_KEY": "k"}, None, True, granted=granted) == ("api key", "")


def test_links_to_a_login_removed_since_never_clear_the_alert(world, monkeypatch):
    folder = _plant(world)
    monkeypatch.setattr(config, "profile", lambda: {})
    codexsignin.note_route("none", codexsignin.NEXT_LOGIN)
    (folder / "auth.json").unlink()
    data = {"node": "pm1b1", "activation": "act-4", "cli": "codex", "role": "builder", "pinned": {},
            "codex_pin": {"probed": True, "checked": True, "version": "codex-cli 0.170.0"},
            "codex_login_links": True, "codex_sign_in": None}
    route, _detail = codexsignin.route({}, None, True, granted={"links": True})
    assert route == "none"
    codexsignin.note_launch(data)
    assert codexsignin.status_lines() != []


# ---------------------------------------------------------------- a probed version on a platform it is not probed on

PLATFORM_STEP = "Codex's shared sign-in is not probed on linux; use a model provider with its own key"


def test_a_version_probed_elsewhere_names_the_platform_not_an_install(world, monkeypatch):
    _plant(world)
    monkeypatch.setenv("YGG_TEST_PLATFORM", "linux")
    route, detail = codexsignin.route({"CODEX_API_KEY": "k-1"}, None, False, version="codex-cli 0.170.0")
    assert (route, detail) == ("none", PLATFORM_STEP)
    assert codexsignin.not_probed_step("codex-cli 0.170.0") == PLATFORM_STEP


def test_a_version_probed_nowhere_keeps_its_step_on_a_probed_platform(world, monkeypatch):
    monkeypatch.setenv("YGG_TEST_PLATFORM", "darwin")
    assert codexsignin.not_probed_step("codex-cli 0.171.0") == (
        "Codex 0.171.0 is not probed for the shared sign-in; use a model provider with its own key, "
        "or install codex-cli 0.170.0")


def test_doctor_names_the_platform_for_a_version_probed_elsewhere(world, monkeypatch):
    monkeypatch.setenv("YGG_TEST_PLATFORM", "linux")
    monkeypatch.setenv("YGG_TEST_CODEX_VERSION", "codex-cli 0.170.0")
    sign_in = doctor.report()["codex_sign_in"]
    assert sign_in == {"route": "none", "detail": PLATFORM_STEP}


def test_the_alert_names_the_platform_for_a_reviewer_probed_elsewhere(world, monkeypatch):
    _plant(world)
    monkeypatch.setenv("YGG_TEST_PLATFORM", "linux")
    monkeypatch.setattr(config, "profile", lambda: {})
    data = {"node": "pm1rv1", "activation": "act-9", "cli": "codex", "role": "reviewer", "pinned": {},
            "codex_pin": {"probed": False, "checked": True, "version": "codex-cli 0.170.0"}}
    codexsignin.note_launch(data)
    alerts = _sign_in_alerts()
    assert len(alerts) == 1 and PLATFORM_STEP in alerts[0]["message"]
    assert "install" not in alerts[0]["message"]
    assert codexsignin.status_lines() == ["codex sign-in: none (" + PLATFORM_STEP + ")"]


def test_the_verbs_warn_with_the_platform_for_a_version_probed_elsewhere(world, monkeypatch, capfd):
    _plant(world)
    monkeypatch.setenv("YGG_TEST_PLATFORM", "linux")
    monkeypatch.setenv("YGG_TEST_CODEX_VERSION", "codex-cli 0.170.0")
    code, out, err = _ygg(capfd, "codex", "status")
    assert code == 0 and "Logged in using ChatGPT" in out
    assert "ygg: " + PLATFORM_STEP in err
    assert "this Codex version" not in err
    monkeypatch.setenv("YGG_TEST_CODEX_VERSION", "codex-cli 0.171.0")
    code, _, err = _ygg(capfd, "codex", "status")
    assert "ygg: this Codex version is not probed for the shared sign-in" in err


def test_login_never_replaces_a_config_written_after_its_check(world, capfd, monkeypatch):
    folder = _plant(world, login=False)
    checked = codexsignin._set_up_before

    def then_the_user_writes(descriptor):
        found = checked(descriptor)
        (folder / "config.toml").write_text('model = "o3"\n')
        return found

    monkeypatch.setattr(codexsignin, "_set_up_before", then_the_user_writes)
    code, _, err = _ygg(capfd, "codex", "login", "--device-auth")
    assert code == 3 and _not_ours(folder) in err
    assert (folder / "config.toml").read_text() == 'model = "o3"\n'
    assert sorted(os.listdir(folder)) == ["config.toml"]
    assert _calls(world) == []


def test_login_leaves_its_own_config_alone(world, capfd):
    folder = _plant(world, login=False)
    (folder / "config.toml").write_text(codexsignin.CONFIG_TEXT)
    before = os.lstat(folder / "config.toml").st_ino
    code, _, _ = _ygg(capfd, "codex", "login", "--device-auth")
    assert code == 0
    assert os.lstat(folder / "config.toml").st_ino == before
