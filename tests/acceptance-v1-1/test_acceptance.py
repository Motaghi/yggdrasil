import re
import signal

import pytest

mandatory = pytest.mark.release('v1.1-mandatory')
optional = pytest.mark.release('v1.1-optional')

NARRATION = (
    'demo: 1/7 brief written',
    'demo: 2/7 plan approved',
    'demo: 3/7 WP-1 ready for review',
    'demo: 4/7 WP-2 ready for review',
    'demo: 5/7 reviews approved',
    'demo: 6/7 merged into main',
    'demo: 7/7 project closed',
)
ALL_OFF = 'dashboard,dashboard_serve,summary,doctor,confirm_window,retry_pm_reset,t3_live_send,retry_cap,stage_close'


def narration(output):
    lines = output.splitlines()
    for line in NARRATION:
        assert lines.count(line) == 1
    positions = [lines.index(line) for line in NARRATION]
    assert positions == sorted(positions)
    return positions


def project(status):
    assert isinstance(status, dict)
    return next(row for row in status['projects'] if row['id'] == 'alpha-p1')


def no_processes(d, folder):
    pairs = d.pairs(folder)
    assert pairs, 'The kept demo must provide recorded process evidence'
    assert all(pair and pair[0] and pair[1] for pair in pairs)
    d.settle()
    assert d.live_pairs(pairs) == []


@mandatory
@pytest.mark.ac('AC-01')
def test_ac01_version_ignores_config_and_managed_environment(d):
    d.write('broken.yaml', 'models: [')
    result = d.run('--version', env={'YGG_STATE_ROOT': d.path('state'),
                   'YGG_CONFIG': d.path('broken.yaml'), 'YGG_NODE': 'pm1tl1'})
    assert result.stdout == 'ygg 1.1.1\n'
    assert result.returncode == 0
    assert result.stderr == ''
    assert not d.exists(d.path('state'))


@mandatory
@pytest.mark.ac('AC-02')
def test_ac02_fast_demo_closes_and_merges_both_packages(d):
    result, folder, _ = d.demo()
    positions = narration(result.stdout)
    assert result.returncode == 0
    assert d.elapsed <= 120
    assert result.stdout.splitlines()[positions[-1] + 1].startswith('demo: finished in ')
    status = d.status(folder)
    assert project(status)['state'] == 'closed'
    packages = {row['id']: row for row in status['work_packages']}
    assert packages['WP-1']['state'] == 'merged'
    assert packages['WP-2']['state'] == 'merged'


@mandatory
@pytest.mark.ac('AC-02')
def test_ac02_default_pace_is_observed_at_the_seventh_line(d):
    result, _, _ = d.demo(keep=False, fast=False)
    narration(result.stdout)
    assert result.returncode == 0
    match = re.search(r'^demo: finished in (\d+(?:\.\d+)?) s$', result.stdout, re.M)
    assert match
    assert 105 <= float(match[1]) <= 300
    seventh = next(elapsed for line, elapsed in d.line_times if line == NARRATION[-1])
    assert seventh >= 105


@mandatory
@pytest.mark.ac('AC-02')
def test_ac02_v100_shipped_switch_set(d):
    result, folder, _ = d.demo(env={'YGG_TEST_MODE': '1',
        'YGG_TEST_RELEASE_OFF': 'confirm_window,t3_live_send,dashboard_serve'})
    narration(result.stdout)
    assert result.returncode == 0
    assert d.elapsed <= 120
    assert any(line.startswith('demo: dashboard file ') for line in result.stdout.splitlines())
    assert not any(row['kind'] in {'ygg-launch-refused', 'ygg-uncertain', 'ygg-stall'}
                   for row in d.lines(folder / 'state/alerts.jsonl'))


