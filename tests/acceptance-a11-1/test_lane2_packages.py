import pytest

from driver_a11 import INTEGRATION, LANE


def state(d, package='WP-1'):
    value = d.wp(package)
    assert value is not None, 'The example requires an observable package'
    return value['state']


@pytest.mark.ac('AC-03')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('phase', ['authorised', 'in-review'])
def test_ac03_row1_acceptance_requires_testing(a11_1, phase):
    d = a11_1
    d.authorise()
    d.build()
    _, head = d.review(verdict='revise')
    if phase == 'in-review':
        d.review(verdict=None)
    before = d.ref(INTEGRATION)
    result = d.accept(head=head)
    assert result.returncode == 3 and 'WP-1 is not ready for test' in result.stderr
    assert state(d) == phase
    result = d.merge_lane()
    assert result.returncode == 3
    assert 'WP-1 on lane pm1tl1 is not accepted' in result.stderr
    assert d.ref(INTEGRATION) == before


@pytest.mark.ac('AC-03')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('phase', ['accepted', 'merged'])
@pytest.mark.parametrize('verdict', ['accept', 'reject'])
def test_ac03_row2_terminal_acceptance_changes_nothing(a11_1, phase, verdict):
    d = a11_1
    if phase == 'merged':
        d.merge_base()
        head = d.accepted_heads[0]
    else:
        head = d.reviewed_accept()
    result = d.accept(head=head, verdict=verdict)
    assert result.returncode == 3 and 'WP-1 is not ready for test' in result.stderr
    assert state(d) == phase
    assert d.work_owed('pm1tl1', 'WP-1') is False
    if phase == 'merged':
        assert 'merge check: passed' in d.line('alpha-p1')


@pytest.mark.ac('AC-05')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('phase', ['review-exhausted', 'suspended'])
def test_ac05_row1_cancel_allows_project_to_finish(a11_1, phase):
    d = a11_1
    d.one_stage()
    d.reviewed_accept('WP-2', file='scripts/c.py')
    d.merge_lane()
    d.authorise()
    d.pair_exhausted() if phase == 'review-exhausted' else d.suspended()
    d.staged_files_hook()
    result = d.cancel()
    assert result.returncode == 0
    assert state(d) == 'cancelled'
    assert d.work_owed('pm1tl1', 'WP-1') is False
    d.end_and_check()
    assert 'merge check: passed' in d.line('alpha-p1')
    assert (d.project() or {}).get('state') == 'ready-to-merge'
    assert d.merge_main().returncode == 0
    d.tick()
    assert (d.project() or {}).get('state') == 'closed'


@pytest.mark.ac('AC-05')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('phase', ['review-exhausted', 'suspended'])
def test_ac05_row2_reopen_resets_pair_review_count(a11_1, phase):
    d = a11_1
    d.authorise()
    d.pair_exhausted() if phase == 'review-exhausted' else d.suspended()
    result = d.run('wp', 'reopen', 'alpha-p1', '--package', 'WP-1', env={'YGG_NODE': 'pm1'})
    assert result.returncode == 0
    assert state(d) == 'authorised'
    messages = d.pending_messages('pm1tl1', 'work')
    assert len([m for m in messages if 'WP-1' in m.get('body', '')]) == 1
    d.tick()
    for number in range(1, 5):
        d.run('review', 'record', '--from', 'pm1tl1', '--package', 'WP-1',
              '--round', str(number), '--verdict', 'changes')
    assert state(d) == 'authorised'
    d.run('review', 'record', '--from', 'pm1tl1', '--package', 'WP-1',
          '--round', '5', '--verdict', 'changes')
    assert state(d) == 'review-exhausted'


