"""v1.1.1 follow-ups: the lane check judges what a commit really adds (a token already held by a parent's tree
passes, wherever it sat), the push guard skips what the home already trusts after a lane sync, redaction stays
linear on hostile text, the Codex sign-in next steps, and the texts for quarantined boxes."""

import json
import random
import re
import subprocess
import time

import pytest

from yggdrasil import clones, codexsignin, config, lanecheck, quarantine, util

# Sample tokens, each built from parts so that no scanner reads this file as holding one.
JWT_IO = ("eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwi"
          "aWF0IjoxNTE2MjM5MDIyfQ" + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
OTHER_JWT = "eyJ" + "hbGciOiJSUzI1NiJ9" + "." + "eyJzdWIiOiJ1c2VyLTEyMyJ9" + "." + "c2lnbmF0dXJlLXBhcnQ"
REFRESH_VALUE = "rt_" + "q" * 30
FIELD = '"refresh_' + 'token"'
GITHUB = "gh" + "p_" + "A1b2C3d4E5" * 4
ADDS = "adds a token-shaped string (a JSON Web Token, an sk- key or a refresh_token); remove it"
LANE = "ygg/alpha-p1/lane/pm1tl1"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


class _NoConfig:
    @staticmethod
    def get(key, default=None):
        return default


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("YGG_SAFETY_ROOT", raising=False)
    monkeypatch.delenv("YGG_PROJECT", raising=False)
    monkeypatch.setattr(lanecheck, "config", _NoConfig)
    monkeypatch.setattr(lanecheck, "denylist", lambda: [])
    monkeypatch.setattr(lanecheck, "_repo_entry", lambda project, repo_: None)
    monkeypatch.setattr(lanecheck, "_packages", lambda project: [])
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: {})
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    return path


def _commit(repo, message, files):
    for name, text in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


# ---------------------------------------------------------------- the commit-time check

def test_a_renamed_file_holding_the_example_token_passes(repo):
    _commit(repo, "L0", {"tests/fixture.py": "TOKEN = '" + JWT_IO + "'\n"})
    _git(repo, "mv", "tests/fixture.py", "tests/moved_fixture.py")
    assert lanecheck.staged_problems(str(repo)) == []


def test_a_copied_file_holding_the_example_token_passes(repo):
    _commit(repo, "L0", {"tests/fixture.py": "TOKEN = '" + JWT_IO + "'\n"})
    (repo / "elsewhere").mkdir()
    (repo / "elsewhere" / "copy.py").write_text("# a copy\nTOKEN = '" + JWT_IO + "'\n")
    _git(repo, "add", ".")
    assert lanecheck.staged_problems(str(repo)) == []


def test_reformatting_an_existing_refresh_token_field_passes(repo):
    _commit(repo, "L0", {"fixture.json": '{"tokens": {' + FIELD + ': "' + REFRESH_VALUE + '"}}\n'})
    (repo / "fixture.json").write_text('{\n  "tokens": {\n    ' + FIELD + ':"' + REFRESH_VALUE + '"\n  }\n}\n')
    _git(repo, "add", ".")
    assert lanecheck.staged_problems(str(repo)) == []


def test_a_new_token_is_still_refused_with_its_line(repo):
    _commit(repo, "L0", {"fixture.txt": JWT_IO + "\n"})
    (repo / "fixture.txt").write_text(JWT_IO + "\nplain\n" + OTHER_JWT + "\n")
    _git(repo, "add", ".")
    assert lanecheck.staged_problems(str(repo)) == ["fixture.txt: line 3 " + ADDS]


def test_a_token_in_a_new_file_that_no_parent_holds_is_refused(repo):
    _commit(repo, "L0", {"README.md": "x\n"})
    (repo / "new.txt").write_text("one\n" + JWT_IO + "\n")
    _git(repo, "add", ".")
    assert lanecheck.staged_problems(str(repo)) == ["new.txt: line 2 " + ADDS]