@mandatory
@pytest.mark.ac('AC-03')
def test_ac03_conductor_identity_and_review_testing_activations(d):
    result, folder, _ = d.demo()
    assert folder is not None
    records = d.activations(folder)
    conductor = d.pid_pair(folder / 'conductor.pid')
    assert conductor is not None
    assert d.read(folder / 'state/conductor.lock').splitlines()[0] == str(conductor[0])
    assert {'pm1', 'pm1tl1', 'pm1su1'} <= {row['node'] for row in records}
    assert all(row['launcher_pid'] == conductor[0] and row['launcher_started_at'] == conductor[1]
               for row in records)
    reviews = [row for row in records if row['node'] == 'pm1rv1']
    assert len(reviews) == 2
    assert sorted(row['review'] for row in reviews) == ['rv-0001', 'rv-0002']
    testing = {row.get('testing_package') for row in records if row['node'] == 'pm1su1'}
    assert {'WP-1', 'WP-2'} <= testing
    assert not any(row['kind'] in {'ygg-stall', 'ygg-uncertain', 'ygg-launch-refused', 'ygg-review-held'}
                   for row in d.lines(folder / 'state/alerts.jsonl'))
    status = d.status(folder)
    assert status['uncertain_keys'] == []
    assert status['held_launches'] == []


@mandatory
@pytest.mark.ac('AC-04')
def test_ac04_review_acceptance_records_and_exact_main_contents(d):
    result, folder, _ = d.demo()
    assert folder is not None
    store = d.json(folder / 'tracker/_ygg/reviews.json')
    assert store is not None
    reviews = store['reviews']
    assert set(reviews) == {'rv-0001', 'rv-0002'}
    for review_id, package in [('rv-0001', 'WP-1'), ('rv-0002', 'WP-2')]:
        review = reviews[review_id]
        assert review['package'] == package
        assert review['state'] == 'approved'
        assert review['reviewer'] == 'pm1rv1'
        assert any(record.get('kind') == 'acceptance' and record.get('node') == 'pm1su1'
                   and record.get('verdict') == 'accept' and record.get('sha') == review['head']
                   for record in d.records(folder, package))
    subjects = d.git(folder, 'log', '--format=%s', 'main').stdout.splitlines()
    assert subjects.count('WP-1: add greeting.txt') == 1
    assert subjects.count('WP-2: add farewell.txt') == 1
    assert d.git(folder, 'show', 'main:greeting.txt').stdout == 'Hello from WP-1\n'
    assert d.git(folder, 'show', 'main:farewell.txt').stdout == 'Goodbye from WP-2\n'
    assert d.git(folder, 'status', '--porcelain').stdout == ''
    for branch in d.git(folder, 'branch', '--list', 'ygg/*').stdout.splitlines():
        assert d.git(folder, 'merge-base', '--is-ancestor', branch.lstrip('* ').strip(), 'main').returncode == 0


@mandatory
@pytest.mark.ac('AC-05')
def test_ac05_user_trees_and_unrelated_process_are_untouched(d):
    sleeper = d.contaminated()
    names = ['h', 'cwd', 'user-state', 'user-tracker', 'claude-cfg', 'codex-home']
    before = {name: d.snapshot(name) for name in names}
    result, _, _ = d.demo(keep=False)
    assert result.returncode == 0
    assert d.alive(sleeper)
    assert d.read(d.path('rec/calls')) == ''
    assert not d.exists(d.path('rec/yaml-imported'))
    assert {name: d.snapshot(name) for name in names} == before
    assert not d.exists(d.path('user-state.safety'))


@mandatory
@pytest.mark.ac('AC-05')
def test_ac05_kept_demo_contains_no_user_credentials_or_profile_path(d):
    d.contaminated()
    result, folder, _ = d.demo()
    assert folder is not None
    assert result.returncode == 0
    text = d.all_text(folder)
    assert 'sk-' 'ant-demo-zzz123' not in text
    assert 'sk-demo-zzz456' not in text
    assert str(d.path('profile.yaml')) not in text


@mandatory
@pytest.mark.ac('AC-05')
def test_ac05_invalid_exported_user_config_is_ignored(d):
    d.write('user.yaml', 'models: [')
    result, _, _ = d.demo(keep=False, env={'YGG_CONFIG': d.path('user.yaml')})
    narration(result.stdout)
    assert result.returncode == 0


