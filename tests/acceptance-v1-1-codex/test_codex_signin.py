"""Revision 3.9 examples, transcribed from the signed acceptance criteria."""
import stat

import pytest


R1 = '{"tokens": {"refresh_token": "r1"}}'
R2 = '{"tokens": {"refresh_token": "r2"}}'
RC = '{"tokens": {"refresh_token": "rc"}}'
OWN_LOGIN = '{"x": 1}'


@pytest.fixture
def s(d):
    d.signin_boundary()
    return d


@pytest.fixture
def u(factory):
    driver = factory(codex=False)
    driver.signin_boundary()
    return driver


def assert_link(record, name, target):
    entry = record.get(name, {})
    assert stat.S_ISLNK(entry.get('lstat', {}).get('st_mode', 0))
    assert entry.get('target') == str(target)


def assert_no_links(record):
    assert record.get('auth.json', {}).get('exists') is False
    assert record.get('auth.lock', {}).get('exists') is False


def source_login(d):
    source = d.T / 'codex-home'
    d.env['CODEX_HOME'] = str(source)
    d.write(source / 'auth.json', OWN_LOGIN)
    return source


def custom_login(d):
    custom = d.T / 'custom-login'
    d.env['YGG_CONFIG'] = str(d.config_path)
    d.config(agent_codex_login_dir=str(custom))
    d.signin_answer(['login', '--device-auth'], auth=RC)
    d.run('codex', 'login', '--device-auth')
    return custom


def missing_signin_ticks(d):
    d.plant_login()
    d.authorised('WP-1')
    d.package('WP-2', lane='pm1tl2', criteria=True, authorise=True)
    d.config(max_launches_per_tick=1)
    d.signin_agent('pm1tl1')
    d.signin_agent('pm1tl2')
    d.ticks_until(lambda: bool(d.launches('pm1tl1')) and bool(d.launches('pm1tl2')))


@pytest.mark.ac('AC-59')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac59_row1_device_login_uses_private_file_store(u):
    u.env['CODEX_API_KEY'] = 'k-shell-1'
    u.signin_answer(['login', '--device-auth'], auth=R1)
    result = u.run('codex', 'login', '--device-auth')
    assert result.returncode == 0
    calls = u.signin_calls()
    assert len(calls) == 1
    assert calls[0]['argv'] == ['login', '--device-auth']
    assert calls[0]['env'].get('CODEX_HOME') == str(u.L)
    assert 'CODEX_API_KEY' not in calls[0]['env']
    assert u.is_dir(u.L)
    assert u.mode(u.L) == 0o700
    assert u.toml(u.L / 'config.toml').get('cli_auth_credentials_store') == 'file'
    assert u.read(u.L / 'auth.json') == R1
    assert 'r1' not in result.stdout
    assert 'r1' not in result.stderr


@pytest.mark.ac('AC-59')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac59_row2_api_login_forwards_stdin_without_printing(u):
    u.env['CODEX_API_KEY'] = 'k-shell-1'
    u.signin_answer(['login', '--with-api-key'], save_stdin=True)
    result = u.run('codex', 'login', '--with-api-key', stdin='k-row2-123\n')
    assert result.returncode == 0
    assert any(call['argv'] == ['login', '--with-api-key'] for call in u.signin_calls())
    assert u.read(u.L / 'stdin.txt') == 'k-row2-123\n'
    assert 'k-row2-123' not in result.stdout
    assert 'k-row2-123' not in result.stderr


@pytest.mark.ac('AC-59')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac59_row3_login_refuses_symlink_folder(u):
    u.mkdir(u.T / 'elsewhere')
    u.symlink(u.L, u.T / 'elsewhere')
    result = u.run('codex', 'login', '--device-auth')
    assert result.returncode == 3
    assert 'is not a real folder of yours' in result.stderr
    assert u.signin_calls() == []


@pytest.mark.ac('AC-60')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac60_row1_status_forwards_output_and_version_warning(u):
    u.plant_login(auth=R1, config=True)
    u.signin_answer(['login', 'status'], stdout='Logged in using ChatGPT\n', exit=0)
    result = u.run('codex', 'status',
        env={'YGG_TEST_CODEX_VERSION': 'codex-cli 0.171.0'})
    assert result.returncode == 0
    assert 'Logged in using ChatGPT' in result.stdout
    assert 'ygg: this Codex version is not probed for the shared sign-in' in result.stderr
    assert any(call['argv'] == ['login', 'status']
        and call['env'].get('CODEX_HOME') == str(u.L) for call in u.signin_calls())


