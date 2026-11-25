import pytest


@pytest.mark.ac("AC-01")
def test_current_folder_plugin_paths_require_explicit_trust(a10_factory):
    trusted = a10_factory("trusted", tree=False)
    folder, config, _ = trusted.untrusted_plugins()
    result = trusted.run("config", "validate", cwd=folder, env={"YGG_CONFIG": config})
    assert result.returncode == 0

    untrusted = a10_factory("untrusted", tree=False)
    folder, config, marker = untrusted.untrusted_plugins()
    result = untrusted.run("config", "validate", cwd=folder)
    assert result.returncode == 1
    assert result.stderr.strip() == (
        f"{config}: plugin_paths may be set only in a config named by YGG_CONFIG, "
        "the home config, a profile or the environment"
    )
    assert not untrusted.exists(marker)


@pytest.mark.ac("AC-01")
def test_current_folder_cannot_select_tracker_plugin(a10_factory):
    driver = a10_factory("selection", tree=False)
    folder, config, marker = driver.untrusted_plugins(tracker=True)
    result = driver.run("config", "validate", cwd=folder)
    assert result.returncode == 1
    assert result.stderr.strip() == (
        f"{config}: tracker: memory may be set only in a config named by YGG_CONFIG, "
        "the home config, a profile or the environment"
    )
    assert not driver.exists(marker)


@pytest.mark.ac("AC-02")
def test_duplicate_names_refused_and_untrusted_listing_skipped(a10_factory):
    untrusted = a10_factory("untrusted", tree=False)
    folder, _, marker = untrusted.untrusted_plugins()
    result, entries = untrusted.run_json("plugins", "list", "--json", cwd=folder)
    assert result.returncode == 0
    assert isinstance(entries, list)
    assert all(entry["name"] != "evil" for entry in entries)
    assert not untrusted.exists(marker)

    duplicate = a10_factory("duplicate", tree=False)
    duplicate.duplicate_plugins()
    result = duplicate.run("plugins", "list", "--json")
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "plugin name memory is registered twice in yggdrasil.trackers "
        "(ygg_memtracker, ygg_othertracker)"
    )


@pytest.mark.ac("AC-02")
def test_duplicate_plugin_selection_refused(a10_factory):
    driver = a10_factory("duplicate", tree=False)
    driver.duplicate_plugins()
    config = driver.root / "explicit.yaml"
    driver.write(config, "tracker: memory\n")
    result = driver.run("config", "validate", env={"YGG_CONFIG": config})
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "plugin name memory is registered twice in yggdrasil.trackers "
        "(ygg_memtracker, ygg_othertracker)"
    )


@pytest.mark.ac("AC-03")
def test_agent_commit_runs_home_precommit_and_allows_no_hook(a10_factory):
    for location in ("configured", "default", "absent"):
        driver = a10_factory(location)
        if location != "absent":
            driver.home_precommit(location)
        driver.ready()
        driver.launch_and_wait("pm1tl1")
        result = driver.lane_commit()
        assert result.returncode == 0
        assert "home pre-commit ran" not in result.stderr


@pytest.mark.ac("AC-04")
def test_staged_scan_checks_plus_lines_and_binary_files_and_allows_hello(a10_factory):
    for name, text, message in (("notes.md", "hello", "add notes"),
                                ("a.md", "++zebra-orchid-7", "add a"),
                                ("b.dat", "zebra-orchid-7", "add b")):
        driver = a10_factory(name, tree=False)
        directory = driver.scanner()
        if name == "b.dat":
            driver.stage_file(directory, ".gitattributes", "*.dat binary\n")
            driver.git(directory, "commit", "-m", "attributes")
        driver.stage_file(directory, name, text + "\n")
        result = driver.git_result(directory, "commit", "-m", message)
        if name == "notes.md":
            assert result.returncode == 0
        else:
            assert result.returncode == 1
            assert "leak-scan: staged content matches a denylist entry" in result.stderr


@pytest.mark.ac("AC-05")
def test_commit_messages_and_identities_scanned_with_public_identity_exception(a10_factory):
    for case in ("message", "identity", "public"):
        driver = a10_factory(case, tree=False)
        directory = driver.scanner()
        driver.stage_file(directory, "c.md", "hello\n")
        if case != "message":
            driver.git(directory, "config", "user.name", "zebra-orchid-7")
            driver.git(directory, "config", "user.email", "z@example.invalid")
        if case == "public":
            driver.git(directory, "config", "ygg.publicIdentity", "zebra-orchid-7 <z@example.invalid>")
        message = "release zebra-orchid-7" if case == "message" else "add c"
        result = driver.git_result(directory, "commit", "-m", message)
        if case == "public":
            assert result.returncode == 0
        else:
            assert result.returncode == 1
            expected = ("leak-scan: commit message matches a denylist entry" if case == "message" else
                        "leak-scan: author or committer identity matches a denylist entry")
            assert expected in result.stderr


