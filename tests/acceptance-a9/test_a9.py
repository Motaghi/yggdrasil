from copy import deepcopy
import shlex

import pytest

from driver_a9 import INT, L1, L2, SAFE


READ = {"tool_name": "Read", "tool_input": {"file_path": "README.md"}}


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


@pytest.mark.ac("AC-01")
@pytest.mark.parametrize("command", [
    "git push --no-verify origin ygg/alpha-p1/lane/pm1tl1",
    "git -c core.hooksPath=/dev/null push origin ygg/alpha-p1/lane/pm1tl1",
    "git -c alias.up='!git push' up origin ygg/alpha-p1/lane/pm1tl1",
], ids=["no-verify", "hooks-path", "alias"])
def test_hook_denies_hook_bypass(a9_tree, command):
    result = a9_tree.hook(bash(command), node="pm1tl1")
    assert result.returncode == 2
    assert result.stderr.strip() == "Hard stop (hook-bypass) denied: ask the user through your PM."


@pytest.mark.ac("AC-02")
def test_ref_write_denied_and_lane_push_allowed(a9_tree):
    denied = a9_tree.hook(bash("git update-ref refs/heads/main HEAD"), node="pm1tl1")
    assert denied.returncode == 2
    assert denied.stderr.strip() == "Hard stop (ref-write) denied: ask the user through your PM."
    allowed = a9_tree.hook(bash("git push origin ygg/alpha-p1/lane/pm1tl1"), node="pm1tl1")
    assert allowed.returncode == 0


@pytest.mark.ac("AC-03")
def test_push_requires_approved_destination_url(a9_tree):
    a9_tree.push_seed(branches=(L1,))
    allowed = a9_tree.push(url=a9_tree.root / "remote-a.git")
    assert allowed.returncode == 0
    wrong = a9_tree.push(url=a9_tree.root / "remote-b.git")
    assert wrong.returncode == 4
    assert "is not the approved remote of" in wrong.stderr
    assert "remote-b.git" in wrong.stderr
    missing = a9_tree.push()
    assert missing.returncode == 4
    assert "destination URL missing" in missing.stderr


@pytest.mark.ac("AC-04")
def test_no_approved_remote_allows_only_url_less_check(a9_tree):
    a9_tree.push_seed(remote=False, branches=(L1,))
    allowed = a9_tree.push()
    assert allowed.returncode == 0
    denied = a9_tree.push(url=a9_tree.root / "remote-b.git")
    assert denied.returncode == 4
    assert "no approved remote recorded for" in denied.stderr


@pytest.mark.ac("AC-05")
def test_team_lead_pushes_only_own_lane(a9_push):
    url = a9_push.root / "remote-a.git"
    allowed = a9_push.push(L1, node="pm1tl1", url=url)
    assert allowed.returncode == 0
    for branch in (L2, INT):
        denied = a9_push.push(branch, node="pm1tl1", url=url)
        assert denied.returncode == 4
        assert "pm1tl1 may push only ygg/alpha-p1/lane/pm1tl1" in denied.stderr


@pytest.mark.ac("AC-06")
def test_worker_and_pm_push_only_their_branches(a9_push):
    url = a9_push.root / "remote-a.git"
    worker = a9_push.push(L1, node="pm1tl1w1", url=url)
    assert worker.returncode == 0
    pm = a9_push.push(INT, node="pm1", url=url)
    assert pm.returncode == 0
    denied = a9_push.push(L1, node="pm1", url=url)
    assert denied.returncode == 4
    assert "pm1 may push only ygg/alpha-p1/_integration" in denied.stderr


@pytest.mark.ac("AC-07")
def test_senior_user_cannot_push(a9_push):
    result = a9_push.push(L1, node="pm1su1", url=a9_push.root / "remote-a.git")
    assert result.returncode == 4
    assert "pm1su1 may not push" in result.stderr


