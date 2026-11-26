import json

import pytest

from assertions import present, merge_parents


DEFAULTS = [
    ('stage_close', 'default'), ('agent_project_settings', 'exclude'), ('agent_env_passthrough', []),
    ('agent_claude_config_dir', '~/.config/yggdrasil/agent-claude'), ('command_retry_limit', 3),
    ('busy_max_minutes', 60), ('needs_l1_minutes', 30), ('merge_check_timeout_minutes', 60),
    ('merge_check_path', '{conductor-path}'), ('merge_check_setup', None), ('alert_t3_thread', None),
    ('alert_command', None), ('alert_send_timeout_seconds', 120), ('alert_max_attempts', 10),
    ('alert_tick_budget_seconds', 150), ('agent_commands', []), ('dashboard_allowed_hosts', []),
    ('models.review_model', None), ('alert_forwarder', 'auto'), ('review_rounds', 5),
]
SWITCH_CRITERIA = ['AC-07', 'AC-08', 'AC-09', 'AC-14', 'AC-22', 'AC-23', 'AC-24',
                     'AC-36', 'AC-44', 'AC-45', 'AC-46', 'AC-47', 'AC-48', 'AC-49',
                     'AC-50', 'AC-62', 'AC-63', 'AC-77', 'AC-78', 'AC-79']


@pytest.mark.ac('AC-35')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('key,default', DEFAULTS)
def test_ac35_row1_defaults(a11_factory, key, default):
    d = a11_factory(tree=False)
    d.remove(d.config_path)
    d.env.pop('YGG_PROFILE', None)
    if default == '{conductor-path}':
        default = d.env['PATH']
    result = d.run('config', 'explain', key)
    assert key + ' = ' + json.dumps(default) + '  (core)' in result.stdout


@pytest.mark.ac('AC-35')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row,key,value,text', [
    (2, 'stage_close', 'sometimes', "stage_close must be default, auto or pm-only, not 'sometimes'"),
    (3, 'command_retry_limit', 0, 'command_retry_limit must be a positive whole number')])
def test_ac35_invalid_values(a11, row, key, value, text):
    a11.config(**{key: value})
    result = a11.run('status')
    assert result.returncode == 1
    assert text in result.stderr


@pytest.mark.ac('AC-98')
@pytest.mark.release('v1.0-blocker')
def test_ac98_row1_programme_cannot_set_agent_commands(a11):
    a11.programme_config(agent_commands=['make test'])
    result = a11.run('config', 'explain', 'agent_commands', '--project', 'alpha-p1')
    assert 'ignored' in result.stdout and 'programme' in result.stdout


@pytest.mark.ac('AC-98')
@pytest.mark.release('v1.0-blocker')
def test_ac98_row2_retry_limit_unchanged(a11):
    assert 'retry_limit = 2  (core)' in a11.run('config', 'explain', 'retry_limit').stdout


@pytest.mark.ac('AC-98')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('key,value', [('alert_command', '[x]'), ('agent_env_passthrough', '[TZ]')])
def test_ac98_row3_untrusted_config(a11, key, value):
    folder = a11.make_cwd('untrusted')
    a11.write(folder / 'yggdrasil.yaml', key + ': ' + value + '\n')
    result = a11.run('status', cwd=folder)
    assert result.returncode == 1
    assert key + ' may be set only in a config named by YGG_CONFIG, the home config, a profile or the environment' in result.stderr


@pytest.mark.ac('AC-99')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('layer', ['pid', 'user', 'profile', 'programme', 'reviewer-only', 'programme-reviewer'])
def test_ac99_row1_codex_roles(a11, layer):
    models = {'model': 'gpt-6.1-sol', 'effort': 'high'}
    if layer == 'pid':
        a11.pid(models=models)
    elif layer == 'user':
        a11.config(launcher_cli='codex')
    elif layer == 'profile':
        a11.profile(models=models)
    elif layer == 'programme':
        a11.programme_config(models=models)
    elif layer == 'reviewer-only':
        a11.pid(models={'review_model': 'gpt-6.1-sol'})
    else:
        a11.run('node', 'add', 'rv1', '--role', 'reviewer', '--parent', 'alpha')
        a11.run('launch', 'rv1')
        launches = a11.launches('rv1')
        assert len(launches) == 1 and launches[0]['binary'] == 'claude'
        assert 'claude-opus-5-5' in launches[0]['argv']
        return
    result = a11.run('pid', 'approve', 'alpha-p1')
    assert result.returncode == 0
    assert 'Codex builds' not in result.stderr


