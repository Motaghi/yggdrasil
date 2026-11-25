"""A broken config or profile file makes every command refuse, `stop`, `pause` and `resume` included.

With an unreadable file Yggdrasil cannot know which folders or tracker it would act on, so acting on defaults and
reporting success would be worse than refusing. The message names the file and its line, tells the emergency commands
what to do next, and never quotes the file. A broken profile is the same safe usage error as a broken config, and a
non-secret `key` name such as `linear_team_key` is shown."""
from __future__ import annotations

from pathlib import Path

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (fixture and helper)

SENTINEL = "sentinel-value-9f3a1c"
BROKEN = [(b"models: [\n", "not valid YAML at line"), (b"- a\n", "not a mapping"), (b"a: 1\n\xff\xfe", "not valid YAML")]
PINS = ("YGG_STATE_ROOT", "YGG_FILES_ROOT", "YGG_TRACKER")
COMMANDS = [["stop"], ["pause"], ["resume"]]
NEXT_STEP = "Yggdrasil can't tell which project to act on until this file is fixed; fix"


def _snapshot(folder: Path) -> list:
    """Every path under the folder with its size and modification time: equal before and after means nothing was
    written anywhere in it (the real state folder, the default one and `./ygg-tracker` are all inside)."""
    return sorted((str(p.relative_to(folder)), p.lstat().st_size, p.lstat().st_mtime_ns) for p in folder.rglob("*"))


def _broken_file(world, content, name="broken.yaml"):
    path = world / name
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("pinned", [True, False], ids=["roots-set", "roots-unset"])
@pytest.mark.parametrize("command", COMMANDS)
def test_stop_pause_and_resume_refuse_a_broken_config_and_write_nothing(world, monkeypatch, capsys, command, pinned):
    """The config names the real folders and a node lives there; the file is then broken. Each emergency command
    refuses with the file and its line, a next step and no file text, and nothing is written anywhere."""
    from yggdrasil import cli, commands
    real_state, real_files = world / "real-state", world / "real-files"
    config_file = world / "real.yaml"
    config_file.write_text("token: " + SENTINEL + "\nstate_root: " + str(real_state) + "\nfiles_root: " +
                           str(real_files) + "\ntracker: files\nlaunch_confirm_seconds: 0.5\nsettle_delay_seconds: 0\n")
    for name in PINS:
        monkeypatch.delenv(name)
    monkeypatch.setenv("YGG_CONFIG", str(config_file))
    clear_caches()
    home = world / "beta-home"
    home.mkdir()
    commands.programme_create("beta", "beta", str(home), "vault-docs")
    commands.project_create("b1", "beta", "pmb")  # a node recorded under the configured folders
    assert real_state.is_dir() and real_files.is_dir()
    config_file.write_text("token: " + SENTINEL + "\nstate_root: [\n")  # now it is broken
    if pinned:
        monkeypatch.setenv("YGG_STATE_ROOT", str(real_state))
        monkeypatch.setenv("YGG_FILES_ROOT", str(real_files))
        monkeypatch.setenv("YGG_TRACKER", "files")
    clear_caches()
    before = _snapshot(world)
    assert cli.main(command) == util.EXIT_USAGE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(str(config_file) + ": not valid YAML at line ")
    assert NEXT_STEP + " that line and run the command again." in captured.err
    assert SENTINEL not in captured.err and "state_root" not in captured.err
    assert _snapshot(world) == before  # not the real folders, not the default ones, no ./ygg-tracker
    assert not (Path.home() / ".local" / "state" / "yggdrasil").exists()
    assert not (world / "ygg-tracker").exists()


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("content,problem", BROKEN, ids=["yaml", "list", "utf8"])
def test_the_emergency_commands_refuse_every_kind_of_broken_config_with_the_next_step(
        world, monkeypatch, capsys, command, content, problem):
    from yggdrasil import cli
    path = _broken_file(world, content)
    monkeypatch.setenv("YGG_CONFIG", str(path))
    clear_caches()
    before = _snapshot(world)
    assert cli.main(command) == util.EXIT_USAGE
    err = capsys.readouterr().err
    assert err.startswith(str(path) + ": " + problem)
    assert NEXT_STEP in err and "run the command again." in err
    assert _snapshot(world) == before


def test_stop_refuses_a_broken_profile_with_the_same_next_step(world, monkeypatch, capsys):
    from yggdrasil import cli
    path = _broken_file(world, b"x: !S3cretTag y\nmodels: [\n", "profile.yaml")
    monkeypatch.setenv("YGG_PROFILE", str(path))
    clear_caches()
    before = _snapshot(world)
    assert cli.main(["stop"]) == util.EXIT_USAGE
    err = capsys.readouterr().err
    assert err.startswith(str(path) + ": not valid YAML") and NEXT_STEP in err and "S3cretTag" not in err
    assert _snapshot(world) == before