@pytest.mark.ac('AC-60')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac60_row2_logout_forwards_nonzero_exit(u):
    u.plant_login(auth=R1, config=True)
    u.signin_answer(['logout'], stdout='Not logged in\n', exit=1)
    result = u.run('codex', 'logout',
        env={'YGG_TEST_CODEX_VERSION': 'codex-cli 0.170.0'})
    assert result.returncode == 1
    assert 'Not logged in' in result.stdout
    assert 'not probed' not in result.stderr
    assert any(call['argv'] == ['logout'] for call in u.signin_calls())


@pytest.mark.ac('AC-60')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac60_row3_logout_refuses_running_codex_session(s):
    s.plant_login(auth=R1, config=True)
    before = s.read_bytes(s.L / 'auth.json')
    s.signin_launch(running=True)
    try:
        result = s.run('codex', 'logout')
        assert result.returncode == 3
        assert 'refused: Codex sessions are running; stop them or let them end, then log out' in result.stderr
        assert not any(call['argv'][:1] == ['logout'] for call in s.signin_calls())
        assert s.read_bytes(s.L / 'auth.json') == before
    finally:
        s.exit_agent('pm1tl1')


@pytest.mark.ac('AC-61')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac61_row1_agent_cannot_login(factory):
    d = factory(codex=False)
    d.signin_boundary()
    d.launch(binary='claude')
    result = d.run('codex', 'login', '--device-auth', env={'YGG_NODE': 'pm1tl1'})
    assert result.returncode == 3
    assert 'user-only' in result.stderr
    assert d.signin_calls() == []


@pytest.mark.ac('AC-61')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac61_row2_relay_cannot_logout(s):
    s.plant_login(auth=R1)
    s.signin_launch(steps=[{'id': 'logout', 'run': ['ygg', 'codex', 'logout']}])
    answer = s.step('logout')
    assert answer.returncode != 0
    assert 'is not available to managed sessions' in answer.stdout + answer.stderr
    assert not any(call['argv'][:1] == ['logout'] for call in s.signin_calls())
    assert s.read(s.L / 'auth.json') == R1


@pytest.mark.ac('AC-62')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac62_row1_shared_links_removed_after_settlement(s):
    s.plant_login(auth=R1)
    source = source_login(s)
    result = s.signin_launch()
    assert result.returncode == 0
    record = s.signin_snapshot()
    assert_link(record, 'auth.json', s.L / 'auth.json')
    assert_link(record, 'auth.lock', s.L / 'auth.lock')
    home = s.signin_home()
    assert not s.exists(home / 'auth.json') and not s.is_link(home / 'auth.json')
    assert not s.exists(home / 'auth.lock') and not s.is_link(home / 'auth.lock')
    assert s.read(s.L / 'auth.json') == R1
    assert s.is_regular(s.L / 'auth.lock')
    assert s.read_bytes(s.L / 'auth.lock') == b''
    assert s.mode(s.L / 'auth.lock') == 0o600
    assert all(s.read_bytes(path) != s.read_bytes(source / 'auth.json')
        for path in s.files(home.parent))
    assert s.read(source / 'auth.json') == OWN_LOGIN


@pytest.mark.ac('AC-62')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac62_row2_refresh_through_link_reaches_next_session(s):
    s.plant_login(auth=R1)
    source = source_login(s)
    s.signin_launch(refresh=R2)
    s.signin_launch('pm1tl2')
    assert s.read(s.L / 'auth.json') == R2
    record = s.signin_snapshot('pm1tl2')
    assert_link(record, 'auth.json', s.L / 'auth.json')
    assert record.get('auth.json', {}).get('content') == R2
    home = s.signin_home('pm1tl2')
    assert not s.exists(home / 'auth.json') and not s.is_link(home / 'auth.json')
    assert not s.exists(home / 'auth.lock') and not s.is_link(home / 'auth.lock')
    assert s.read(source / 'auth.json') == OWN_LOGIN


@pytest.mark.ac('AC-62')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac62_row3_launch_never_imports_user_login(s):
    s.plant_login()
    source = source_login(s)
    result = s.signin_launch()
    assert result.returncode == 0
    assert_no_links(s.signin_snapshot())
    assert all(s.read_bytes(path) != s.read_bytes(source / 'auth.json')
        for path in s.files(s.signin_home().parent))
    assert s.read(source / 'auth.json') == OWN_LOGIN


