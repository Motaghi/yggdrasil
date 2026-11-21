import pytest

from driver_a11 import INTEGRATION, LANE


@pytest.mark.ac('AC-11')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', ['worker', 'reserved', 'uncertain'])
def test_ac11_row1_cancel_waits_for_every_lane_activation(a11_1, run):
    d = a11_1
    d.authorise()
    if run == 'worker':
        d.agent('pm1tl1w1', mode='persist')
        d.run('launch', 'pm1tl1w1')
        d.wait_launch('pm1tl1w1')
    elif run == 'reserved':
        d.config(launch_confirm_seconds=1)
        d.start_delay(10)
        d.send(10, body='WP-1')
    else:
        d.agent(mode='persist')
        d.run('launch', 'pm1tl1')
        d.wait_launch('pm1tl1')
        d.signal_agent(supervisor=True)
        d.tick()
    head = d.ref(LANE)
    before = [r for r in d.calls() if r.get('event') in {'interrupt', 'child-signal'}]
    if run == 'reserved':
        with d.tick_until_reserved():
            result = d.cancel()
    else:
        result = d.cancel()
    assert result.returncode == 3
    expected = {'worker': 'the lane still has a running agent (pm1tl1w1); stop it first',
                'reserved': 'the lane still has a running agent (pm1tl1); stop it first',
                'uncertain': 'the lane still has a running agent (pm1tl1); stop it, then ygg node release pm1tl1 --as aborted|failed'}
    assert expected[run] in result.stderr
    assert (d.wp() or {}).get('state') == 'authorised'
    assert [r for r in d.calls() if r.get('event') in {'interrupt', 'child-signal'}] == before
    assert d.ref(LANE) == head


@pytest.mark.ac('AC-11')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', ['untracked', 'unstaged', 'staged', 'ignored'])
def test_ac11_row2_cancel_requires_clean_worktree(a11_1, run):
    d = a11_1
    d.authorise()
    file = {'untracked': 'scripts/new.py', 'ignored': 'build/out.txt'}.get(run, 'scripts/a.py')
    d.write(d.lane() / file, 'dirty\n')
    if run == 'staged':
        d.trusted_git(d.lane(), 'add', file)
    before = d.lane_snapshot()
    result = d.cancel()
    if run == 'ignored':
        assert result.returncode == 0
    else:
        assert result.returncode == 3 and 'the lane has uncommitted changes' in result.stderr
        assert (d.wp() or {}).get('state') == 'authorised'
        assert d.lane_snapshot() == before


@pytest.mark.ac('AC-11')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', ['authorised', 'suspended'])
def test_ac11_row3_cancel_names_all_unfinished_packages(a11_1, run):
    d = a11_1
    d.one_stage()
    d.authorise('WP-1')
    d.build('scripts/a.py', 'x = 2\n', 'C7')
    c7 = d.ref(LANE)
    d.authorise('WP-3')
    d.build('scripts/c.py', 'WP-3\n', 'C8')
    c8 = d.ref(LANE)
    if run == 'suspended':
        d.suspended(('WP-1', 'WP-3'))
    head = d.ref(LANE)
    result = d.cancel('WP-1', *(['WP-3'] if run == 'suspended' else []))
    if run == 'authorised':
        assert result.returncode == 3 and 'finish or cancel WP-3 on this lane first' in result.stderr
        assert (d.wp() or {}).get('state') == 'authorised'
        assert (d.wp('WP-3') or {}).get('state') == 'authorised'
        assert d.ref(LANE) == head
    else:
        assert result.returncode == 0
        assert '2 commits dropped' in result.stdout and c7[:7] in result.stdout and c8[:7] in result.stdout
        assert d.ref(LANE) == d.l0
        assert (d.wp() or {}).get('state') == 'cancelled'
        assert (d.wp('WP-3') or {}).get('state') == 'cancelled'


@pytest.mark.ac('AC-13')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', ['integration', 'main', 'sync'])
def test_ac13_row1_cancel_refuses_work_that_already_landed(a11_1, run):
    d = a11_1
    package = 'WP-1'
    if run == 'main':
        d.authorise()
        _, c1 = d.build()
        d.trusted_git(d.repo_a, 'merge', '--ff-only', c1)
    elif run == 'integration':
        d.one_stage()
        d.reviewed_accept()
        d.merge_lane()
        d.home_hook(code=1)
        d.end_and_check()
        d.run('wp', 'reopen', 'alpha-p1', '--package', 'WP-1', env={'YGG_NODE': 'pm1'})
        d.build('scripts/c.py', 'fix\n', 'C4')
    else:
        d.two_accepted()
        d.main_change()
        d.sync()
        d.merge_lane()
        d.home_hook(code=1)
        d.end_and_check()
        package = 'WP-2'
        d.run('wp', 'reopen', 'alpha-p1', '--package', package, env={'YGG_NODE': 'pm1'})
    head, integration = d.ref(LANE), d.ref(INTEGRATION)
    result = d.cancel(package)
    assert result.returncode == 3
    location = 'main' if run == 'main' else "the project's integration branch"
    assert f'{package} is already in {location}; send a revert through review instead' in result.stderr
    assert (d.wp(package) or {}).get('state') == 'authorised'
    assert d.ref(LANE) == head and d.ref(INTEGRATION) == integration