def test_a_new_refresh_token_value_is_refused_even_beside_an_old_one(repo):
    _commit(repo, "L0", {"fixture.json": '{' + FIELD + ': "' + REFRESH_VALUE + '"}\n'})
    (repo / "fixture.json").write_text('{' + FIELD + ': "' + REFRESH_VALUE + '"}\n'
                                       '{' + FIELD + ': "' + "rt_" + "w" * 30 + '"}\n')
    _git(repo, "add", ".")
    assert lanecheck.staged_problems(str(repo)) == ["fixture.json: line 2 " + ADDS]


def test_in_a_merge_the_tree_of_the_merged_commit_counts_too(repo):
    _commit(repo, "L0", {"README.md": "x\n"})
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "side", {"side.txt": JWT_IO + "\n"})
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "main", {"main.txt": "m\n"})
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    (repo / "moved.txt").write_text(JWT_IO + "\n")
    _git(repo, "add", ".")
    assert lanecheck.staged_problems(str(repo)) == []


def test_a_token_glued_to_a_name_in_a_new_file_is_refused(repo):
    _commit(repo, "L0", {"README.md": "x\n"})
    (repo / "new.txt").write_text("one\ntoken_" + "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "c2lnbmF0dXJlLXBhcnQ" + "\n")
    _git(repo, "add", ".")
    assert lanecheck.staged_problems(str(repo)) == ["new.txt: line 2 " + ADDS]


def test_a_root_commit_counts_every_match(repo):
    (repo / "fixture.txt").write_text(JWT_IO + "\n")
    _git(repo, "add", ".")
    assert lanecheck.staged_problems(str(repo)) == ["fixture.txt: line 1 " + ADDS]


def test_an_older_secret_shape_is_still_judged_over_the_whole_file_when_renamed(repo):
    _commit(repo, "L0", {"config.txt": "token = " + GITHUB + "\n"})
    _git(repo, "mv", "config.txt", "moved.txt")
    assert lanecheck.staged_problems(str(repo)) == ["moved.txt: content holds a secret-shaped token"]


def test_the_range_check_passes_a_rename_and_refuses_a_new_token(repo):
    base = _commit(repo, "L0", {"fixture.txt": JWT_IO + "\n"})
    _git(repo, "mv", "fixture.txt", "renamed.txt")
    _git(repo, "commit", "-q", "-m", "rename")
    tip = _commit(repo, "add", {"other.txt": "b\n" + OTHER_JWT + "\n"})
    assert lanecheck.range_problems(str(repo), base, tip) == [tip[:12] + ": other.txt: line 2 " + ADDS]


# ---------------------------------------------------------------- the push guard after a lane sync

def _synced_lane(repo, monkeypatch):
    """A Claude worktree lane with a commit of its own, then `ygg lane sync` merging a main commit that adds a
    JWT-shaped string (a test fixture) and a key-shaped one."""
    base = _commit(repo, "L0", {"README.md": "x\n"})
    _git(repo, "checkout", "-q", "-b", LANE)
    _commit(repo, "lane work", {"lane.txt": "lane\n"})
    _git(repo, "checkout", "-q", "main")
    main = _commit(repo, "main adds a fixture", {"tests/fixture.py": "TOKEN = '" + JWT_IO + "'\n",
                                                 "tests/keys.txt": "token = " + GITHUB + "\n"})
    _git(repo, "checkout", "-q", LANE)
    _git(repo, "merge", "-q", "--no-ff", "-m", "sync main into the lane", "main")
    sync = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(lanecheck, "recorded_syncs",
                        lambda project: {sync: {"lane": "pm1tl1", "main": main}} if project == "alpha-p1" else {})
    return base, main, sync


def test_a_lane_that_synced_a_main_token_can_push(repo, monkeypatch):
    base, _main, sync = _synced_lane(repo, monkeypatch)
    assert lanecheck.range_problems(str(repo), base, sync) == []


def test_after_a_sync_a_lane_commit_adding_a_new_token_is_still_refused(repo, monkeypatch):
    base, _main, _sync = _synced_lane(repo, monkeypatch)
    tip = _commit(repo, "lane adds a token", {"lane.txt": "lane\n" + OTHER_JWT + "\n"})
    assert lanecheck.range_problems(str(repo), base, tip) == [tip[:12] + ": lane.txt: line 2 " + ADDS]


def test_a_sync_merge_is_judged_by_what_it_adds_beyond_its_parents(repo, monkeypatch):
    base = _commit(repo, "L0", {"README.md": "x\n"})
    _git(repo, "checkout", "-q", "-b", LANE)
    _commit(repo, "lane work", {"lane.txt": "lane\n"})
    _git(repo, "checkout", "-q", "main")
    main = _commit(repo, "main", {"main.txt": "m\n"})
    _git(repo, "checkout", "-q", LANE)
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "main")
    (repo / "smuggled.txt").write_text(OTHER_JWT + "\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "sync")
    sync = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: {sync: {"lane": "pm1tl1", "main": main}})
    assert lanecheck.range_problems(str(repo), base, sync) == [sync[:12] + ": smuggled.txt: line 1 " + ADDS]