@pytest.mark.ac("AC-08")
@pytest.mark.parametrize("case", ["git-dir", "add-revert", "replace"])
def test_repository_and_transmitted_history_checked(a9_ready, case):
    a9_ready.git_labels()
    if case == "git-dir":
        a9_ready.branch(L1, "b2c3d4e")
        result = a9_ready.push(repo="/", env={"GIT_DIR": a9_ready.repo_a / ".git"})
        message = "no approved PID naming this repo"
    elif case == "add-revert":
        a9_ready.delete_workflow_commit()
        a9_ready.branch(L1, "f6a7b8c")
        result = a9_ready.push(local="f6a7b8c", remote="b2c3d4e")
        message = "workflow-change"
    else:
        a9_ready.branch(L1, "e5f6a7b")
        a9_ready.git(a9_ready.repo_a, "replace", a9_ready.sha("e5f6a7b"), a9_ready.sha("b2c3d4e"))
        result = a9_ready.push(local="e5f6a7b", remote="b2c3d4e")
        message = "workflow-change"
    assert result.returncode == 4
    assert message in result.stderr


@pytest.mark.ac("AC-09")
def test_protection_requires_safe_force_push_and_deletion_policy(a9_factory):
    for field in (None, "allow_force_pushes", "allow_deletions"):
        driver = a9_factory(field or "safe")
        body = deepcopy(SAFE)
        if field:
            body[field]["enabled"] = True
        driver.protection_body(body)
        result = driver.run("pid", "approve", "alpha-p1")
        if field is None:
            assert result.returncode == 0
            assert driver.protection_alerts() == []
        else:
            assert result.returncode == 3
            assert field in result.stderr


@pytest.mark.ac("AC-10")
@pytest.mark.parametrize("case", ["admins", "exists", "exists-warn"])
def test_protection_requires_admin_enforcement_and_reviews(a9_tree, case):
    if case == "admins":
        body = deepcopy(SAFE)
        body["enforce_admins"]["enabled"] = False
        message = "enforce_admins"
    else:
        body = {"url": "https://example.invalid/protection"}
        message = "required_pull_request_reviews"
    a9_tree.protection_body(body)
    if case == "exists-warn":
        a9_tree.pid(branch_protection="warn")
    before = a9_tree.protection_alerts()
    result = a9_tree.run("pid", "approve", "alpha-p1")
    if case == "exists-warn":
        assert result.returncode == 0
        new = a9_tree.protection_alerts()[len(before):]
        assert len(new) == 1
        assert message in new[0]["message"]
    else:
        assert result.returncode == 3
        assert message in result.stderr


@pytest.mark.ac("AC-11")
@pytest.mark.parametrize("kind", ["criteria", "accept", "reject"])
def test_parent_cannot_write_child_judgement(a9_ready, kind):
    if kind == "criteria":
        a9_ready.package()
        args = ("tracker", "write", "--as", "pm1su1", "--project", "alpha-p1",
                "--package", "WP-41", "--kind", "criteria", "--body", "Saves load")
        message = "criteria for pm1su1 must be written by pm1su1 itself"
    else:
        a9_ready.completed_package()
        args = ("acceptance", "record", "--from", "pm1su1", "--package", "WP-41",
                "--sha", a9_ready.sha("b2c3d4e"), "--verdict", kind)
        message = ("acceptance" if kind == "accept" else "rejection") + " for pm1su1 must be recorded by pm1su1 itself"
    result = a9_ready.run(*args, env={"YGG_NODE": "pm1"})
    assert result.returncode == 3
    assert result.stderr.strip() == message


@pytest.mark.ac("AC-12")
def test_review_requires_reviewer_itself(a9_ready):
    a9_ready.completed_package()
    result = a9_ready.run("review", "record", "--from", "pm1tl1w1", "--package", "WP-41",
                          "--round", "1", "--verdict", "pass", env={"YGG_NODE": "pm1tl1"})
    assert result.returncode == 3
    assert result.stderr.strip() == "review for pm1tl1w1 must be recorded by pm1tl1w1 itself"