@mandatory
@pytest.mark.ac('AC-06')
def test_ac06_no_outside_binary_or_blocking_shim_runs(d):
    d.recording_path()
    result, folder, _ = d.demo()
    assert folder is not None
    assert result.returncode == 0
    assert d.read(d.path('rec/calls')) == ''
    assert not d.exists(folder / 'blocked-calls.jsonl')
    config = d.yaml(folder / 'config.yaml')
    assert config['alert_forwarder'] == 'jsonl'
    assert config['alert_macos'] == 'off'
    assert config['branch_protection'] == 'off'


@mandatory
@pytest.mark.ac('AC-06')
def test_ac06_linear_positive_control_then_no_demo_requests(d):
    before, result, after = d.linear_demo()
    assert len(before) >= 1
    assert result.returncode == 0
    assert after == before


@mandatory
@pytest.mark.ac('AC-06')
def test_ac06_exact_child_environment_and_launch_profile(d):
    result, folder, _ = d.demo(env={'LINEAR_API_KEY': 'lin_' 'api_demo123456',
        'YGG_PROFILE': d.path('profile.yaml'), 'GIT_CONFIG_GLOBAL': d.path('user.gitconfig')})
    assert folder is not None
    env = d.json(folder / 'demo-env.json')
    assert set(env) == {'PATH', 'HOME', 'TMPDIR', 'LANG', 'YGG_CONFIG', 'YGG_PROFILE',
        'YGG_STATE_ROOT', 'YGG_FILES_ROOT', 'YGG_TRACKER', 'YGG_CLAUDE_BIN', 'YGG_CODEX_BIN',
        'YGG_DEMO_ROOT', 'YGG_DEMO_PACE', 'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL'}
    for key, suffix in [('HOME', 'userhome'), ('YGG_CONFIG', 'config.yaml'),
                        ('YGG_PROFILE', 'profile.yaml'), ('GIT_CONFIG_GLOBAL', 'userhome/.gitconfig')]:
        assert env[key] == str(folder / suffix)
    assert env['YGG_TRACKER'] == 'files'
    assert env['PATH'].split(':')[0] == str(folder / 'bin')
    assert d.yaml(folder / 'profile.yaml') == {'launch_env': {
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': str(folder / 'userhome/.gitconfig')}}


@mandatory
@pytest.mark.ac('AC-07')
def test_ac07_dashboard_file_changes_at_both_hold_points(d):
    result, folder, pages = d.held()
    assert len(pages) == 2
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    line = next(line for line in lines if line.startswith('demo: dashboard file '))
    assert line == f'demo: dashboard file {folder}/state/dashboard/alpha.html (refreshes every 2 s)'
    assert lines.index(NARRATION[0]) < lines.index(line) < lines.index(NARRATION[1])
    assert 'http://' not in result.stdout
    for page in pages:
        assert '<h1>alpha</h1>' in page
        assert '<meta http-equiv="refresh" content="2">' in page
    assert 'data-package="WP-1"' not in pages[0]
    columns = d.package_columns(pages[1])
    assert columns['WP-1'] == 'done'
    assert columns['WP-2'] == 'done'


@mandatory
@pytest.mark.ac('AC-07')
def test_ac07_dashboard_switch_off_uses_no_page(d):
    result, folder, _ = d.demo(env={'YGG_TEST_MODE': '1', 'YGG_TEST_RELEASE_OFF': 'dashboard'})
    narration(result.stdout)
    assert result.returncode == 0
    assert 'demo: dashboard not in this release' in result.stdout.splitlines()
    assert not any(line.startswith('demo: dashboard file ') or 'http://' in line
                   for line in result.stdout.splitlines())
    assert not d.exists(folder / 'state/dashboard/alpha.html')


@mandatory
@pytest.mark.ac('AC-09')
def test_ac09_refresh_absent_by_default_and_present_when_configured(d):
    d.standard()
    result = d.standard_run('dashboard', 'alpha', '--no-open')
    assert result.returncode == 0
    assert 'http-equiv="refresh"' not in d.standard_dashboard()
    d.standard_config(dashboard_refresh_seconds=2)
    result = d.standard_run('dashboard', 'alpha', '--no-open')
    assert result.returncode == 0
    assert '<meta http-equiv="refresh" content="2">' in d.standard_dashboard()


@mandatory
@pytest.mark.ac('AC-09')
def test_ac09_zero_refresh_is_a_validation_error(d):
    d.standard()
    d.standard_config(dashboard_refresh_seconds=0)
    result = d.standard_run('status')
    assert result.returncode == 1
    assert "dashboard_refresh_seconds must be a positive whole number, not '0'" in result.stderr


@mandatory
@pytest.mark.ac('AC-10')
def test_ac10_default_run_removes_its_folder(d):
    result, _, _ = d.demo(keep=False)
    assert result.stdout.splitlines(), 'Demo must print its removal receipt'
    assert result.returncode == 0
    last = result.stdout.splitlines()[-1]
    assert last.startswith('demo: removed ')
    assert d.inside(last.removeprefix('demo: removed '), d.path('t'))
    assert d.entries(d.path('t')) == []


@mandatory
@pytest.mark.ac('AC-10')
def test_ac10_keep_records_identities_and_ends_every_process(d):
    result, folder, _ = d.demo()
    assert folder is not None
    assert result.returncode == 0
    assert d.folders() == [folder]
    assert result.stdout.splitlines()[-1] == f'demo: kept {folder}'
    assert d.json(folder / 'demo-marker.json')['ended'] is True
    no_processes(d, folder)


@mandatory
@pytest.mark.ac('AC-10')
def test_ac10_reused_pid_with_different_identity_is_never_signalled(d):
    result, folder, sleeper = d.interrupt(signal.SIGINT, replace=True)
    assert result.returncode == 130
    assert d.signal_elapsed <= 60
    assert d.alive(sleeper)
    no_processes(d, folder)


@mandatory
@pytest.mark.ac('AC-11')
@pytest.mark.parametrize('sig,code', [(signal.SIGINT, 130), (signal.SIGTERM, 143)], ids=['SIGINT', 'SIGTERM'])
def test_ac11_interrupt_stops_held_agents(d, sig, code):
    result, folder, _ = d.interrupt(sig)
    assert result.returncode == code
    assert d.signal_elapsed <= 60
    if sig == signal.SIGINT:
        assert any(row['node'] == 'pm1tl1' for row in d.activations(folder))
    no_processes(d, folder)


@mandatory
@pytest.mark.ac('AC-11')
def test_ac11_sigint_without_keep_removes_folder(d):
    result, _, _ = d.interrupt(signal.SIGINT, keep=False)
    assert result.returncode == 130
    assert d.signal_elapsed <= 60
    assert d.entries(d.path('t')) == []


@mandatory
@pytest.mark.ac('AC-12')
def test_ac12_step_fault_reports_step_keeps_root_and_stops_processes(d):
    result, folder, _ = d.demo(keep=False, env={'YGG_TEST_MODE': '1', 'YGG_TEST_DEMO_FAIL_AT': '3'})
    assert result.returncode == 1
    assert NARRATION[1] in result.stdout
    assert 'demo: 3/7' not in result.stdout
    assert 'demo: failed at step 3: test fault' in result.stderr
    assert folder is not None and d.exists(folder)
    assert result.stdout.splitlines()[-1] == f'demo: kept {folder}'
    no_processes(d, folder)


@mandatory
@pytest.mark.ac('AC-12')
def test_ac12_shadow_binary_is_refused_before_conductor(d):
    shadow = d.shadow_claude()
    result, folder, _ = d.demo(keep=False, env={'YGG_TEST_MODE': '1', 'YGG_TEST_DEMO_PATH_FIRST': shadow})
    assert result.returncode == 1
    assert f"demo: failed at setup: claude resolves to {shadow}/claude, outside the demo's bin folder" in result.stderr
    assert d.read(d.path('rec/calls')) == ''
    assert 'demo: 1/7' not in result.stdout
    assert folder is not None
    assert result.stdout.splitlines()[-1] == f'demo: kept {folder}'
    assert not d.exists(folder / 'state/conductor.lock')


@mandatory
@pytest.mark.ac('AC-12')
def test_ac12_missing_git_creates_no_demo_root(d):
    d.no_git()
    result, _, _ = d.demo(keep=False)
    assert result.returncode == 1
    assert 'demo: failed at setup: git not found on PATH' in result.stderr
    assert not any(line.startswith('demo: kept') for line in result.stdout.splitlines())
    assert d.entries(d.path('t')) == []


@mandatory
@pytest.mark.ac('AC-13')
@pytest.mark.parametrize('env', [{'YGG_NODE': 'pm1tl1'}, {'YGG_ACTIVATION': 'act-0001'}], ids=['node', 'activation'])
def test_ac13_managed_environment_is_refused(d, env):
    result = d.run('demo', '--fast', '--no-open', env=env)
    assert result.returncode == 3
    assert "user-only: 'demo' is a user verb and cannot run in a managed session" in result.stderr
    assert d.entries(d.path('t')) == []


@mandatory
@pytest.mark.ac('AC-13')
def test_ac13_managed_worktree_is_refused(d):
    result = d.run('demo', '--fast', '--no-open', cwd=d.managed_cwd())
    assert result.returncode == 3
    assert "user-only: 'demo' is a user verb and cannot run in a managed session" in result.stderr
    assert d.entries(d.path('t')) == []


@mandatory
@pytest.mark.ac('AC-14')
def test_ac14_wheel_includes_demo_without_tests(d):
    names = d.wheel_names()
    assert {'yggdrasil/demo/agent.py', 'yggdrasil/demo/script.py', 'yggdrasil/demo/run.py'} <= set(names)
    assert not any(name.startswith('tests/') for name in names)


@mandatory
@pytest.mark.ac('AC-14')
def test_ac14_plain_tool_install_runs_demo_in_empty_folder(d):
    result = d.installed_demo()
    narration(result.stdout)
    assert result.returncode == 0
    assert d.entries(d.path('empty')) == []


@mandatory
@pytest.mark.ac('AC-14')
def test_ac14_package_distribution_and_command_versions_agree(d):
    metadata, command = d.installed_versions()
    assert metadata.stdout == '1.1.1 1.1.1\n'
    assert command.stdout == 'ygg 1.1.1\n'


@mandatory
@pytest.mark.ac('AC-16')
@pytest.mark.parametrize('state', ['executing', 'exception', 'closed'])
def test_ac16_pid_ready_refuses_active_or_finished_project(d, state):
    d.plan_state(state)
    before = d.standard_alerts()
    result = d.standard_run('tracker', 'write', '--as', 'pm1', '--project', 'alpha-p1', '--kind', 'pid-ready', '--body', 'ready')
    assert result.returncode == 3
    if state in {'executing', 'closed'}:
        assert f'alpha-p1 is {state}' in result.stderr
    if state in {'executing', 'exception'}:
        assert 'pid-ready is only for a new, rejected or replanned PID' in result.stderr
    assert project(d.standard_status())['state'] == state
    if state != 'closed':
        assert sum(row['kind'] == 'ygg-pid-ready' for row in d.standard_alerts()) == sum(row['kind'] == 'ygg-pid-ready' for row in before)


@mandatory
@pytest.mark.ac('AC-17')
@pytest.mark.parametrize('state', ['replan', 'pid-rejected', 'initiating'])
def test_ac17_pid_ready_accepts_new_rejected_or_replanned_pid(d, state):
    d.plan_state(state)
    before = d.standard_alerts()
    result = d.standard_run('tracker', 'write', '--as', 'pm1', '--project', 'alpha-p1', '--kind', 'pid-ready', '--body', 'ready')
    assert project(d.standard_status())['state'] == 'pid-ready'
    assert result.returncode == 0
    if state == 'replan':
        assert sum(row['kind'] == 'ygg-pid-ready' for row in d.standard_alerts()) == sum(row['kind'] == 'ygg-pid-ready' for row in before) + 1


@mandatory
@pytest.mark.ac('AC-18')
def test_ac18_explicit_sender_requires_kind(d):
    d.standard()
    before = d.standard_messages()
    result = d.standard_run('send', '--from', 'pm1tl1', '--to', 'pm1', '--body', 'hi', env={'YGG_NODE': 'pm1tl1'})
    assert result.returncode == 1
    assert '--kind is required with --from' in result.stderr
    assert 'send without --from' not in result.stderr
    assert [row for row in d.standard_messages() if row.get('to') == 'pm1'] == [row for row in before if row.get('to') == 'pm1']


@mandatory
@pytest.mark.ac('AC-18')
def test_ac18_user_short_send_defaults_to_work(d):
    d.standard()
    before = d.standard_messages()
    result = d.standard_run('send', '--to', 'pm1', '--body', 'hi')
    messages = d.standard_messages()[len(before):]
    matching = [row for row in messages if row.get('to') == 'pm1' and row.get('kind') == 'work' and row.get('from') == 'user']
    assert len(matching) == 1
    assert result.returncode == 0


@optional
@pytest.mark.ac('AC-19')
@pytest.mark.parametrize('change', [None, 'criteria', 'decision'], ids=['fresh', 'criteria-change', 'decision-change'])
def test_ac19_blockers_show_route_map_time_and_staleness(d, change):
    d.route_map(change)
    result = d.standard_run('status')
    suffix = ', older than the latest change to its packages or decisions' if change else ''
    assert f'  blockers (route map of 2026-11-22 09:00 UTC{suffix}): waiting on WP-2' in result.stdout.splitlines()


@mandatory
@pytest.mark.ac('AC-21')
def test_ac21_no_terminal_notice_and_explicit_noninteractive_control(d):
    result = d.run('init', '--out', d.path('cfg.yaml'), stdin='')
    assert result.returncode == 0
    assert d.exists(d.path('cfg.yaml'))
    assert 'no terminal: the five questions were skipped and their defaults used' in result.stderr
    explicit = d.run('init', '--non-interactive', '--out', d.path('cfg2.yaml'), stdin='')
    assert explicit.returncode == 0
    assert 'no terminal' not in explicit.stderr


@mandatory
@pytest.mark.ac('AC-22')
@pytest.mark.parametrize('service', [False, True], ids=['secrets', 'keychain-service'])
def test_ac22_secret_key_names_redact_values_and_preserve_plain_values(d, service):
    d.profile('launch_env:\n  STRIPE_KEY: abcdef123456\n  AUTH_HEADER: "Bearer abcdef123457"\n  SESSION_COOKIE: sid-abcdef123458\n  COOKIE: x1y2z3w4v5u6\n  LOG_LEVEL: debug\n' + ('linear_keychain_service: com.example.ygg\n' if service else ''))
    result = d.run('profile', 'show')
    for key in ('STRIPE_KEY', 'AUTH_HEADER', 'SESSION_COOKIE', 'COOKIE'):
        assert re.search(r'\b' + key + r':\s*[\'\"]?\[redacted\]', result.stdout)
    assert 'LOG_LEVEL: debug' in result.stdout
    if service:
        assert 'com.example.ygg' in result.stdout
    for secret in ('abcdef123456', 'abcdef123457', 'abcdef123458', 'x1y2z3w4v5u6'):
        assert secret not in result.stdout


@mandatory
@pytest.mark.ac('AC-22')
def test_ac22_whole_secret_containers_are_redacted(d):
    d.profile('tokens: {linear: tk-abcdef123459}\nclient_secrets: [cs-abcdef123460]\npools: {live: pool-a}\n')
    result = d.run('profile', 'show')
    for key in ('tokens', 'client_secrets'):
        assert re.search(r'\b' + key + r':\s*[\'\"]?\[redacted\]', result.stdout)
    assert 'pool-a' in result.stdout
    assert 'abcdef123459' not in result.stdout
    assert 'abcdef123460' not in result.stdout


@mandatory
@pytest.mark.ac('AC-23')
@pytest.mark.parametrize('content,diagnostic', [('models: [', 'not valid YAML'), ('- a', 'not a mapping')], ids=['malformed', 'not-mapping'])
def test_ac23_invalid_config_reports_path_and_error(d, content, diagnostic):
    d.write('c.yaml', content)
    result = d.run('status', env={'YGG_CONFIG': d.path('c.yaml')})
    assert result.returncode == 1
    assert f'{d.path("c.yaml")}: {diagnostic}' in result.stderr


@mandatory
@pytest.mark.ac('AC-23')
def test_ac23_empty_config_is_valid(d):
    d.write('c.yaml', '')
    assert d.run('status', env={'YGG_CONFIG': d.path('c.yaml')}).returncode == 0


@mandatory
@pytest.mark.ac('AC-24')
def test_ac24_team_lead_prompt_explains_pair_reviewer(d):
    result, text = d.pair_prompt()
    assert 'When you start your pair reviewer (the agent you start on the configured pair model to check your work), pass it the project brief verbatim:' in text
    assert 'Sol pair' not in text


@mandatory
@pytest.mark.ac('AC-26')
@pytest.mark.parametrize('off', ['stage_close', ALL_OFF], ids=['stage-close-off', 'all-off'])
def test_ac26_demo_finishes_with_release_switches_off(d, off):
    result, folder, _ = d.demo(env={'YGG_TEST_MODE': '1', 'YGG_TEST_RELEASE_OFF': off})
    narration(result.stdout)
    assert result.returncode == 0
    assert d.elapsed <= 120
    if off == 'stage_close':
        assert not d.exists(folder / 'state/stage-close.json')
    else:
        assert 'demo: dashboard not in this release' in result.stdout.splitlines()
        assert project(d.status(folder))['state'] == 'closed'


@mandatory
@pytest.mark.ac('AC-27')
@pytest.mark.parametrize('sig,twice,code', [(signal.SIGHUP, False, 129), (signal.SIGINT, True, 130)], ids=['SIGHUP', 'double-SIGINT'])
def test_ac27_hangup_and_double_interrupt_stop_all_processes(d, sig, twice, code):
    result, folder, _ = d.interrupt(sig, twice=twice)
    assert result.returncode == code
    assert d.signal_elapsed <= 60
    if sig == signal.SIGHUP:
        assert any(row['node'] == 'pm1tl1' for row in d.activations(folder))
    no_processes(d, folder)


@mandatory
@pytest.mark.ac('AC-27')
def test_ac27_killed_driver_is_reported_then_explicitly_cleaned(d):
    second, cleanup, folder, pairs = d.recover_killed()
    assert folder is not None
    assert second.returncode == 0
    assert f'demo: an earlier demo was left in {folder}; to stop it and remove it, run: ygg demo --cleanup {folder}' in second.stdout.splitlines()
    assert cleanup.returncode == 0
    assert cleanup.stdout.splitlines()[-1] == f'demo: removed {folder}'
    assert not d.exists(folder)
    assert pairs
    assert d.live_pairs(pairs) == []


@mandatory
@pytest.mark.ac('AC-28')
def test_ac28_parent_and_agent_git_ignore_planted_configs(d):
    d.git_isolation()
    result, folder, _ = d.demo()
    narration(result.stdout)
    assert result.returncode == 0
    assert d.read(d.path('rec/calls')) == ''
    calls = d.lines(d.path('rec/git.jsonl'))
    assert any(row['node'] == 'pm1tl1' for row in calls)
    assert any(row['node'] is None for row in calls)
    assert all(row['nosystem'] == '1' for row in calls)
    authors = d.git(folder, 'log', '--format=%an %G?', 'main', real=True).stdout.splitlines()
    assert authors
    assert all(line == 'Yggdrasil Demo N' for line in authors)


@mandatory
@pytest.mark.ac('AC-29')
def test_ac29_os_network_denial_with_positive_control(d):
    reason = d.linux_tools()
    if reason:
        pytest.skip(reason)
    result, control, trace = d.offline_demo()
    narration(result.stdout)
    assert result.returncode == 0
    assert control.returncode != 0
    assert 'Network is unreachable' in control.stderr
    for line in trace.splitlines():
        if 'sa_family=AF_INET6' in line:
            assert '"::1"' in line
        elif 'sa_family=AF_INET' in line:
            assert 'inet_addr("127.0.0.1")' in line


@mandatory
@pytest.mark.ac('AC-30')
@pytest.mark.parametrize('kind', ['symlink', 'mode', 'state-symlink'])
def test_ac30_unsafe_cleanup_signals_nothing_and_preserves_user_data(d, kind):
    result, folder, sleeper, before, after = d.unsafe_cleanup(kind)
    assert result.returncode == 1
    assert result.stderr.startswith(f'demo: {folder} is not a safe demo folder:')
    assert d.alive(sleeper)
    if kind == 'mode':
        assert d.exists(folder)
    else:
        assert after == before