@pytest.mark.ac("AC-06")
def test_history_scan_reports_second_commit_and_allows_clean_history(a10_factory):
    clean = a10_factory("clean", tree=False)
    directory, _ = clean.history_seed(hit=False)
    assert clean.scan_history(directory).returncode == 0

    hit = a10_factory("hit", tree=False)
    directory, second_sha = hit.history_seed(hit=True)
    result = hit.scan_history(directory)
    assert result.returncode == 1
    assert second_sha in result.stdout
    assert "commit message" in result.stdout


@pytest.mark.ac("AC-06")
def test_staged_scan_refuses_unreadable_denylist(a10_factory):
    driver = a10_factory("missing", tree=False)
    directory = driver.scanner()
    missing = driver.root / "missing.txt"
    driver.git(directory, "config", "ygg.leakDenylist", missing)
    driver.stage_file(directory, "d.md", "hello\n")
    result = driver.git_result(directory, "commit", "-m", "add d")
    assert result.returncode == 1
    assert f"leak-scan: cannot read denylist {missing}" in result.stderr


@pytest.mark.ac("AC-07")
@pytest.mark.parametrize("family", ["app-service-aws-bot", "user-server-admin-session"])
def test_stage_plan_redacts_four_token_families(a10_ready, family):
    if family == "app-service-aws-bot":
        tokens = ("gh" + "o_" + "A" * 36, "sk-" + "svcacct-" + "B" * 24,
                  "AK" + "IA" + "C" * 16, "xo" + "xb-" + "1" * 12 + "-" + "D" * 24)
    else:
        tokens = ("gh" + "u_" + "E" * 36, "gh" + "s_" + "F" * 36,
                  "sk-" + "admin-" + "G" * 24, "AS" + "IA" + "H" * 16)
    relative = "projects/alpha-p1/documents"
    before = a10_ready.documents(relative)
    result = a10_ready.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1",
                           "--stage", "2", "--kind", "stage-plan", "--body",
                           "a " + tokens[0] + " b " + tokens[1] + " c " + tokens[2] + " d " + tokens[3])
    assert result.returncode == 0
    texts = a10_ready.changed_texts(before, relative)
    assert texts
    assert any(text.count("[REDACTED]") == 4 and all(token not in text for token in tokens)
               for text in texts)


@pytest.mark.ac("AC-07")
def test_decision_issue_redacts_slack_user_token(a10_ready):
    token = "xo" + "xp-" + "2" * 12 + "-" + "J" * 24
    relative = "projects/alpha-p1/issues"
    before = a10_ready.documents(relative)
    result = a10_ready.run("decision", "raise", "--from", "pm1", "--question", "Rotate " + token + "?",
                           "--option", "A", "--option", "B", "--recommended", "A",
                           "--reversible", "no", "--aligns", "yes")
    assert result.returncode == 0
    texts = a10_ready.changed_texts(before, relative)
    assert texts
    assert any(token not in text and "[REDACTED]" in text for text in texts)


@pytest.mark.ac("AC-08")
def test_alerts_and_messages_remove_tokens(a10_factory):
    alert = a10_factory("alert")
    alert.ready()
    token = "gh" + "u_" + "E" * 36
    alert.run("blocker", "raise", "--from", "pm1tl1", "--reason", "token " + token + " leaked")
    entries = alert.state_lines("alerts.jsonl")
    assert entries
    assert token not in entries[-1]["message"]
    assert "[REDACTED]" in entries[-1]["message"]

    message = a10_factory("message")
    message.ready()
    token = "xo" + "xp-" + "2" * 12 + "-" + "J" * 24
    message.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", token)
    assert token not in message.state_text("messages.jsonl")
    assert token not in message.run("inbox", "pm1tl1").stdout


@pytest.mark.ac("AC-08")
def test_receipt_stderr_tail_redacts_admin_token(a10_ready):
    token = "sk-" + "admin-" + "G" * 24
    a10_ready.fake_behavior(claude={"agents": {"pm1tl1": {"stderr": "leaked " + token, "exit_code": 1}}})
    a10_ready.launch_and_wait("pm1tl1")
    receipt = a10_ready.newest_receipt("pm1tl1")
    assert isinstance(receipt, dict)
    assert token not in receipt["stderr_tail"]
    assert "[REDACTED]" in receipt["stderr_tail"]