@pytest.mark.ac('AC-63')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac63_row1_builder_refuses_linked_auth_file(s):
    s.plant_login()
    s.write(s.T / 'target.json', '{"t": 1}')
    s.symlink(s.L / 'auth.json', s.T / 'target.json')
    result = s.signin_launch()
    assert result.returncode == 3
    assert 'launch precondition: ' in result.stderr
    assert '/auth.json is not a regular file of yours' in result.stderr
    assert s.launches('pm1tl1') == []
    assert s.read(s.T / 'target.json') == '{"t": 1}'


@pytest.mark.ac('AC-63')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac63_row2_builder_refuses_linked_auth_lock(s):
    s.plant_login(auth='')
    s.write(s.T / 'lock-target', 'keep')
    s.symlink(s.L / 'auth.lock', s.T / 'lock-target')
    result = s.signin_launch()
    assert result.returncode == 3
    assert 'launch precondition: ' in result.stderr
    assert '/auth.lock is not a regular file of yours' in result.stderr
    assert s.read(s.T / 'lock-target') == 'keep'


@pytest.mark.ac('AC-63')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac63_row3_unprobed_reviewer_gets_no_shared_credentials(s):
    s.plant_login(auth=R1)
    s.env['CODEX_API_KEY'] = 'k-rv-2'
    s.signin_review_given()
    s.env['YGG_TEST_CODEX_VERSION'] = 'codex-cli 0.171.0'
    s.signin_review_ticks()
    assert_no_links(s.signin_snapshot('pm1rv1'))
    launch = s.launch_record('pm1rv1')
    assert launch.get('binary') == 'codex'
    assert 'CODEX_API_KEY' not in launch.get('env', {})
    assert 'unprobed: no shared sign-in' in s.run('status').stdout
    alerts = s.signin_alerts()
    assert len(alerts) == 1
    assert alerts[0].get('class') == 'log'
    assert 'not probed' in alerts[0]['text']


@pytest.mark.ac('AC-64')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac64_row1_repository_cannot_set_login_folder(s):
    s.write_yaml(s.H / 'yggdrasil.yaml', {'agent_codex_login_dir': str(s.H / 'login')})
    result = s.run('launch', 'pm1tl1', cwd=s.H)
    assert result.returncode == 1
    assert 'agent_codex_login_dir may be set only in a config named by YGG_CONFIG, the home config, a profile or the environment' in result.stderr
    assert s.launches('pm1tl1') == []


@pytest.mark.ac('AC-64')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac64_row2_login_folder_cannot_be_inside_state_root(s):
    s.config(agent_codex_login_dir=str(s.S / 'codex-login'))
    result = s.run('codex', 'login', '--device-auth')
    assert result.returncode == 3
    assert 'refused: agent_codex_login_dir' in result.stderr
    assert 'lies inside' in result.stderr
    assert s.signin_calls() == []


@pytest.mark.ac('AC-64')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac64_row3_linked_project_path_cannot_receive_login(s):
    s.mkdir(s.H / '.ygg-worktrees')
    s.symlink(s.T / 'up', s.H / '.ygg-worktrees')
    s.config(agent_codex_login_dir=str(s.T / 'up/login'))
    result = s.run('codex', 'login', '--device-auth')
    assert result.returncode == 3
    assert 'lies inside' in result.stderr
    assert s.signin_calls() == []
    assert not s.exists(s.H / '.ygg-worktrees/login')


@pytest.mark.ac('AC-65')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac65_row1_codex_receives_key_and_excludes_it_from_shells(s):
    s.plant_login()
    s.env['CODEX_API_KEY'] = 'k-row1-456'
    result = s.signin_launch()
    assert result.returncode == 0
    launch = s.launch_record()
    assert launch.get('env', {}).get('CODEX_API_KEY') == 'k-row1-456'
    config = s.toml(s.signin_home() / 'config.toml')
    assert config.get('cli_auth_credentials_store') == 'file'
    excluded = config.get('shell_environment_policy', {}).get('exclude', [])
    assert all(name in excluded for name in ['CODEX_API_KEY', 'OPENAI_API_KEY', 'CODEX_ACCESS_TOKEN'])
    argv = launch.get('argv', [])
    overrides = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == '-c']
    assert 'cli_auth_credentials_store="file"' in overrides
    assert any(value.startswith('shell_environment_policy.exclude=')
        and all(name in value for name in ['CODEX_API_KEY', 'OPENAI_API_KEY', 'CODEX_ACCESS_TOKEN'])
        for value in overrides)


@pytest.mark.ac('AC-65')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac65_row2_claude_drops_key_after_environment_overlays(factory):
    d = factory(codex=False)
    d.signin_boundary()
    d.env['CODEX_API_KEY'] = 'k-row2-456'
    d.config(agent_env_passthrough=['CODEX_API_KEY'])
    d.signin_profile(launch_env={'CODEX_API_KEY': 'k-row2-789'})
    d.launch(binary='claude')
    launch = d.launch_record()
    assert launch.get('binary') == 'claude'
    assert 'CODEX_API_KEY' not in launch.get('env', {})


