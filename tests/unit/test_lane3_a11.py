"""A11 lane 3 helpers: the agent environment, the git settings allowlist, the process-group stop and the
launch settings."""

import os
import signal
import subprocess
import sys
import time

import pytest
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)

from yggdrasil import util


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _profile(world, monkeypatch, text):
    path = world / "profile.yaml"
    path.write_text(text)
    monkeypatch.setenv("YGG_PROFILE", str(path))
    clear_caches()


# ---------------------------------------------------------------- agent environment

def test_the_profile_timezone_wins_and_tz_is_otherwise_removed(world, monkeypatch):
    from yggdrasil import launcher
    monkeypatch.setenv("TZ", "UTC")
    assert launcher.agent_tz() is None
    (world / "config.yaml").write_text("agent_env_passthrough: [TZ]\nagent_claude_config_dir: ''\n")
    clear_caches()
    assert launcher.agent_tz() == "UTC"
    _profile(world, monkeypatch, "timezone: Europe/Copenhagen\n")
    assert launcher.agent_tz() == "Europe/Copenhagen"
    environment = launcher.child_env({"node": "tl1", "activation": "act-0001", "cli": "claude-code",
                                      "agent_tz": "Europe/Copenhagen"})
    assert environment["TZ"] == "Europe/Copenhagen" and environment["GIT_TERMINAL_PROMPT"] == "0"
    assert "TZ" not in launcher.child_env({"node": "tl1", "activation": "act-0001", "cli": "claude-code"})


@pytest.mark.parametrize("profile,config_text,text", [
    ("timezone: Mars/Base\n", "", "timezone 'Mars/Base' is not a known zone"),
    ("launch_env: {TZ: UTC}\n", "", "launch_env may not set TZ; use the profile's timezone"),
    ("", "agent_env_passthrough: [PATH]\n", "agent_env_passthrough may not name PATH"),
    ("", "agent_env_passthrough: [GIT_DIR]\n", "agent_env_passthrough may not name GIT_DIR"),
    ("", "agent_env_passthrough: [YGG_NODE]\n", "agent_env_passthrough may not name YGG_NODE"),
])
def test_an_unsafe_agent_environment_refuses_the_launch(world, monkeypatch, profile, config_text, text):
    from yggdrasil import launcher
    (world / "config.yaml").write_text(config_text + "agent_claude_config_dir: ''\n")
    if profile:
        _profile(world, monkeypatch, profile)
    clear_caches()
    with pytest.raises(util.YggError) as error:
        launcher.check_agent_env()
    assert error.value.code == util.EXIT_REFUSED and text in error.value.message


def test_agents_get_yggdrasils_own_claude_folder_by_default(world):
    from yggdrasil import launcher
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nagent_claude_config_dir: ~/.config/yggdrasil/agent-claude\n")
    clear_caches()
    environment = launcher.child_env({"node": "tl1", "activation": "act-0001", "cli": "claude-code"})
    folder = os.path.join(os.environ["HOME"], ".config", "yggdrasil", "agent-claude")
    assert environment["CLAUDE_CONFIG_DIR"] == folder and os.path.isdir(folder)
    assert "CLAUDE_CONFIG_DIR" not in launcher.child_env({"node": "tl1", "activation": "act-0001", "cli": "codex"})


def test_launch_settings_always_switch_off_claude_codes_own_trailer(world):
    from yggdrasil import launcher
    content = launcher.settings_content("'/bin/ygg'")
    assert content["includeCoAuthoredBy"] is False and content["attribution"] == {"commit": "", "pr": ""}
    assert content["disableAllHooks"] is False


# ---------------------------------------------------------------- git settings allowlist