@pytest.mark.ac("AC-09")
def test_init_ignores_runtime_folders_preserves_rules_and_can_track_records(a10_factory):
    driver = a10_factory("init", tree=False)
    directory = driver.git_folder("g")
    assert driver.init(directory).returncode == 0
    assert driver.git_result(directory, "check-ignore", "ygg-state/x", "ygg-tracker/x").returncode == 0

    directory = driver.git_folder("h")
    driver.write(directory / ".gitignore", "node_modules/\n")
    driver.init(directory)
    driver.init(directory, "--force")
    lines = driver.read(directory / ".gitignore").splitlines()
    for line in ("node_modules/", "ygg-state/", "ygg-tracker/"):
        assert lines.count(line) == 1

    directory = driver.git_folder("k")
    driver.init(directory, "--track-records")
    text = driver.read(directory / ".gitignore")
    assert "ygg-state/" in text
    assert "ygg-tracker/" not in text


@pytest.mark.ac("AC-10")
def test_launch_sets_private_state_and_receipt_modes(a10_ready):
    a10_ready.chmod(a10_ready.state, 0o755)
    a10_ready.launch_and_wait("pm1tl1")
    assert a10_ready.mode(a10_ready.state) == 0o700
    assert a10_ready.newest_receipt_mode("pm1tl1") == 0o600


@pytest.mark.ac("AC-10")
def test_message_and_alert_logs_have_private_modes(a10_ready):
    a10_ready.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "Do WP-1")
    a10_ready.run("blocker", "raise", "--from", "pm1tl1", "--reason", "waiting")
    assert a10_ready.mode(a10_ready.state / "messages.jsonl") == 0o600
    assert a10_ready.mode(a10_ready.state / "alerts.jsonl") == 0o600


@pytest.mark.ac("AC-11")
@pytest.mark.parametrize("case", ["role", "duplicate", "programme-id"])
def test_corrupt_node_records_refused(a10_tree, case):
    if case == "role":
        entry = {"id": "pm1tl9", "role": "../../outside/note", "parent": "pm1"}
        expected = ("corrupt tracker record", "role ../../outside/note")
        args = ("launch", "pm1tl9")
    elif case == "duplicate":
        entry = {"id": "pm2tl1", "role": "tl", "parent": "pm1"}
        expected = ("node id pm2tl1 is registered twice",)
        args = ("status", "--json")
    else:
        entry = {"id": "alpha", "role": "tl", "parent": "pm1"}
        expected = ("node id alpha collides with a programme id",)
        args = ("status", "--json")
    a10_tree.append_node(entry)
    before = a10_tree.launches()
    result = a10_tree.run(*args)
    assert result.returncode == 8
    for text in expected:
        assert text in result.stderr
    if case == "role":
        assert a10_tree.launches() == before


@pytest.mark.ac("AC-12")
def test_corrupt_project_pm_refused(a10_tree):
    a10_tree.change_json(a10_tree.tracker / "projects/alpha-p1/project.json", pm="PM 1; rm")
    result = a10_tree.run("status", "--json")
    assert result.returncode == 8
    assert "corrupt tracker record" in result.stderr
    assert "pm" in result.stderr


@pytest.mark.ac("AC-12")
def test_root_programme_home_refused(a10_ready):
    a10_ready.change_json(a10_ready.tracker / "initiatives/alpha/initiative.json", home="/")
    before = a10_ready.launches()
    result = a10_ready.run("launch", "pm1tl1")
    assert result.returncode == 3
    assert result.stderr.strip() == (
        "REFUSED: launch precondition: programme alpha home / is not an allowed home folder"
    )
    assert a10_ready.launches() == before


@pytest.mark.ac("AC-12")
def test_demo_home_resolves_from_tracker_parent(a10_factory):
    driver = a10_factory("demo", tree=False)
    elsewhere, expected_home = driver.demo_seed()
    driver.run("rehydrate", cwd=elsewhere)
    result, status = driver.run_json("status", "--json", cwd=elsewhere)
    assert result.returncode == 0
    assert isinstance(status, dict)
    programme = next(item for item in status["programmes"] if item["id"] == "demo")
    assert programme["home"] == expected_home