@pytest.mark.ac("AC-12")
def test_acceptance_records_real_caller(a9_ready):
    a9_ready.completed_package()
    before = a9_ready.package_records()
    result = a9_ready.run("acceptance", "record", "--from", "pm1su1", "--package", "WP-41",
                          "--sha", a9_ready.sha("b2c3d4e"), "--verdict", "accept",
                          env={"YGG_NODE": "pm1su1"})
    assert result.returncode == 0
    new = [record for record in a9_ready.package_records()[len(before):]
           if record.get("kind") == "acceptance"]
    assert len(new) == 1
    assert new[0]["caller"] == "pm1su1"


@pytest.mark.ac("AC-13")
@pytest.mark.parametrize("verb,args", [
    ("launch", ("launch", "pm1tl1")),
    ("_supervise", ("_supervise", "act-0001")),
    ("conductor tick", ("conductor", "tick")),
], ids=["launch", "supervise", "conductor"])
def test_launch_and_conductor_verbs_are_user_only(a9_ready, verb, args):
    result = a9_ready.run(*args, env={"YGG_NODE": "pm1"})
    assert result.returncode == 3
    assert result.stderr.strip() == f"REFUSED: user-only: '{verb}' is a user verb and cannot run in a managed session"


@pytest.mark.ac("AC-14")
@pytest.mark.parametrize("verb,args", [
    ("heimdall tick", ("heimdall", "tick")),
    ("rehydrate", ("rehydrate",)),
    ("event ci", ("event", "ci", "--repo", "repo_a", "--branch", L1, "--status", "passed")),
], ids=["heimdall", "rehydrate", "event"])
def test_liveness_rehydrate_and_event_verbs_are_user_only(a9_ready, verb, args):
    if verb == "event ci":
        args = (*args[:3], a9_ready.repo_a, *args[4:])
    result = a9_ready.run(*args, env={"YGG_NODE": "pm1"})
    assert result.returncode == 3
    assert result.stderr.strip() == f"REFUSED: user-only: '{verb}' is a user verb and cannot run in a managed session"


@pytest.mark.ac("AC-15")
@pytest.mark.parametrize("verb", ["attach", "detach"])
def test_cockpit_lock_verbs_are_user_only(a9_ready, verb):
    result = a9_ready.run(verb, "--session", "s1", env={"YGG_NODE": "pm1"})
    assert result.returncode == 3
    assert result.stderr.strip() == f"REFUSED: user-only: '{verb}' is a user verb and cannot run in a managed session"


@pytest.mark.ac("AC-16")
def test_hardstop_requires_self_and_user_can_tick(a9_ready):
    denied = a9_ready.run("hardstop", "check", "--node", "pm1tl1", "--action", "push",
                          "--target", "main", env={"YGG_NODE": "pm1"})
    assert denied.returncode == 3
    assert denied.stderr.strip() == "hardstop: pm1 may only ask for itself"
    allowed = a9_ready.run("conductor", "tick", env={"YGG_NODE": None})
    assert allowed.returncode == 0


@pytest.mark.ac("AC-17")
def test_managed_reads_are_scoped_and_user_sees_beta_decision(a9_ready):
    a9_ready.ready("beta-p1")
    a9_ready.beta_decision()
    result, status = a9_ready.run_json("status", "--json", env={"YGG_NODE": "pm1"})
    assert result.returncode == 0
    assert isinstance(status, dict)
    assert [programme["id"] for programme in status["programmes"]] == ["alpha"]
    assert all(node["programme"] != "beta" for node in status["nodes"])
    result, managed = a9_ready.run_json("pending", "--json", env={"YGG_NODE": "pm1"})
    assert result.returncode == 0
    assert isinstance(managed, dict)
    assert all(decision["id"] != "D-0005" for decision in managed["decisions"])
    result, user = a9_ready.run_json("pending", "--json", env={"YGG_NODE": None})
    assert result.returncode == 0
    assert isinstance(user, dict)
    assert any(decision["id"] == "D-0005" for decision in user["decisions"])