@pytest.mark.ac('AC-65')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac65_row3_relay_child_drops_codex_key(s):
    s.plant_login()
    s.env['CODEX_API_KEY'] = 'k-row1-456'
    s.signin_relay_wrapper()
    s.signin_launch(steps=[{'id': 'doctor', 'run': ['ygg', 'doctor', '--json']}])
    assert s.step('doctor').returncode == 0
    environments = s.signin_relay_environments()
    assert len(environments) == 1
    assert 'CODEX_API_KEY' not in environments[0]


@pytest.mark.ac('AC-66')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac66_row1_doctor_prefers_provider_key_without_printing_keys(s):
    s.signin_provider()
    s.env.update(HUB_KEY='hk-1', CODEX_API_KEY='k-1')
    s.config(agent_env_passthrough=['HUB_KEY'])
    s.plant_login(auth='')
    result = s.run('doctor', '--json')
    assert result.returncode == 0
    route = s.result_json(result).get('codex_sign_in', {})
    assert route.get('route') == 'provider key'
    assert 'hub' in route.get('detail', '')
    assert 'hk-1' not in result.stdout
    assert 'k-1' not in result.stdout


@pytest.mark.ac('AC-66')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac66_row2_blank_api_key_uses_yggdrasil_login(s):
    s.env['CODEX_API_KEY'] = '   '
    s.plant_login(auth=R1)
    result = s.run('doctor', '--json')
    assert s.result_json(result).get('codex_sign_in', {}).get('route') == 'yggdrasil login'
    assert 'r1' not in result.stdout


@pytest.mark.ac('AC-66')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac66_row3_config_without_auth_requests_login_again(s):
    s.plant_login(config=True)
    result = s.run('doctor', '--json')
    route = s.result_json(result).get('codex_sign_in', {})
    assert route.get('route') == 'none'
    assert 'run ygg codex login again' in route.get('detail', '')


@pytest.mark.ac('AC-67')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac67_row1_missing_signin_alert_is_once_across_tick_processes(s):
    missing_signin_ticks(s)
    alerts = s.signin_alerts()
    assert len(alerts) == 1
    assert alerts[0].get('class') == 'log'
    assert 'ygg codex login' in alerts[0]['text']
    assert s.signin_alerts(user=True) == []


@pytest.mark.ac('AC-67')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac67_row2_status_names_missing_signin(s):
    missing_signin_ticks(s)
    assert 'codex sign-in: none (' in s.run('status').stdout


@pytest.mark.ac('AC-68')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac68_row1_probed_reviewer_links_shared_login(s):
    s.plant_login(auth=R1)
    s.signin_review_given()
    s.env['YGG_TEST_CODEX_VERSION'] = 'codex-cli 0.170.0'
    s.signin_review_ticks()
    record = s.signin_snapshot('pm1rv1')
    assert_link(record, 'auth.json', s.L / 'auth.json')
    assert_link(record, 'auth.lock', s.L / 'auth.lock')


@pytest.mark.ac('AC-68')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac68_row2_probed_reviewer_receives_api_key(s):
    s.plant_login(auth=R1)
    s.signin_review_given()
    s.env.update(YGG_TEST_CODEX_VERSION='codex-cli 0.170.0', CODEX_API_KEY='k-rv-1')
    s.signin_review_ticks()
    assert s.launch_record('pm1rv1').get('env', {}).get('CODEX_API_KEY') == 'k-rv-1'


@pytest.mark.ac('AC-68')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac68_row3_all_t3_processes_drop_codex_key(factory):
    d = factory(codex=False)
    d.signin_boundary()
    d.config(launcher='t3')
    d.a5_t3()
    d.env['CODEX_API_KEY'] = 'k-t3-1'
    d.run('launch', 'pm1tl1')
    calls = d.calls('t3')
    assert calls
    assert any(call['argv'][:2] == ['thread', 'start'] for call in calls)
    assert all('CODEX_API_KEY' not in call.get('env', {}) for call in calls)


@pytest.mark.ac('AC-69')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac69_row1_missing_provider_key_has_no_fallback(s):
    s.signin_provider()
    s.env['CODEX_API_KEY'] = 'k-1'
    s.plant_login(auth='')
    result = s.run('doctor', '--json')
    route = s.result_json(result).get('codex_sign_in', {})
    assert route.get('route') == 'none'
    assert 'HUB_KEY' in route.get('detail', '')
    assert 'k-1' not in result.stdout