def test_the_pushed_branchs_project_names_the_syncs_whatever_the_checkout(repo, monkeypatch):
    base, _main, sync = _synced_lane(repo, monkeypatch)
    _git(repo, "checkout", "-q", "--detach", sync)
    assert lanecheck.range_problems(str(repo), base, sync, project="alpha-p1") == []
    assert lanecheck.range_problems(str(repo), base, sync) != []


def test_without_a_recorded_sync_main_commits_in_the_range_are_judged(repo, monkeypatch):
    base, _main, sync = _synced_lane(repo, monkeypatch)
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: {})
    problems = lanecheck.range_problems(str(repo), base, sync)
    assert any("tests/keys.txt: content holds a secret-shaped token" in p for p in problems)


# ---------------------------------------------------------------- redaction stays linear

@pytest.mark.parametrize("unit", ["eyJ", "eyJ_", "eyJ-", "eyJ."])
def test_redaction_of_a_long_run_of_jwt_prefixes_is_fast(unit):
    started = time.monotonic()
    assert util.redact(unit * 100000) == unit * 100000
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("before, after", [("", ""), ("Authorization: Bearer ", ""), ("token=", "&x=1"),
                                           ('{"id_token": "', '"}'), ("line\n", "\n"), ("token_", ""),
                                           ("token-", ""), ("a%3D", ""), ("x" * 50, "")])
def test_a_jwt_is_still_redacted_where_it_usually_sits(before, after):
    assert util.redact(before + JWT_IO + after) == before + "[REDACTED]" + after
    assert util.redact(before + OTHER_JWT.rsplit(".", 1)[0] + after) == before + "[REDACTED]" + after


def test_a_jwt_after_an_escaped_newline_in_json_is_redacted():
    text = json.dumps({"log": "line one\n" + JWT_IO + "\nline two"})
    shown = util.redact(text)
    assert JWT_IO not in shown and "eyJ" not in shown
    assert shown == json.dumps({"log": "line one\n[REDACTED]\nline two"})


def test_the_other_shapes_are_redacted_as_before():
    legacy = "sk-" + "Ab1" * 11
    refresh = '{' + FIELD + ': "' + REFRESH_VALUE + '"}'
    assert util.redact("key=" + legacy) == "key=[REDACTED]"
    assert util.redact(refresh) == '{' + FIELD + ': "[REDACTED]"}'
    assert util.redact("t " + GITHUB + " e") == "t [REDACTED] e"
    assert util.redact("AKIA" + "ABCDEFGHIJKLMNOP" + "x") == "[REDACTED]x"