@pytest.mark.ac("AC-18")
def test_merge_mode_ignores_unprotected_programme_field(a9_ready):
    a9_ready.git_labels()
    a9_ready.branch(INT, "b2c3d4e")
    a9_ready.change_json(a9_ready.tracker / "initiatives" / "alpha" / "initiative.json", merge_mode="local")
    a9_ready.run("merge", "check", "alpha-p1", "--repo", a9_ready.repo_a)
    result = a9_ready.run("merge", "main", "alpha-p1", "--repo", a9_ready.repo_a, "--print")
    assert result.returncode == 0
    assert result.stdout.startswith("Merge mode prepare: main is unchanged.")
    assert a9_ready.symbolic(a9_ready.ref("main")) == "a1b2c3d"
    explained = a9_ready.command_json("config", "explain", "merge_mode", "--project", "alpha-p1", "--json")
    assert isinstance(explained, dict)
    assert explained["value"] == "prepare"


@pytest.mark.ac("AC-19")
def test_launched_environment_is_allowlisted(a9_ready):
    a9_ready.env.update(GH_TOKEN="gh-secret-1", OPENAI_API_KEY="sk-test-2",
                        AWS_SECRET_ACCESS_KEY="aws-3", YGG_TEST_MODE="1")
    a9_ready.at("2026-11-16T08:00:00Z")
    result = a9_ready.run("launch", "pm1tl1")
    assert result.returncode == 0
    a9_ready.wait_exit("pm1tl1")
    launches = a9_ready.launches("pm1tl1")
    assert launches
    environment = launches[-1]["env"]
    for name in ("GH_TOKEN", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "YGG_TEST_MODE", "YGG_NOW"):
        assert name not in environment
    assert environment["YGG_NODE"] == "pm1tl1"
    receipts = a9_ready.a5_receipts("pm1tl1")
    assert receipts
    assert environment["YGG_ACTIVATION"] == receipts[-1]["activation"]
    assert environment["PATH"] == a9_ready.env["PATH"]


@pytest.mark.ac("AC-19")
@pytest.mark.parametrize("name,value", [("YGG_NODE", "user"), ("YGG_TEST_MODE", "1")],
                         ids=["identity", "test-mode"])
def test_launch_overlay_cannot_set_reserved_keys(a9_ready, name, value):
    a9_ready.example_profile(launch_env={name: value})
    before = a9_ready.launches()
    result = a9_ready.run("launch", "pm1tl1")
    assert result.returncode == 1
    assert result.stderr.strip() == "launch_env may not set " + name
    if name == "YGG_NODE":
        assert a9_ready.launches() == before


@pytest.mark.ac("AC-20")
def test_ordinary_launch_overlay_reaches_agent(a9_ready):
    a9_ready.example_profile(launch_env={"EXTRA_FLAG": "on"})
    result = a9_ready.run("launch", "pm1tl1")
    assert result.returncode == 0
    a9_ready.wait_exit("pm1tl1")
    launches = a9_ready.launches("pm1tl1")
    assert launches
    assert launches[-1]["env"]["EXTRA_FLAG"] == "on"


@pytest.mark.ac("AC-20")
def test_profile_show_redacts_secret(a9_tree):
    a9_tree.example_profile(linear_client_secret="sec-xyz-9")
    result = a9_tree.run("profile", "show")
    assert result.returncode == 0
    assert "sec-xyz-9" not in result.stdout
    assert "[redacted]" in result.stdout


@pytest.mark.ac("AC-21")
def test_launch_hook_uses_absolute_installed_ygg(a9_ready):
    result = a9_ready.run("launch", "pm1tl1")
    assert result.returncode == 0
    a9_ready.wait_exit("pm1tl1")
    settings = a9_ready.launch_settings("pm1tl1")
    assert isinstance(settings, dict)
    assert settings["disableAllHooks"] is False
    commands = [hook["command"] for group in settings["hooks"]["PreToolUse"]
                for hook in group["hooks"]]
    assert commands == [a9_ready.expected_hook_command()]