@pytest.mark.ac('AC-05')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('verb', ['reopen', 'cancel'])
def test_ac05_row3_only_pm_can_rework_package(a11_1, verb):
    d = a11_1
    d.authorise()
    d.pair_exhausted()
    result = d.run('wp', verb, 'alpha-p1', '--package', 'WP-1', env={'YGG_NODE': 'pm1tl1'})
    assert result.returncode == 3
    assert 'identity: only the PM of alpha-p1' in result.stderr
    assert state(d) == 'review-exhausted'


@pytest.mark.ac('AC-06')
@pytest.mark.release('v1.0-blocker')
def test_ac06_row1_cancel_drops_stopped_work_before_main(a11_1):
    d = a11_1
    d.one_stage()
    s2 = d.reviewed_accept('WP-2', file='scripts/c.py')
    d.authorise()
    _, c1 = d.build()
    d.cap_current()
    d.answer_cap('stop')
    d.staged_files_hook()
    result = d.cancel()
    assert result.returncode == 0
    assert '1 commit dropped' in result.stdout and c1[:7] in result.stdout
    assert state(d) == 'cancelled'
    assert d.work_owed('pm1tl1', 'WP-1') is False
    assert d.ref(LANE) == s2 and d.clean_status() == ''
    assert d.merge_lane().returncode == 0
    d.end_and_check()
    assert 'merge check: passed' in d.line('alpha-p1')
    assert d.merge_main().returncode == 0
    d.tick()
    assert (d.project() or {}).get('state') == 'closed'
    assert d.contains(INTEGRATION, c1) is False and d.contains('main', c1) is False
    assert d.tree_text('main', 'scripts/a.py').splitlines()[:1] == ['x = 1']


@pytest.mark.ac('AC-06')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', ['authorised', 'rejected', 'shared'])
def test_ac06_row2_cancel_to_creation_and_settle_each_item(a11_1, run):
    d = a11_1
    c1, c2 = d.cancel_commits(rejected=run != 'authorised')
    if run == 'shared':
        d.authorise('WP-4', 'pm1tl2')
        d.send(14, body='WP-1 and WP-4 share the config change')
    before = len(d.launches('pm1tl1'))
    result = d.cancel()
    assert result.returncode == 0
    assert '2 commits dropped' in result.stdout and c1[:7] in result.stdout and c2[:7] in result.stdout
    assert d.ref(LANE) == d.l0
    assert d.read(d.lane() / 'scripts/a.py').splitlines()[:1] == ['x = 1']
    assert d.exists(d.lane() / 'scripts/d.py') is False
    assert d.clean_status() == '' and state(d) == 'cancelled'
    if run == 'shared':
        assert 'm-0014' in d.pending_ids()
        assert 'rejection:WP-1' not in d.inbox('pm1tl1').stdout
        d.agent(mode='exit', exit_code=0)
    else:
        assert d.work_owed('pm1tl1', 'WP-1') is False
        if run == 'rejected':
            assert 'rejection:WP-1' not in d.inbox('pm1tl1').stdout
    d.tick(2)
    d.wait_exit('pm1tl1')
    if run == 'shared':
        assert len(d.launches('pm1tl1')) - before == 1
        record = d.current() or {}
        assert 'm-0014' in str(record)
        assert 'WP-4' in d.work_packages(record) and 'WP-1' not in d.work_packages(record)
    else:
        assert len(d.launches('pm1tl1')) == before


@pytest.mark.ac('AC-06')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('commit', [True, False])
def test_ac06_row3_drop_point_keeps_recorded_integrated_syncs(a11_1, commit):
    d = a11_1
    _, _, m2 = d.two_accepted(syncs=True, merge=True)
    d.authorise('WP-3')
    c6 = d.build('scripts/c.py', 'new work\n', 'C6')[1] if commit else None
    result = d.cancel('WP-3')
    assert result.returncode == 0
    assert d.ref(LANE) == m2
    assert ('1 commit dropped' if commit else '0 commits dropped') in result.stdout
    if commit:
        assert c6[:7] in result.stdout
    assert state(d, 'WP-3') == 'cancelled'