# The JSON Web Token part of the redaction as it was before it was made linear, for comparison on small inputs.
_OLD = re.compile(r"(?:lin_api_|lin_oauth_|sk-(?:ant|proj|svcacct|admin)-|gh[opsur]_|github_pat_)[A-Za-z0-9_\-]*"
                  r"|(?:AKIA|ASIA)[A-Z0-9]{16}|xox[abposr]-[A-Za-z0-9-]+"
                  r"|eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
                  r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
                  r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9]{32,}"
                  r'|(?P<refresh>\\?"refresh_token\\?"\s*:\s*\\?")[^"\\]{20,}')


def test_redaction_matches_the_earlier_patterns_on_many_small_inputs():
    pieces = ["eyJ", "a", "b1", "_", "-", ".", " ", "=", "%3D", "\\n", "lin_api_", "AKIA", "ABCDEFGH", "x" * 10,
              "eyJ" + "c" * 10, "." + "d" * 10, "ghp_"]
    generator = random.Random(7)
    for _ in range(4000):
        text = "".join(generator.choice(pieces) for _ in range(generator.randint(1, 14)))
        expected = _OLD.sub(lambda found: (found.group("refresh") or "") + "[REDACTED]", text)
        assert util.redact(text) == expected, text


# ---------------------------------------------------------------- the Codex sign-in next steps