def test_other_commands_get_the_usual_message_without_the_emergency_next_step(world, monkeypatch, capsys):
    from yggdrasil import cli
    path = _broken_file(world, b"models: [\n")
    monkeypatch.setenv("YGG_CONFIG", str(path))
    clear_caches()
    assert cli.main(["status"]) == util.EXIT_USAGE
    err = capsys.readouterr().err
    assert err.startswith(str(path) + ": not valid YAML") and NEXT_STEP not in err


def test_a_valid_config_lets_stop_pause_and_resume_run(world, capsys):
    from yggdrasil import cli
    for command in COMMANDS:
        assert cli.main(command) == 0
    assert "not valid YAML" not in capsys.readouterr().err


# ---------------------------------------------------------------- a broken profile

@pytest.mark.parametrize("content,problem", BROKEN, ids=["yaml", "list", "utf8"])
def test_a_broken_profile_is_the_same_usage_error_as_a_broken_config(world, monkeypatch, capsys, content, problem):
    from yggdrasil import cli
    path = _broken_file(world, content, "profile.yaml")
    monkeypatch.setenv("YGG_PROFILE", str(path))
    clear_caches()
    assert cli.main(["status"]) == util.EXIT_USAGE
    assert str(path) + ": " + problem in capsys.readouterr().err
    clear_caches()
    assert cli.main(["profile", "show"]) == util.EXIT_USAGE
    assert str(path) + ": " + problem in capsys.readouterr().err


def test_a_broken_profile_never_echoes_the_file_and_an_empty_one_is_fine(world, monkeypatch, capsys):
    from yggdrasil import cli
    path = world / "profile.yaml"
    path.write_text("token: sk-abcdef-1234567\nx: !S3cretTag y\n")
    monkeypatch.setenv("YGG_PROFILE", str(path))
    clear_caches()
    assert cli.main(["status"]) == util.EXIT_USAGE
    err = capsys.readouterr().err
    assert "sk-abcdef-1234567" not in err and "S3cretTag" not in err
    path.write_text("")
    clear_caches()
    assert cli.main(["status"]) == 0


# ---------------------------------------------------------------- names that look secret

def test_a_non_secret_key_name_is_shown_and_secret_ones_stay_hidden():
    from yggdrasil import config
    shown = util.redact_profile({"linear_team_key": "YGG", "LINEAR_TEAM_KEY": "YGG", "STRIPE_KEY": "abcdef123456",
                                 "api_key": "abcdef123457"})
    assert shown["linear_team_key"] == "YGG" and shown["LINEAR_TEAM_KEY"] == "YGG"
    assert shown["STRIPE_KEY"] == "[redacted]" and shown["api_key"] == "[redacted]"
    assert config._shown("YGG", "linear_team_key") == "YGG"
    assert util.redact_profile({"team": {"linear_team_key": "YGG"}}) == {"team": {"linear_team_key": "YGG"}}


def test_profile_show_pins_what_it_really_prints_for_the_linear_names(world, monkeypatch, capsys):
    """`ygg profile show` hides `linear_token_store` and `linear_token_file` (their names contain `token`); only
    `ygg config explain` shows those two as they are. `linear_team_key` is shown by both."""
    from yggdrasil import cli, config
    profile = world / "profile.yaml"
    profile.write_text("linear_token_store: keychain\nlinear_token_file: /secrets/linear.json\n"
                       "linear_team_key: YGG\nSTRIPE_KEY: abcdef123456\n")
    monkeypatch.setenv("YGG_PROFILE", str(profile))
    clear_caches()
    assert cli.main(["profile", "show"]) == 0
    out = capsys.readouterr().out
    assert "linear_team_key: YGG" in out
    assert "keychain" not in out and "/secrets/linear.json" not in out and "abcdef123456" not in out
    assert out.count("[redacted]") == 3
    assert config._shown("keychain", "linear_token_store") == "keychain"  # config explain shows it as it is


@pytest.mark.parametrize("body", ["x: *SENTINEL_ALIAS_7Q\n", "a: [1, 2\nSENTINEL_ALIAS_7Q: x\n", b"\xff\xfeSENTINEL_ALIAS_7Q"])
def test_a_broken_file_error_chains_no_parser_text(tmp_path, body):
    """The refusal never carries the parser's exception: a traceback of it can't show the file's text."""
    import traceback
    from yggdrasil import config
    path = tmp_path / "broken.yaml"
    path.write_bytes(body if isinstance(body, bytes) else body.encode())
    with pytest.raises(config.BrokenFileError) as caught:
        config._load_strict(path)
    error = caught.value
    assert error.__cause__ is None and error.__context__ is None
    assert "SENTINEL_ALIAS_7Q" not in "".join(traceback.format_exception(error))