@pytest.mark.ac("AC-13")
@pytest.mark.parametrize("case", ["post-checkout", "fsmonitor", "merge-hooks"])
def test_internal_git_does_not_run_repository_hooks_or_fsmonitor(a10_tree, case):
    if case == "post-checkout":
        _, marker = a10_tree.marker_script("post-checkout-ran", path=a10_tree.repo_a / ".git/hooks/post-checkout")
        a10_tree.ready()
        result = a10_tree.run("launch", "pm1tl1")
    elif case == "fsmonitor":
        a10_tree.accepted_integration()
        script, marker = a10_tree.marker_script("fsmonitor-ran")
        a10_tree.git(a10_tree.repo_a, "config", "core.fsmonitor", script)
        result = a10_tree.merge_lane()
    else:
        a10_tree.accepted_integration()
        for hook in ("post-merge", "pre-merge-commit"):
            _, marker = a10_tree.marker_script("merge-hook-ran", path=a10_tree.repo_a / ".git/hooks" / hook)
        result = a10_tree.merge_lane()
    assert result.returncode == 0
    assert not a10_tree.exists(marker)

    if case == "post-checkout":
        a10_tree.git_result(a10_tree.repo_a, "checkout", "-q", "-b", "control-a")
    elif case == "fsmonitor":
        a10_tree.git_result(a10_tree.repo_a, "status")
    else:
        a10_tree.git_result(a10_tree.repo_a, "checkout", "-q", "-b", "control-m", "main")
        a10_tree.git_result(a10_tree.repo_a, "merge", "--no-ff", "-m", "control",
                           "ygg/alpha-p1/lane/pm1tl1")
    assert a10_tree.exists(marker)


@pytest.mark.ac("AC-14")
def test_repository_merge_driver_refused(a10_tree):
    a10_tree.accepted_integration()
    script, marker = a10_tree.marker_script("driver-ran")
    a10_tree.git(a10_tree.repo_a, "config", "merge.evil.driver", script)
    result = a10_tree.merge_lane()
    assert result.returncode == 3
    assert result.stderr.strip() == (
        f"REFUSED: gate: {a10_tree.repo_a} defines merge.evil.driver; "
        "Yggdrasil does not run repository-defined git helpers"
    )
    assert not a10_tree.exists(marker)


@pytest.mark.ac("AC-14")
def test_repository_filter_refused_and_standard_lfs_allowed(a10_factory):
    lfs = a10_factory("lfs")
    for key, value in (("filter.lfs.smudge", "git-lfs smudge -- %f"),
                       ("filter.lfs.process", "git-lfs filter-process"), ("filter.lfs.required", "true")):
        lfs.git(lfs.repo_a, "config", key, value)
    lfs.ready()
    assert lfs.run("launch", "pm1tl1").returncode == 0

    evil = a10_factory("evil")
    script, marker = evil.marker_script("filter-ran")
    evil.git(evil.repo_a, "config", "filter.evil.smudge", script)
    evil.ready()
    before = evil.launches()
    result = evil.run("launch", "pm1tl1")
    assert result.returncode == 3
    assert ("defines filter.evil.smudge; Yggdrasil does not run repository-defined git helpers"
            in result.stderr)
    assert not evil.exists(marker)
    assert evil.launches() == before


@pytest.mark.ac("AC-15")
@pytest.mark.parametrize("node", ["pm1su1", "pm1tl1"])
def test_claude_prompt_and_brief_delivered_privately(a10_tree, node):
    a10_tree.brief_seed()
    result = a10_tree.launch_and_wait(node)
    assert result.returncode == 0
    launches = [call for call in a10_tree.launches(node) if call["binary"] == "claude"]
    assert launches
    launch = launches[-1]
    if node == "pm1su1":
        assert all("zebra-5813" not in argument and "You are Yggdrasil node" not in argument
                   for argument in launch["argv"])
        assert a10_tree.delivered_privately(launch, "zebra-5813")
    else:
        assert all("You are Yggdrasil node pm1tl1" not in argument for argument in launch["argv"])
        assert a10_tree.delivered_privately(launch, "You are Yggdrasil node pm1tl1")


@pytest.mark.ac("AC-16")
@pytest.mark.parametrize("node", ["pm1su1", "pm1tl1"])
def test_codex_prompt_and_brief_delivered_privately(a10_tree, node):
    a10_tree.brief_seed(codex=True)
    result = a10_tree.launch_and_wait(node)
    assert result.returncode == 3
    assert "Codex builds are not in this release" in result.stderr
    assert [call for call in a10_tree.launches(node) if call["binary"] == "codex"] == []