@pytest.fixture
def signin(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("YGG_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.delenv("YGG_PROFILE", raising=False)
    monkeypatch.delenv("YGG_TEST_PLATFORM", raising=False)
    (tmp_path / "config.yaml").write_text("schema: ygg.config.v1\n")
    for cached in (config.file_layers, config.profile, config.effective):
        getattr(cached, "cache_clear", lambda: None)()
    yield tmp_path
    for cached in (config.file_layers, config.profile, config.effective):
        getattr(cached, "cache_clear", lambda: None)()


PLATFORM_STEP = "Codex's shared sign-in is not probed on linux; use a model provider with its own key"


@pytest.mark.parametrize("version", ["codex-cli 0.171.0", "codex-cli 0.170.0", "unknown", None])
def test_on_a_platform_with_no_probed_version_the_step_is_the_platform_whatever_the_version(signin, monkeypatch,
                                                                                         version):
    monkeypatch.setenv("YGG_TEST_PLATFORM", "linux")
    assert codexsignin.not_probed_step(version) == PLATFORM_STEP


def test_on_a_probed_platform_an_unprobed_version_keeps_its_install_step(signin, monkeypatch):
    monkeypatch.setenv("YGG_TEST_PLATFORM", "darwin")
    assert codexsignin.not_probed_step("codex-cli 0.171.0") == (
        "Codex 0.171.0 is not probed for the shared sign-in; use a model provider with its own key, "
        "or install codex-cli 0.170.0")


HUB = {"name": "hub", "base_url": "http://127.0.0.1:18080/v1", "env_key": "HUB_KEY", "wire_api": "responses"}


def test_a_provider_key_the_conductor_has_but_agents_do_not_get_names_the_passthrough(signin, monkeypatch):
    monkeypatch.setenv("HUB_KEY", "hk-conductor")
    assert codexsignin.route({}, HUB, True) == (
        "none", "list HUB_KEY under agent_env_passthrough so agents get it")


def test_a_provider_key_the_conductor_lacks_keeps_the_conductor_step(signin, monkeypatch):
    monkeypatch.delenv("HUB_KEY", raising=False)
    assert codexsignin.route({}, HUB, True) == ("none", "set HUB_KEY for the conductor")
    monkeypatch.setenv("HUB_KEY", "   ")
    assert codexsignin.route({}, HUB, True) == ("none", "set HUB_KEY for the conductor")


def test_a_provider_key_already_passed_through_keeps_the_conductor_step(signin, monkeypatch):
    (signin / "config.yaml").write_text("schema: ygg.config.v1\nagent_env_passthrough: [HUB_KEY]\n")
    for cached in (config.file_layers, config.profile, config.effective):
        getattr(cached, "cache_clear", lambda: None)()
    monkeypatch.setenv("HUB_KEY", "hk-conductor")
    assert codexsignin.route({}, HUB, True) == ("none", "set HUB_KEY for the conductor")


# ---------------------------------------------------------------- texts for quarantined boxes

PAGE = "https://github.com/Motaghi/yggdrasil/blob/main/docs/security.md#quarantined-boxes"


def test_the_quarantine_warning_names_the_public_page():
    assert PAGE in quarantine.WARNING
    assert "as docs/security.md says" not in quarantine.WARNING
    assert "cp -RP" in quarantine.WARNING


def test_the_clone_cap_refusal_asks_for_a_complete_copy_and_names_the_page(monkeypatch):
    now = util.iso()
    records = [{"odd_check": "1", "path": "/", "at": now} for _ in range(3)]
    monkeypatch.setattr(clones, "quarantine_records", lambda project, tl: records)
    text = clones.cap_refusal("alpha-p1", "pm1tl1")
    assert "replaced its clone 3 times in the last 24 hours" in text and "moved out of the lane folder" in text
    assert "copy it with cp -RP and check that the copy is complete" in text
    assert PAGE in text


# ---------------------------------------------------------------- what a sync record and a parent may vouch for

def test_a_sync_record_naming_an_agent_commit_off_main_excludes_nothing(repo, monkeypatch):
    base = _commit(repo, "L0", {"README.md": "x\n"})
    _git(repo, "checkout", "-q", "-b", LANE)
    agent = _commit(repo, "agent adds a token", {"lane.txt": "lane\n" + OTHER_JWT + "\n"})
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: {"f" * 40: {"lane": "pm1tl1", "main": agent}})
    assert lanecheck.range_problems(str(repo), base, agent) == [agent[:12] + ": lane.txt: line 2 " + ADDS]


def test_a_recorded_merge_whose_parents_lack_the_recorded_main_is_judged_in_full(repo, monkeypatch):
    base = _commit(repo, "L0", {"README.md": "x\n"})
    main = _commit(repo, "main", {"main.txt": "m\n"})
    _git(repo, "checkout", "-q", "-b", "side", base)
    side = _commit(repo, "side adds a token", {"side.txt": OTHER_JWT + "\n"})
    _git(repo, "checkout", "-q", "-b", LANE, base)
    _commit(repo, "lane work", {"lane.txt": "lane\n"})
    _git(repo, "merge", "-q", "--no-ff", "-m", "not a sync of main " + GITHUB, "side")
    merge = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(lanecheck, "recorded_syncs", lambda project: {merge: {"lane": "pm1tl1", "main": main}})
    problems = lanecheck.range_problems(str(repo), base, merge)
    assert side[:12] + ": side.txt: line 1 " + ADDS in problems
    assert merge[:12] + ": the commit message holds a secret-shaped token" in problems


def test_a_value_first_committed_as_plain_text_is_refused_inside_a_refresh_token_field(repo):
    _commit(repo, "L0", {"notes.txt": "an ordinary line " + REFRESH_VALUE + "\n"})
    (repo / "auth.json").write_text('{' + FIELD + ': "' + REFRESH_VALUE + '"}\n')
    _git(repo, "add", ".")
    assert lanecheck.staged_problems(str(repo)) == ["auth.json: line 1 " + ADDS]


def test_a_refresh_token_field_moved_to_another_file_passes(repo):
    _commit(repo, "L0", {"old.json": '{"tokens": {' + FIELD + ': "' + REFRESH_VALUE + '"}}\n'})
    _git(repo, "mv", "old.json", "new.json")
    (repo / "new.json").write_text('{\n  ' + FIELD + ' :  "' + REFRESH_VALUE + '"\n}\n')
    _git(repo, "add", ".")
    assert lanecheck.staged_problems(str(repo)) == []