@pytest.mark.ac('AC-13')
@pytest.mark.release('v1.0-blocker')
def test_ac13_row2_interrupted_cancel_waits_for_user(a11_1):
    d = a11_1
    _, _, killed = d.interrupted()
    assert killed.returncode != 0
    assert (d.wp() or {}).get('state') == 'authorised'
    before, workers = len(d.records()), len(d.records('pm1tl1w1'))
    starts = len(d.launches('pm1tl1'))
    d.send(13, body='x')
    integration = d.ref(INTEGRATION)
    for _ in range(2):
        d.tick()
        assert len(d.node_alerts('ygg-cancel-interrupted')) == 1
    assert len(d.records()) == before and len(d.records('pm1tl1w1')) == workers
    assert 'lane pm1tl1: an interrupted cancel needs finishing' in d.run('status').stdout
    alerts = d.node_alerts('ygg-cancel-interrupted')
    assert alerts[0]['class'] == 'user'
    refusal = 'a cancel on this lane was interrupted; ask your main session to finish it'
    result = d.run('launch', 'pm1tl1')
    assert result.returncode == 3 and refusal in result.stderr
    assert len(d.launches('pm1tl1')) == starts
    result = d.merge_lane()
    assert result.returncode == 3 and refusal in result.stderr
    assert d.ref(INTEGRATION) == integration
    snapshot = d.cancel_snapshot()
    preview = d.finish()
    assert preview.returncode == 3
    for path in ['scripts/a.py', 'scripts/d.py']:
        assert any(path in line and 'from the dropped commits' in line for line in preview.stdout.splitlines())
    assert preview.stdout.strip().splitlines()[-1].startswith('finish code:')
    assert d.finish_code(preview)
    assert d.cancel_snapshot() == snapshot and (d.wp() or {}).get('state') == 'authorised'
    result = d.finish(d.finish_code(preview))
    assert result.returncode == 0
    assert d.ref(LANE) == d.l0 and d.clean_status(ignored=True) == ''
    assert (d.wp() or {}).get('state') == 'cancelled'
    assert 'lane pm1tl1: an interrupted cancel needs finishing' not in d.run('status').stdout
    d.agent(mode='exit', exit_code=0)
    d.tick()
    d.wait_exit('pm1tl1')
    assert len(d.launches('pm1tl1')) - starts == 1
    assert 'm-0013' in d.receipt_text()
    assert len(d.node_alerts('ygg-cancel-interrupted')) == 1


@pytest.mark.ac('AC-13')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', ['after-reservation', 'already-cancelled', 'shared'])
def test_ac13_row3_launcher_last_check_settles_each_item(a11_1, run):
    d = a11_1
    if run == 'already-cancelled':
        d.cancel_commits()
        d.cancel()
        d.authorise('WP-4')
        d.send(15, body='WP-1 is cancelled; go on with WP-4')
        messages = d.pending_messages('pm1tl1', 'work')
        message_id = next((m['msg_id'] for m in messages
                           if m.get('body') == 'WP-1 is cancelled; go on with WP-4'), None)
    else:
        d.authorise()
        if run == 'shared':
            d.authorise('WP-4', 'pm1tl2')
            d.send(15, body='WP-1 and WP-4 share the config change')
        d.config(launch_confirm_seconds=1)
        d.start_delay(10)
    before, starts = len(d.records()), len(d.launches('pm1tl1'))
    d.agent(mode='exit', exit_code=0)
    if run == 'already-cancelled':
        d.tick()
        d.wait_exit('pm1tl1')
        assert len(d.launches('pm1tl1')) - starts == 1
        record = d.current() or {}
        assert d.activation_aborted(record) is False
        assert message_id and message_id in str(record)
        assert 'WP-4' in d.work_packages(record) and 'WP-1' not in d.work_packages(record)
    else:
        with d.tick_until_reserved():
            d.seed_cancel('alpha-p1', 'WP-1')
        record = d.wait_terminal()
        d.env.pop('YGG_TEST_BARRIER', None)
        d.env.pop('YGG_TEST_BARRIER_DIR', None)
        assert d.activation_aborted(record or {}) is True
        assert len(d.launches('pm1tl1')) == starts
        if run == 'shared':
            assert 'm-0015' in d.pending_ids()
        d.tick(2)
        d.wait_exit('pm1tl1')
        if run == 'shared':
            assert len(d.records()) - before == 2
            assert len(d.launches('pm1tl1')) - starts == 1
            record = d.current() or {}
            assert 'm-0015' in str(record)
            assert d.work_packages(record) == ['WP-4']
        else:
            assert len(d.records()) - before == 1
            assert len(d.launches('pm1tl1')) == starts