@pytest.mark.ac('AC-99')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', ['launch', 'node-add', 'package'])
def test_ac99_row2_later_codex_overrides(a11, run):
    a11.ready()
    if run == 'launch':
        a11.config(launcher_cli='codex')
        result = a11.run('launch', 'pm1tl1')
    elif run == 'node-add':
        a11.programme_config(models={'model': 'gpt-6.1-sol'})
        result = a11.run('node', 'add', 'pm1tl1w9', '--role', 'worker', '--parent', 'pm1tl1')
    else:
        a11.package('WP-1', criteria=True)
        a11.set_package_config(models={'model': 'gpt-6.1-sol'})
        result = a11.run('wp', 'authorise', 'alpha-p1', '--package', 'WP-1', '--lane', 'pm1tl1')
    assert result.returncode == (3 if run == 'package' else 0)
    if run == 'package':
        assert "WP-1 would run on Codex but lane ygg/alpha-p1/lane/pm1tl1 runs on Claude Code; a lane's team lead and workers use one runtime" in result.stderr
    if run == 'launch':
        a11.wait_exit('pm1tl1')
        before = len([call for call in a11.launches('pm1tl1') if call['binary'] == 'codex'])
        a11.send(10)
        a11.tick()
        assert len([call for call in a11.launches('pm1tl1') if call['binary'] == 'codex']) == before + 1
        assert [alert for alert in a11.alerts('ygg-launch-refused') if alert.get('node') == 'pm1tl1'] == []
    elif run == 'node-add':
        assert a11.node('pm1tl1w9') is not None
    else:
        assert present(a11.wp())['state'] == 'planned'


@pytest.mark.ac('AC-99')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('switch', ['dashboard', 'dashboard_serve', 'summary', 'doctor', 'confirm_window',
                                    't3_live_send', 'retry_cap', 'stage_close'])
def test_ac99_row3_disabled_features(a11, switch):
    a11.release_off(switch)
    if switch in {'dashboard', 'dashboard_serve', 'summary', 'doctor'}:
        commands = {'dashboard': ['dashboard', 'alpha', '--no-open'],
                    'dashboard_serve': ['dashboard', 'serve', 'alpha', '--port', '0'],
                    'summary': ['summary', '--since', '1h'], 'doctor': ['doctor']}
        if switch == 'dashboard':
            a11.tick()
            assert not a11.exists(a11.state / 'dashboard/alpha.html')
        result = a11.run(*commands[switch])
        assert result.returncode == 3
        assert ('ygg ' + {'dashboard': 'dashboard', 'dashboard_serve': 'dashboard serve',
                         'summary': 'summary', 'doctor': 'doctor'}[switch] + ' is not in this release') in result.stderr
        if switch == 'dashboard_serve':
            assert a11.no_dashboard_listener() is True
    elif switch == 'confirm_window':
        a11.delayed_given(delay=20, load='1.5', quiesce_pms=True)
        a11.later(12)
        a11.tick()
        assert a11.activation() in present(a11.status())['uncertain_keys']
        alerts = a11.alerts('ygg-uncertain')
        assert len(alerts) == 1 and alerts[0]['class'] == 'log'
    elif switch == 't3_live_send':
        a11.t3_running()
        a11.send(12, urgent=True)
        assert a11.t3_sends('thr-tl1') == []
    elif switch == 'retry_cap':
        a11.start()
        a11.fail_command()
        assert a11.call('make gate').returncode == 0
        assert 'make gate' not in a11.inbox().stdout
    else:
        a11.config(stage_close='auto')
        result = a11.run('status')
        assert result.returncode == 1 and 'stage_close: auto is not in this release' in result.stderr
        a11.config(stage_close=None)
        a11.accepted('WP-1')
        before = len(a11.close_task_launches('pm1'))
        a11.tick(2)
        assert 'ready to close' not in a11.line('alpha-p1')
        assert len(a11.close_task_launches('pm1')) == before
        assert present(a11.project())['stage']['state'] == 'executing'


@pytest.mark.ac('AC-99')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', [9, 10, 11])
def test_ac99_row3_written_release_file(a11, run):
    results = {key: [] for key in SWITCH_CRITERIA}
    if run == 9:
        results.update({'AC-63': [1], 'AC-49': [1]})
    elif run == 10:
        results['AC-45'] = [1]
    else:
        del results['AC-63']
    source = a11.read(a11.core_path('src/yggdrasil/data/release.yaml'))
    a11.release_script(results)
    written = a11.disk_yaml(a11.root / 'release.yaml')
    assert written is not None
    expected = {key: 'on' for key in ['dashboard', 'dashboard_serve', 'summary', 'doctor',
        'confirm_window', 'retry_pm_reset', 't3_live_send', 'retry_cap', 'stage_close']}
    for key in {9: ['stage_close', 'dashboard_serve'], 10: ['dashboard', 'dashboard_serve'], 11: ['stage_close']}[run]:
        expected[key] = 'off'
    assert written == expected
    a11.env['YGG_RELEASE_FILE'] = str(a11.root / 'release.yaml')
    a11.env.pop('YGG_TEST_RELEASE_OFF', None)
    if run != 11:
        result = a11.run('dashboard', 'serve', 'alpha', '--port', '0')
        assert result.returncode == 3
        assert 'ygg dashboard serve is not in this release' in result.stderr
    assert a11.read(a11.core_path('src/yggdrasil/data/release.yaml')) == source