@pytest.mark.ac("AC-21")
def test_launch_without_ygg_on_path_is_refused(a9_ready):
    before = a9_ready.launches()
    result = a9_ready.run("launch", "pm1tl1", env={"PATH": a9_ready.system_path()})
    assert result.returncode == 3
    assert result.stderr.strip() == "REFUSED: launch precondition: no ygg executable on PATH"
    assert a9_ready.launches() == before


@pytest.mark.ac("AC-22")
def test_hook_denies_malformed_payload(a9_tree):
    result = a9_tree.run("hook", "pretool", env={"YGG_NODE": "pm1tl1"}, stdin="not json")
    assert result.returncode == 2
    assert result.stderr.strip() == "Malformed hook payload; stop work now."


@pytest.mark.ac("AC-22")
@pytest.mark.parametrize("mode", ["malformed", "directory"])
def test_hook_denies_corrupt_or_unreadable_tracker(a9_ready, mode):
    a9_ready.corrupt_nodes(mode)
    result = a9_ready.hook(READ, node="pm1tl1")
    assert result.returncode == 2
    assert result.stderr.strip() == "Yggdrasil tracker unavailable or corrupt; stop work now."


@pytest.mark.ac("AC-23")
def test_stop_blocks_own_programme_until_resume(a9_factory):
    own = a9_factory("own-programme")
    own.ready()
    own.run("stop", "alpha")
    stopped = own.hook(READ, node="pm1tl1")
    assert stopped.returncode == 2
    assert stopped.stderr.strip() == "Yggdrasil emergency stop for this node; stop work now."
    own.run("resume", "alpha")
    resumed = own.hook(READ, node="pm1tl1")
    assert resumed.returncode == 0
    other = a9_factory("other-programme")
    other.ready()
    other.ready("beta-p1")
    other.run("stop", "alpha")
    unaffected = other.hook(READ, node="pm2tl1")
    assert unaffected.returncode == 0


@pytest.mark.ac("AC-24")
@pytest.mark.parametrize("tracker", ["readonly", "writable"])
def test_stop_kills_agent_before_tracker_write(a9_ready, tracker):
    a9_ready.keep_running()
    a9_ready.run("launch", "pm1tl1")
    pid = a9_ready.fake_agent_pid("pm1tl1")
    if tracker == "readonly":
        a9_ready.readonly(a9_ready.tracker)
    result = a9_ready.run("stop")
    if tracker == "readonly":
        assert result.returncode == 8
        assert "stopped 1 session(s); tracker write failed" in result.stderr
    else:
        assert result.returncode == 0
        assert result.stdout.strip() == "stopped 1 session(s)"
    assert a9_ready.fake_agent_running(pid) is False


@pytest.mark.ac("AC-25")
def test_launch_refuses_foreign_checkout_at_worktree_path(a9_ready):
    a9_ready.foreign_worktree()
    result = a9_ready.run("launch", "pm1tl1")
    assert result.returncode == 3
    assert "is not the worktree of ygg/alpha-p1/lane/pm1tl1" in result.stderr


@pytest.mark.ac("AC-25")
@pytest.mark.parametrize("kind", ["report", "map"])
def test_managed_ingestion_refuses_file_outside_work_area(a9_ready, kind):
    path = a9_ready.outside_file(kind + ".md")
    if kind == "report":
        args = ("stage", "end", "alpha-p1", "--within-tolerance", "yes", "--report", path)
    else:
        args = ("routemap", "write", "--as", "pm1", "--project", "alpha-p1", "--file", path)
    result = a9_ready.run(*args, env={"YGG_NODE": "pm1"})
    assert result.returncode == 3
    assert "report must be inside the tracker or your worktree" in result.stderr


@pytest.mark.ac("AC-26")
def test_printed_merge_command_quotes_repository_with_space(a9_tree):
    repo = a9_tree.quoted_repo()
    a9_tree.run("merge", "check", "alpha-p1", "--repo", repo)
    result = a9_tree.run("merge", "main", "alpha-p1", "--repo", repo, "--print")
    assert result.returncode == 0
    assert shlex.quote(str(repo)) in result.stdout