@pytest.mark.ac('AC-69')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac69_row2_blank_provider_key_has_no_fallback(s):
    s.signin_provider()
    s.env.update(CODEX_API_KEY='k-1', HUB_KEY='   ')
    s.config(agent_env_passthrough=['HUB_KEY'])
    s.plant_login(auth='')
    result = s.run('doctor', '--json')
    assert s.result_json(result).get('codex_sign_in', {}).get('route') == 'none'


@pytest.mark.ac('AC-69')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac69_row3_doctor_reports_api_key_without_printing_it(s):
    s.env['CODEX_API_KEY'] = 'k-2'
    s.plant_login()
    result = s.run('doctor', '--json')
    assert s.result_json(result).get('codex_sign_in', {}).get('route') == 'api key'
    assert 'k-2' not in result.stdout


@pytest.mark.ac('AC-70')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac70_row1_login_uses_custom_global_folder(s):
    custom = s.T / 'custom-login'
    s.env['YGG_CONFIG'] = str(s.config_path)
    s.config(agent_codex_login_dir=str(custom))
    s.signin_answer(['login', '--device-auth'], auth=RC)
    result = s.run('codex', 'login', '--device-auth')
    assert result.returncode == 0
    assert any(call['env'].get('CODEX_HOME') == str(custom) for call in s.signin_calls())
    assert s.read(custom / 'auth.json') == RC
    assert not s.exists(s.L)


@pytest.mark.ac('AC-70')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac70_row2_builder_links_custom_login_folder(s):
    custom = custom_login(s)
    s.signin_launch()
    assert_link(s.signin_snapshot(), 'auth.json', custom / 'auth.json')


@pytest.mark.ac('AC-70')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac70_row3_doctor_uses_custom_login_without_printing_it(s):
    custom_login(s)
    result = s.run('doctor', '--json')
    assert s.result_json(result).get('codex_sign_in', {}).get('route') == 'yggdrasil login'
    assert 'rc' not in result.stdout


@pytest.mark.ac('AC-71')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac71_row1_builder_refuses_linked_login_folder(s):
    real = s.plant_login(directory=s.T / 'realL', auth='')
    s.symlink(s.L, real)
    result = s.signin_launch()
    assert result.returncode == 3
    assert 'launch precondition: ' in result.stderr
    assert 'is not a real folder of yours' in result.stderr
    assert s.launches('pm1tl1') == []


@pytest.mark.ac('AC-71')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac71_row2_builder_refuses_group_access_to_login_folder(s):
    s.plant_login(auth='')
    s.chmod(s.L, 0o750)
    result = s.signin_launch()
    assert result.returncode == 3
    assert 'is not a real folder of yours' in result.stderr
    assert s.launches('pm1tl1') == []


@pytest.mark.ac('AC-71')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac71_row3_logout_lease_spans_state_roots(s):
    s.plant_login(auth='')
    before = s.read_bytes(s.L / 'auth.json')
    first_root = s.S
    with s.second_state_root() as second:
        launch = second.signin_launch(running=True)
        assert launch.returncode == 0
        assert second.signin_running()
        result = s.run('codex', 'logout', env={'YGG_STATE_ROOT': str(first_root)})
        assert result.returncode == 3
        assert 'refused: Codex sessions are running' in result.stderr
        assert not any(call['argv'][:1] == ['logout'] for call in s.signin_calls())
        assert s.read_bytes(s.L / 'auth.json') == before


@pytest.mark.ac('AC-72')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac72_row1_doctor_ignores_provider_key_dropped_from_session(s):
    s.signin_provider()
    s.env.update(HUB_KEY='hk-1', CODEX_API_KEY='k-1')
    s.plant_login(auth='')
    result = s.run('doctor', '--json')
    route = s.result_json(result).get('codex_sign_in', {})
    assert route.get('route') == 'none'
    assert 'HUB_KEY' in route.get('detail', '')
    assert 'hk-1' not in result.stdout
    assert 'k-1' not in result.stdout


@pytest.mark.ac('AC-72')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac72_row2_doctor_sees_provider_key_from_profile(s):
    s.signin_provider()
    s.env['CODEX_API_KEY'] = 'k-1'
    s.plant_login(auth='')
    s.signin_profile(launch_env={'HUB_KEY': 'hk-2'})
    result = s.run('doctor', '--json')
    assert s.result_json(result).get('codex_sign_in', {}).get('route') == 'provider key'
    assert 'hk-2' not in result.stdout