def test_every_unknown_key_is_named_and_yggdrasils_keys_need_its_values(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for key, value in (("foo.bar", "1"), ("baz.qux", "2"), ("core.untrackedcache", "true")):
        _git(repo, "config", key, value)
    found = util.repository_helpers(repo)
    assert "foo.bar" in found and "baz.qux" in found and "core.untrackedcache" not in found
    _git(repo, "config", "--unset", "foo.bar")
    _git(repo, "config", "--unset", "baz.qux")
    for key, good, bad in (("commit.gpgsign", "false", "true"), ("core.editor", "true", "vim"),
                           ("sequence.editor", "true", "vim")):
        _git(repo, "config", key, good)
        assert util.repository_helpers(repo) == [] and util.repository_helpers(repo, agents=True) == [], key
        _git(repo, "config", key, bad)
        assert util.repository_helpers(repo) == [key] and util.repository_helpers(repo, agents=True) == [key]
        _git(repo, "config", "--unset", key)


def test_an_fsmonitor_program_refuses_a_launch_but_not_yggdrasils_own_git(tmp_path):
    """A11 AC-41 holds an agent launch to an fsmonitor that is off; Yggdrasil's own git forces it off anyway, so
    its internal operations (A10 AC-13, such as ygg merge lane) still run with one set."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.fsmonitor", str(tmp_path / "fsmon.sh"))
    assert util.repository_helpers(repo) == []
    util.helper_refusal(repo, "gate")  # internal: no refusal
    assert util.repository_helpers(repo, agents=True) == ["core.fsmonitor"]
    with pytest.raises(util.YggError) as error:
        util.helper_refusal(repo, "launch precondition", agents=True)
    assert "core.fsmonitor" in error.value.message
    _git(repo, "config", "core.fsmonitor", "false")
    assert util.repository_helpers(repo, agents=True) == []


# ---------------------------------------------------------------- the process-group stop

def test_group_members_follow_a_child_left_in_the_group(tmp_path):
    from yggdrasil import interlock
    leader = subprocess.Popen([sys.executable, "-c", "import subprocess,sys,time; "
                               "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"],
                              start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(interlock.group_members(leader.pid)) < 2:
            time.sleep(0.05)
        assert len(interlock.group_members(leader.pid)) == 2
        os.kill(leader.pid, signal.SIGKILL)
        leader.wait()
        assert len(interlock.group_members(leader.pid)) == 1  # the child is still in the group
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except OSError:
            pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and interlock.group_members(leader.pid):
        time.sleep(0.05)
    assert interlock.group_members(leader.pid) == []


# ---------------------------------------------------------------- reviewers, crashes and the cancel slots

def test_a_reviewer_edits_only_its_review_folder_and_runs_no_writing_git(world):
    from yggdrasil import permissions, state
    folder = state.root() / "scratch" / "act-0007"
    folder.mkdir(parents=True)
    message = permissions.REVIEWER_MESSAGE
    assert permissions.reviewer_refusal("Edit", {"file_path": str(world / "a.txt")}, "act-0007") == message
    assert permissions.reviewer_refusal("Write", {"file_path": str(folder / "verdict.json")}, "act-0007") is None
    assert permissions.reviewer_refusal("Bash", {"command": "git commit -m x"}, "act-0007") == message
    assert permissions.reviewer_refusal("Bash", {"command": "git show HEAD"}, "act-0007") is None
    assert permissions.reviewer_refusal("Bash", {"command": "ygg status"}, "act-0007") is None


def test_a_signal_yggdrasil_did_not_send_is_a_crash(world, monkeypatch):
    from yggdrasil import launcher, lifecycle
    monkeypatch.setattr(lifecycle, "signal_state", lambda key: None)
    assert launcher.exit_fields({"exit_code": -9}, "act-0001") == {"capacity": False, "interrupted": True,
                                                                     "crashed": True}
    monkeypatch.setattr(lifecycle, "signal_state", lambda key: "sent")
    assert launcher.exit_fields({"exit_code": -2}, "act-0001") == {"capacity": False, "interrupted": True}
    assert launcher.exit_fields({"exit_code": 0}, "act-0001") == {"capacity": False, "ambiguous": True}


def test_a_reserve_check_refuses_before_anything_is_reserved(world, monkeypatch):
    from yggdrasil import launcher, state
    monkeypatch.setattr(launcher, "RESERVE_CHECKS",
                        (("cancel", lambda node: "a cancel on this lane was interrupted; ask your main session to "
                                                 "finish it"),))
    with pytest.raises(util.YggError) as error:
        launcher.launch("tl1")
    assert error.value.code == util.EXIT_REFUSED and "a cancel on this lane was interrupted" in error.value.message
    assert state.runtime("tl1").get("activation") is None


def test_a_cancelled_start_is_not_put_back_whole(world, monkeypatch):
    from yggdrasil import launcher, state
    launcher.abort_effects("tl1", "act-0099", "cancelled: WP-1")
    assert "tl1" not in state.wakes()


@pytest.mark.parametrize("command,refused", [("git log; git commit -am x", True), ("git status && git push", True),
                                             ("git diff --stat", False)])
def test_a_reviewer_command_passes_only_when_every_git_part_reads(world, command, refused):
    from yggdrasil import permissions
    result = permissions.reviewer_refusal("Bash", {"command": command}, "act-0007")
    assert result == (permissions.REVIEWER_MESSAGE if refused else None)


def test_the_lane_creation_commit_is_recorded_once_at_creation(tmp_path, monkeypatch):
    from yggdrasil import launcher
    calls = []
    monkeypatch.setattr(launcher, "lanedrop", type("Fake", (), {
        "record_creation": staticmethod(lambda project, lane, commit: calls.append((project, lane, commit)))}))
    home = tmp_path / "home"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    _git(home, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "root")
    root = _git(home, "rev-parse", "HEAD").stdout.strip()
    path = home / ".ygg-worktrees" / "q1" / "lane" / "tl2"
    launcher.ensure_worktree(str(home), path, "ygg/q1/lane/tl2", "main")
    launcher.ensure_worktree(str(home), path, "ygg/q1/lane/tl2", "main")  # a relaunch: the branch exists
    assert calls == [("q1", "tl2", root)]
    launcher.ensure_worktree(str(home), home / ".ygg-worktrees" / "q1" / "_integration", "ygg/q1/_integration", "main")
    assert calls == [("q1", "tl2", root)]  # not a lane branch


def test_a_slot_whose_module_is_not_built_yet_is_empty(world):
    from yggdrasil import launcher
    assert launcher._slot("nosuchlanemodule:anything") is None
    assert launcher._slot("launcher:review_for") is launcher.review_for
    assert launcher._slot(None) is None


# ---------------------------------------------------------------- review round 1 (a11r1-lane3)

@pytest.mark.parametrize("command", ["/usr/bin/git commit -m x", "'git' commit -m x", 'g"i"t commit',
                                     'git log; env -S "git commit -am x"', 'sh -c "git commit"',
                                     "env git commit -m x", "x=git; $x commit", "GIT_DIR=/elsewhere git log"])
def test_a_reviewer_git_command_under_any_spelling_is_refused(world, command):
    from yggdrasil import permissions
    assert permissions.reviewer_refusal("Bash", {"command": command}, "act-0007") == permissions.REVIEWER_MESSAGE


@pytest.mark.parametrize("command", ["git diff " + "a" * 40 + ".." + "b" * 40, "git show " + "a" * 40,
                                     "/usr/bin/git show HEAD", "ygg status"])
def test_a_reviewer_read_or_other_command_passes(world, command):
    from yggdrasil import permissions
    assert permissions.reviewer_refusal("Bash", {"command": command}, "act-0007") is None


class _Issue(dict):
    def __init__(self, package, created):
        super().__init__(kind="work-package", state="ready-for-test", lane="tl2", created_at=created)
        self.id = package


def test_a_senior_user_tests_the_package_its_owed_work_names(monkeypatch):
    from yggdrasil import launcher, tracker
    monkeypatch.setattr(tracker, "issues", lambda project: [_Issue("WP-3", "2026-11-19T10:00:00Z"),
                                                            _Issue("WP-4", "2026-11-19T11:00:00Z")])
    node = {"id": "su2", "role": "su", "project": "q1", "pairs_with": ["tl2"]}
    nodes = {"tl2": {"id": "tl2", "role": "tl", "project": "q1"}, "su2": node}
    first = [{"kind": "record", "id": "o-3", "package": "WP-3"}, {"kind": "record", "id": "o-4", "package": "WP-4"},
             {"kind": "message", "id": "m-1"}, {"kind": "record", "id": "o-1", "package": "WP-1"}]
    assert launcher.owed_testing_package(node, nodes, first) == "WP-3"
    # WP-4 is also ready for test, so it waits; WP-1 (its criteria, still being built) goes along, so it never
    # needs an activation of its own (AC-94 row 3, round 2).
    assert [i["id"] for i in launcher.one_piece_of_work(node, nodes, first, "WP-3")] == ["o-3", "m-1", "o-1"]
    # WP-3's item was consumed but WP-3 isn't accepted yet: the next activation tests WP-4, not WP-3 again.
    second = [{"kind": "record", "id": "o-4", "package": "WP-4"}]
    assert launcher.owed_testing_package(node, nodes, second) == "WP-4"
    assert launcher.one_piece_of_work(node, nodes, second, "WP-4") == second
    assert launcher._testing_target(node, nodes, "WP-4")["package"] == "WP-4"
    assert launcher._testing_target(node, nodes)["package"] == "WP-3"


def test_an_unreadable_process_list_never_counts_as_a_gone_group(monkeypatch):
    from yggdrasil import interlock
    failed = subprocess.CompletedProcess([], 1, "", "ps: error")
    monkeypatch.setattr(interlock.subprocess, "run", lambda *a, **k: failed)
    assert interlock.group_members(os.getpgrp()) is None
    assert interlock.group_alive(os.getpgrp()) is True  # our own group exists: the kernel says so

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("ps", 10)
    monkeypatch.setattr(interlock.subprocess, "run", timeout)
    assert interlock.group_members(os.getpgrp()) is None and interlock.group_alive(os.getpgrp()) is True
    monkeypatch.setattr(interlock.os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    assert interlock.group_alive(999999) is False  # only a clear "no such group" counts as gone


@pytest.mark.parametrize("code,out,err,kept", [(0, "123\n", "", True), (1, "", "", False),
                                                (1, "", "lsof: status error on x", True), (2, "", "lsof: boom", True)])
def test_a_lock_is_free_only_when_lsof_finds_nobody(monkeypatch, tmp_path, code, out, err, kept):
    from yggdrasil import interlock
    monkeypatch.setattr(interlock.shutil, "which", lambda name: "/usr/sbin/lsof")
    monkeypatch.setattr(interlock.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], code, out, err))
    assert interlock._open_elsewhere(tmp_path / "index.lock") is kept


def test_a_reviewer_activation_keeps_only_its_reviews_package_items():
    from yggdrasil import launcher
    given = [{"kind": "record", "id": "s-1", "package": "WP-1"}, {"kind": "record", "id": "s-2", "package": "WP-2"},
             {"kind": "message", "id": "m-1"}]
    assert [i["id"] for i in launcher.one_review(given, {"id": "rv-0001", "package": "WP-1"})] == ["s-1", "m-1"]



def test_launcher_cli_codex_refuses_a_claude_builder_but_not_a_claude_reviewer(world, monkeypatch):
    from yggdrasil import config, launcher, models
    monkeypatch.setattr(models, "resolve", lambda node_id: {"model": "claude-opus-5-5", "effort": "high"})
    monkeypatch.setattr(launcher, "_reviewer_choice", lambda node: None)
    real_get = config.get
    monkeypatch.setattr(config, "get", lambda key, default=None: "codex" if key == "launcher_cli" else real_get(key, default))
    with pytest.raises(util.YggError) as error:
        launcher.model_for({"id": "tl1", "role": "tl"})
    assert "launcher_cli is codex" in error.value.message
    assert launcher.model_for({"id": "rv1", "role": "reviewer"}) == ("claude-opus-5-5", "high", "claude-code")



class _Package(dict):
    def __init__(self, package, state, lane):
        super().__init__(kind="work-package", state=state, lane=lane)
        self.id = package


@pytest.mark.parametrize("stamped,packages,expected", [
    (True, [_Package("WP-1", "authorised", "tl2")], True),           # reopened after the last approval
    (True, [_Package("WP-1", "merged", "tl2")], False),              # nothing reopened
    (True, [_Package("WP-1", "authorised", "tl9")], False),          # reopened on another lane
    (False, [_Package("WP-1", "authorised", "tl2")], False),         # not every stage approved
])
def test_a_team_lead_may_redo_a_reopened_package_after_the_last_approval(monkeypatch, stamped, packages, expected):
    from yggdrasil import launcher, tracker
    milestones = {"stages": [{"number": 1, "state": "approved"}], **({"all_approved_at": "T"} if stamped else {})}
    monkeypatch.setattr(tracker, "milestones", lambda project: milestones)
    monkeypatch.setattr(tracker, "issues", lambda project: packages)
    assert launcher.reopened_lane_work({"id": "tl2", "role": "tl", "project": "q1"}) is expected
    assert launcher.reopened_lane_work({"id": "w2", "role": "worker", "parent": "tl2", "project": "q1"}) is expected
    assert launcher.reopened_lane_work({"id": "su2", "role": "su", "project": "q1"}) is False


def test_the_stage_gate_still_refuses_a_team_lead_with_no_executing_stage_and_nothing_reopened(world, monkeypatch):
    from yggdrasil import launcher, tracker
    monkeypatch.setattr(launcher, "allowed_home", lambda home: True)
    monkeypatch.setattr(tracker, "project", lambda project: {"id": project, "state": "executing"})
    monkeypatch.setattr(tracker, "milestones", lambda project: {"stages": [{"number": 1, "state": "approved"}],
                                                               "all_approved_at": "T"})
    node = {"id": "tl1", "role": "tl", "project": "p1", "programme": "alpha"}
    monkeypatch.setattr(tracker, "issues", lambda project: [_Package("WP-1", "merged", "tl1")])
    with pytest.raises(util.YggError) as error:
        launcher.readiness(node)
    assert "has no executing stage" in error.value.message
    monkeypatch.setattr(tracker, "issues", lambda project: [_Package("WP-1", "authorised", "tl1")])
    launcher.readiness(node)  # the reopened package can be redone


# ---------------------------------------------------------------- review round 2 (a11r2-lane3)

@pytest.mark.parametrize("command", [
    "git --exec-path=/usr/bin commit -m x", 'eval "git commit -m x"', "exec git commit", 'bash -c "git log"',
    "xargs git commit", 'env -S "git commit"', "echo $(git commit)", "alias g=git; g commit", "git -C /x log",
    "git -c core.x=y log", "nice git commit", "find . -exec git commit ;", "git show HEAD > out.txt", "(git log)",
    "git diff -- src/a.py", "git log --all", 'python3 -c "import os"; git commit'])
def test_a_reviewer_command_passes_only_by_allowlist(world, command):
    from yggdrasil import permissions
    assert permissions.reviewer_refusal("Bash", {"command": command}, "act-0007") == permissions.REVIEWER_MESSAGE


@pytest.mark.parametrize("command", [
    "git show --stat HEAD", "git status --short", "git log --oneline -n 5 HEAD", "git rev-parse HEAD",
    "git branch --list", "git diff --stat", "git log | head -20", "ls -la", ""])
def test_a_reviewer_read_shape_or_plain_command_passes(world, command):
    from yggdrasil import permissions
    assert permissions.reviewer_refusal("Bash", {"command": command}, "act-0007") is None


def test_a_senior_user_retry_continues_the_package_it_failed_on(monkeypatch):
    from yggdrasil import launcher, tracker
    monkeypatch.setattr(tracker, "issues", lambda project: [_Issue("WP-3", "2026-11-19T10:00:00Z"),
                                                            _Issue("WP-4", "2026-11-19T11:00:00Z")])
    monkeypatch.setattr(launcher, "receipt", lambda key: {"testing_package": "WP-4"} if key == "act-0009" else {})
    node = {"id": "su2", "role": "su", "project": "q1", "pairs_with": ["tl2"]}
    nodes = {"tl2": {"id": "tl2", "role": "tl", "project": "q1"}, "su2": node}
    retry = [{"kind": "retry", "id": "act-0009", "reason": "retry"}]
    assert launcher.owed_testing_package(node, nodes, retry) == "WP-4"
    assert launcher.owed_testing_package(node, nodes, [{"kind": "retry", "id": "act-0001"}]) is None


def test_a_team_lead_may_repair_a_redo_the_senior_user_rejected(monkeypatch):
    from yggdrasil import launcher, tracker
    monkeypatch.setattr(tracker, "milestones", lambda project: {"stages": [{"number": 1, "state": "approved"}],
                                                               "all_approved_at": "T"})
    monkeypatch.setattr(tracker, "issues", lambda project: [_Package("WP-1", "rejected", "tl2")])
    assert launcher.reopened_lane_work({"id": "tl2", "role": "tl", "project": "q1"}) is True
    monkeypatch.setattr(tracker, "issues", lambda project: [_Package("WP-1", "accepted", "tl2")])
    assert launcher.reopened_lane_work({"id": "tl2", "role": "tl", "project": "q1"}) is False



# ---------------------------------------------------------------- review round 3 (a11r3-lane3)

@pytest.mark.parametrize("command", [
    'python3.12 -c "pass"', "python3.11 -c \"import os; os.system('g'+'it commit -m x')\"", "perl5.36 -e 1",
    "bash5 -c ls", "zsh -c ls", "tcsh -c ls", "ruby3.2 x.rb", "node18 x.js", "Rscript x.R", "deno run x",
    "sudo git log", "x=1 git log", "git rev-parse --verify HEAD"])
def test_a_reviewer_interpreter_shell_or_unknown_shape_is_refused(world, command):
    from yggdrasil import permissions
    assert permissions.reviewer_refusal("Bash", {"command": command}, None) == permissions.REVIEWER_MESSAGE


@pytest.mark.parametrize("command", [
    "git status -s", "git status --branch", "git status -b", "git status --short --branch", "git diff --staged",
    "git diff --name-status", "git diff HEAD main", "git log --stat", "git log --max-count=5",
    "git show --name-only HEAD", "git branch --list -v", "git --no-pager show HEAD"])
def test_every_signed_read_shape_passes_for_a_reviewer(world, command):
    from yggdrasil import permissions
    assert permissions.reviewer_refusal("Bash", {"command": command}, None) is None


def test_a_retry_takes_its_own_package_when_both_packages_are_ready(monkeypatch):
    from yggdrasil import launcher, tracker
    monkeypatch.setattr(tracker, "issues", lambda project: [_Issue("WP-3", "2026-11-19T10:00:00Z"),
                                                            _Issue("WP-4", "2026-11-19T11:00:00Z")])
    monkeypatch.setattr(launcher, "receipt", lambda key: {"testing_package": "WP-4"} if key == "act-0009" else {})
    node = {"id": "su2", "role": "su", "project": "q1", "pairs_with": ["tl2"]}
    nodes = {"tl2": {"id": "tl2", "role": "tl", "project": "q1"}, "su2": node}
    given = [{"kind": "record", "id": "o-3", "package": "WP-3"}, {"kind": "record", "id": "o-4", "package": "WP-4"},
             {"kind": "retry", "id": "act-0009", "reason": "retry"}]
    package = launcher.owed_testing_package(node, nodes, given)
    assert package == "WP-4"
    # WP-4's record and its retry go together; WP-3's record waits for a later activation.
    assert [i["id"] for i in launcher.one_piece_of_work(node, nodes, given, package)] == ["o-4", "act-0009"]


# ---------------------------------------------------------------- review round 4 (a11r4-lane3)

@pytest.mark.parametrize("command", ["setsid --fork true", 'setsid --fork python3.12 -c "pass"', "command git log",
                                     "nohup git log", "timeout 5 git log", "stdbuf -o0 git log", "echo git commit"])
def test_every_wrapper_the_guard_knows_is_refused_for_a_reviewer(world, command):
    from yggdrasil import permissions
    assert permissions.reviewer_refusal("Bash", {"command": command}, None) == permissions.REVIEWER_MESSAGE


@pytest.mark.parametrize("command", ["git show ygg/git", "git log ygg/git/lane/tl1", "git branch --list git"])
def test_a_revision_or_pattern_named_git_is_judged_by_the_shared_validator(world, command):
    from yggdrasil import permissions
    assert permissions.reviewer_refusal("Bash", {"command": command}, None) is None


def test_a_retry_whose_package_left_testing_never_travels_with_another_package(monkeypatch):
    from yggdrasil import launcher, tracker
    monkeypatch.setattr(tracker, "issues", lambda project: [_Issue("WP-3", "2026-11-19T10:00:00Z"),
                                                            dict(_Issue("WP-4", "2026-11-19T11:00:00Z"),
                                                                 state="rejected")])
    monkeypatch.setattr(launcher, "receipt", lambda key: {"testing_package": "WP-4"} if key == "act-0009" else {})
    node = {"id": "su2", "role": "su", "project": "q1", "pairs_with": ["tl2"]}
    nodes = {"tl2": {"id": "tl2", "role": "tl", "project": "q1"}, "su2": node}
    given = [{"kind": "record", "id": "o-3", "package": "WP-3"}, {"kind": "retry", "id": "act-0009"}]
    package = launcher.owed_testing_package(node, nodes, given)
    kept = launcher.one_piece_of_work(node, nodes, given, package)
    assert package == "WP-3" and [i["id"] for i in kept] == ["o-3"]
    assert launcher.withheld_retries(node, given, kept) == ["WP-4"]
    only = [{"kind": "retry", "id": "act-0009"}]
    assert launcher.one_piece_of_work(node, nodes, only, launcher.owed_testing_package(node, nodes, only)) == []


# ---------------------------------------------------------------- review round 5 (a11r5-lane3)

def test_a_withheld_retry_is_a_visible_hold_in_status_after_the_launch_is_refused(world, monkeypatch):
    """A senior user owing only a retry for a package that left testing (WP-4 rejected): the conductor's launch is
    refused, and `ygg status` shows the same hold, read-only (A7 I4)."""
    from yggdrasil import launcher, reducer, state, status
    retry = {"kind": "retry", "id": "act-0009", "reason": "retry", "retries_used": 1, "at": None}
    monkeypatch.setattr(reducer, "owed", lambda node_id: [dict(retry)] if node_id == "su1" else [])
    real_owed_all = reducer.owed_all
    monkeypatch.setattr(reducer, "owed_all", lambda now=None: dict(real_owed_all(now), su1=[dict(retry)]))
    monkeypatch.setattr(launcher, "_item_package", lambda item: "WP-4" if item.get("id") == "act-0009"
                        else item.get("package"))
    monkeypatch.setattr(launcher, "_ready_for_test", lambda node, nodes: {"WP-3"})
    monkeypatch.setattr(launcher, "_testing_target", lambda node, nodes, prefer=None: {"package": "WP-3"})
    with pytest.raises(util.YggError) as error:
        launcher.launch("su1", explicit=False)
    assert "retry withheld: WP-4 left testing" in error.value.message
    assert state.runtime("su1").get("activation") is None  # refused before anything was reserved
    view = status.build_status()
    node = next(n for n in view["nodes"] if n["id"] == "su1")
    assert node["held_reason"] == "retry withheld: WP-4 left testing"
    assert "su1" in view["held_launches"]
    assert reducer.owed("su1") == [retry]  # read-only: the retry is still owed


# ---------------------------------------------------------------- A11 AC-99 row 3 (confirm_window off)

def test_a_start_held_past_its_confirm_window_shows_as_uncertain(world):
    """The confirm step held a reservation as uncertain (its agent start was held past the window), and the process
    that reserved it is gone: status lists it as uncertain, the same way `settle` keeps it held (AC-43 row 3)."""
    from yggdrasil import launcher, lifecycle, util as yutil
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    started = yutil.process_start(os.getpid())
    with yutil.locked():
        lifecycle.commit("tl1", "act-0042", "reserved", launcher_pid=gone.pid, launcher_started_at=started,
                         work={}, retries_used=0)
    launcher.save_receipt("act-0042", {"node": "tl1", "activation": "act-0042", "outcome": "pending",
                                       "reserved_at": yutil.iso()})
    assert lifecycle.is_uncertain("tl1", "act-0042") is False  # not held by its confirm step
    launcher.update_receipt("act-0042", outcome="uncertain", uncertain_reason="no process reported within the "
                                                                              "confirm window")
    assert lifecycle.is_uncertain("tl1", "act-0042") is True
    assert "act-0042" in lifecycle.uncertain_activations(["tl1"])
