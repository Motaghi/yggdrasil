import json
import shlex

import pytest

from assertions import present, merge_parents

from driver_a11 import T


@pytest.mark.ac('AC-01')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac01_process_timezone(a11, row):
    a11.ready()
    a11.agent(mode='persist')
    a11.send(10)
    a11.tick()
    a11.wait_launch('pm1tl1')
    unrelated = a11.legacy_receipt(unrelated=row == 3) if row > 1 else None
    if row == 3:
        a11.run('stop', 'pm1tl1', env={'TZ': 'Europe/Copenhagen'})
        assert a11.alive(unrelated)
        assert present(a11.node())['held_reason'] is not None
    else:
        data = a11.status(env={'TZ': 'Europe/Copenhagen' if row == 1 else 'America/Los_Angeles'})
        assert data is not None
        assert next(n for n in data['nodes'] if n['id'] == 'pm1tl1')['state'] == 'running'
        assert present(a11.current())['outcome'] == 'confirmed'
        assert a11.activation() not in data['uncertain_keys']


@pytest.mark.ac('AC-02')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac02_stall_exit_and_busy(a11, row):
    if row == 3:
        a11.busy()
        a11.tick()
        assert 'busy (running make)' in a11.line()
        assert present(a11.node())['state'] == 'running'
        assert a11.alerts('ygg-stall') == []
        return
    a11.config(max_silence_minutes=1)
    a11.later(-120)
    a11.start()
    a11.at(T)
    a11.tick()
    a11.send(10)
    before = len(a11.launches('pm1tl1'))
    activation = a11.activation()
    if row == 1:
        a11.exit_agent('pm1tl1')
        a11.wait_exit('pm1tl1')
    else:
        a11.signal_agent(supervisor=True)
        a11.signal_agent()
    a11.tick(2)
    if row == 1:
        assert present(a11.node())['state'] != 'stalled-suspect'
        assert len(a11.launches('pm1tl1')) - before == 1
        assert 'm-0010' in a11.receipt_text()
    else:
        assert activation in present(a11.status())['uncertain_keys']
        assert len(a11.launches('pm1tl1')) == before
        assert 'm-0010' in json.dumps(present(a11.status())['inbox_pending'])


@pytest.mark.ac('AC-03')
@pytest.mark.release('v1.0-should')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac03_status_words(a11_factory, row):
    d = a11_factory(tree=row != 3)
    if row == 1:
        d.start()
        d.set_load('2.0')
        assert present(d.node())['state'] == 'running'
        assert present(d.node())['held_reason'] is None
        assert 'held-load' not in d.line()
    elif row == 2:
        d.ready()
        d.set_load('2.0')
        d.send(recipient='pm1tl2')
        d.tick()
        assert 'waiting for the computer to be less busy (other programs are using it; load 2.0, limit 1.6)' in d.line('pm1tl2')
    else:
        d.run('pause')
        assert d.run('status').stdout.splitlines()[:1] == ['Paused: everything.']


@pytest.mark.ac('AC-11')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac11_user_rulings(a11, row):
    if row == 2:
        for env in [{'YGG_NODE': 'pm1'}, {'YGG_ACTIVATION': 'act-0001'},
                    {'YGG_NODE': 'user', 'YGG_ACTIVATION': 'act-0001'}]:
            result = a11.record_ruling(env=env)
            assert result.returncode == 3
            assert 'user-only' in result.stderr
        assert 'r-0001' not in a11.run('status').stdout
    else:
        result = a11.record_ruling()
        if row == 1:
            assert result.returncode == 0
            assert result.stdout.strip() == 'recorded r-0001'
            assert 'Ruling r-0001: commit with plain git on the lane' in a11.run('status').stdout
        else:
            a11.empty_state()
            a11.run('rehydrate')
            assert a11.run('status').stdout.count('r-0001') == 1


@pytest.mark.ac('AC-12')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac12_review_dispatch(a11, row):
    if row == 3:
        a11.env['YGG_CODEX_BIN'] = str(a11.root / 'missing-codex')
    a11.brief_seed()
    a11.authorised()
    topology = a11.node('pm1rv1')
    assert topology is not None
    assert (topology['role'], topology['parent'], topology['project']) == ('reviewer', 'pm1', 'alpha-p1')
    a11.agent('pm1rv1', binary='codex', mode='persist')
    a11.agent('pm1rv1', mode='persist')
    head = a11.ref('HEAD', a11.lane())
    result = a11.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
    a11.tick()
    a11.wait_launch('pm1rv1')
    if row == 2:
        result = a11.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
        a11.tick(2)
        assert result.stdout.strip() == 'review rv-0001 of WP-1 is already open'
    elif row == 1:
        assert result.returncode == 0
        assert result.stdout.strip() == 'review rv-0001 of WP-1 at ' + head[:7]
        assert present(a11.wp())['state'] == 'in-review'
        prompt = a11.prompt('pm1rv1')
        assert 'rv-0001' in prompt and head in prompt and 'WP-1' in prompt
        assert 'Brief marker zebra-5813: ship saves' in prompt
        base = a11.trusted_git(a11.repo_a, 'merge-base', head, 'ygg/alpha-p1/_integration').stdout.strip()
        assert base in prompt
    else:
        assert 'review: same family' in a11.line('WP-1')
    launches = a11.launches('pm1rv1')
    assert len(launches) == 1
    assert launches[0]['binary'] == ('claude' if row == 3 else 'codex')
    assert ('claude-opus-5-5' if row == 3 else 'gpt-6.1-sol') in ' '.join(launches[0]['argv'])


@pytest.mark.ac('AC-13')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac13_verdict_routes(a11, row):
    if row == 3:
        a11.cap()
        before = len(a11.launches('pm1rv1'))
        a11.tick(2)
        pending = a11.pending()
        assert pending is not None
        text = json.dumps(pending)
        decisions = a11.cap_decisions()
        assert len(decisions) == 1
        assert [option['label'] for option in decisions[0]['options']] == ['accept as is', 'one more round', 'stop']
        assert a11.work_owed('pm1tl1', 'WP-1') is False
        assert len(a11.launches('pm1rv1')) == before
        assert present(a11.wp())['held_reason'] == 'decision'
    else:
        a11.open_review()
        head = a11.ref('HEAD', a11.lane())
        base = a11.trusted_git(a11.repo_a, 'merge-base', head, 'ygg/alpha-p1/_integration').stdout.strip()
        if row == 2:
            before = len(a11.launches('pm1su1'))
        a11.verdict(verdict='revise' if row == 1 else 'approve', findings=[{
            'file': 'scripts/a.py', 'line': 12, 'text': 'unsafe report path'}] if row == 1 else None)
        assert present(a11.wp())['state'] == ('authorised' if row == 1 else 'ready-for-test')
        if row == 1:
            inbox = a11.inbox('pm1tl1').stdout
            assert len(a11.pending_messages('pm1tl1', 'review')) == 1
            assert 'review' in inbox and 'unsafe report path' in inbox
            assert 'review round 1, revise' in a11.line('WP-1')
        else:
            assert len(a11.launches('pm1su1')) - before == 1
            assert a11.ref('HEAD', a11.lane('pm1su1')) == head
            result = a11.run('acceptance', 'record', '--from', 'pm1su1', '--package', 'WP-1',
                             '--sha', base, '--verdict', 'accept')
            assert result.returncode == 3
            assert 'acceptance must name the reviewed head' in result.stderr


@pytest.mark.ac('AC-14')
@pytest.mark.release('v1.0-should')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac14_stage_close(a11, row):
    a11.accepted('WP-1')
    a11.config(stage_close='default')
    if row == 1:
        before = len(a11.launches('pm1'))
        a11.tick(2)
        assert 'stage 1 ready to close' in a11.line('alpha-p1')
        assert len(a11.launches('pm1')) - before == 1
        alerts = a11.alerts('ygg-stage-ready')
        assert len(alerts) == 1 and alerts[0]['class'] == 'log'
    else:
        result = a11.run('stage', 'end', 'alpha-p1')
        if row == 3:
            result = a11.run('stage', 'end', 'alpha-p1')
            assert result.returncode == 3
            assert 'stage 1 is already awaiting approval' in result.stderr
        else:
            assert result.returncode == 0
            assert present(a11.project())['state'] == 'awaiting-approval'
            before = len(a11.launches('pm1'))
            a11.tick()
            assert len(a11.launches('pm1')) == before
            assert len(a11.alerts('ygg-stage-approval', user=True)) == 1


@pytest.mark.ac('AC-22')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac22_confirm_windows(a11, row):
    a11.delayed_given(delay=15 if row == 1 else 120, load='1.5')
    if row == 1:
        a11.wait_launch('pm1tl1', timeout=20)
        a11.tick()
        assert present(a11.current())['outcome'] == 'confirmed'
        assert len(a11.launches('pm1tl1')) == 1
        assert a11.alerts('ygg-uncertain') == []
    elif row == 2:
        a11.later(89)
        a11.tick()
        assert 'starting' in a11.line()
        assert a11.alerts('ygg-uncertain') == []
        a11.later(91)
        a11.tick(3)
        assert a11.activation() in present(a11.status())['uncertain_keys']
        alerts = a11.alerts('ygg-uncertain')
        assert len(alerts) == 1 and alerts[0]['class'] == 'log'
        assert a11.launches('pm1tl1') == []
    else:
        a11.set_load('0.1')
        a11.later(61)
        a11.tick()
        assert 'starting' in a11.line()


@pytest.mark.ac('AC-29')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac29_ack_work_consumption(a11, row):
    if row == 1:
        a11.ready()
        a11.send(10)
        a11.agent(mode='persist', auto_ack=False)
        a11.tick()
        result = a11.call('git commit -m x')
        assert result.returncode == 0
        assert a11.notice(result) is None
    else:
        a11.unread(1)
        a11.ack(11)
        assert 'm-0011' in a11.receipt_text()
        before = len(a11.launches('pm1tl1'))
        if row == 2:
            a11.exit_agent('pm1tl1')
        else:
            a11.signal_agent()
        a11.wait_exit('pm1tl1')
        a11.tick(2)
        assert len(a11.launches('pm1tl1')) - before == (0 if row == 2 else 1)
        if row == 3:
            assert 'm-0011' in a11.receipt_text()


@pytest.mark.ac('AC-37')
@pytest.mark.release('v1.0-blocker')
def test_ac37_row1_pilot_replay(a11):
    result = a11.pilot()
    assert result is not None
    assert result['project']['state'] == 'closed'
    assert result['packages'] == {'WP-1': 'merged', 'WP-2': 'merged'}
    assert result['main_parents'][1] == result['integration']
    assert result['reviewer_launches'] == 3
    assert all(author == 'Example Builder' for author in result['authors'])
    assert 'Co-Authored-By' not in result['messages']
    assert result['user_alerts'] == {'ygg-pid-ready': 1, 'ygg-stage-approval': 2,
                                    'ygg-merge-ready': 1, 'ygg-project-closed': 1}
    assert result['unwanted_alerts'] == []
    assert result['delivered_before_merge'] is True
    assert result['home_hook_runs'] == 1
    assert result['user_commands_only_answers_and_main_merge'] is True


@pytest.mark.ac('AC-37')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', [1, 2])
def test_ac37_row2_last_stage_merge_ready(a11, run):
    a11.merge_given(last=run == 2)
    a11.tick(2)
    alerts = a11.alerts('ygg-merge-ready')
    if run == 1:
        assert alerts == []
        assert a11.merge_check_started() is False
    else:
        assert len(alerts) == 1
        assert alerts[0]['class'] == 'user'
        assert alerts[0]['text'] == 'alpha-p1 passed its checks and is ready to go into main. Tell your main session "merge this" when you\'re ready.'


@pytest.mark.ac('AC-37')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', [1, 2])
def test_ac37_row3_closes_only_on_main(a11, run):
    a11.merge_given(unmerged=run == 2)
    if run == 2:
        assert present(a11.project())['state'] == 'executing'
        assert 'all stages approved; waiting for every package to be merged and checked' in a11.line('alpha-p1')
        assert a11.merge_check_started() is False
        assert a11.alerts('ygg-merge-ready') == []
    else:
        assert present(a11.project())['state'] == 'ready-to-merge'
        assert 'ready to merge, not yet in main' in a11.line('alpha-p1')
        assert a11.alerts('ygg-project-closed') == []
        result = a11.run('merge', 'main', 'alpha-p1')
        assert result.returncode == 0
        assert merge_parents(a11.parents('main'))[1] == a11.ref('ygg/alpha-p1/_integration')
        a11.tick()
        assert present(a11.project())['state'] == 'closed'
        assert len(a11.alerts('ygg-project-closed')) == 1


@pytest.mark.ac('AC-39')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', ['post', 'ceiling'])
def test_ac39_row1_busy_end(a11, run):
    a11.busy()
    if run == 'post':
        a11.post(tool_id='tu-7')
        a11.tick()
        assert a11.state_text('heartbeats/pm1tl1').strip() == T
        assert 'running' in a11.line() and 'busy' not in a11.line()
    else:
        a11.later(3540)
        a11.tick()
        assert present(a11.node())['state'] == 'stalled-suspect'
        assert len(a11.alerts('ygg-stall')) == 1


@pytest.mark.ac('AC-39')
@pytest.mark.release('v1.0-blocker')
def test_ac39_row2_child_is_not_activity(a11):
    a11.later(-120)
    a11.config(max_silence_minutes=1)
    a11.start(child=True)
    a11.call('ls', tool_id='tu-7')
    a11.post('ls', tool_id='tu-7')
    a11.at(T)
    a11.tick()
    assert present(a11.node())['state'] == 'stalled-suspect'
    assert len(a11.alerts('ygg-stall')) == 1
    assert not any(r.get('event') in {'interrupt', 'child-signal'} for r in a11.calls())
    a11.tick()
    assert len(a11.alerts('ygg-stall')) == 1


@pytest.mark.ac('AC-39')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('age', [30, 180])
def test_ac39_row3_codex_output_heartbeat(a11, age):
    a11.config(max_silence_minutes=1)
    a11.later(-180)
    a11.open_review()
    a11.at(T)
    a11.stamp_output(a11.activation('pm1rv1'), age)
    a11.tick()
    assert (present(a11.node('pm1rv1'))['state'] == 'stalled-suspect') == (age == 180)
    assert len(a11.alerts('ygg-stall')) == (1 if age == 180 else 0)


@pytest.mark.ac('AC-40')
@pytest.mark.release('v1.0-blocker')
def test_ac40_row1_starting(a11):
    with a11.delayed_given(delay=20, background=True):
        a11.later(2)
        line = a11.line()
    assert 'starting' in line
    assert 'uncertain' not in line


@pytest.mark.ac('AC-40')
@pytest.mark.release('v1.0-blocker')
def test_ac40_row2_matching_post_clears_busy(a11):
    a11.busy()
    a11.tick()
    assert 'busy (running make)' in a11.line()
    a11.post(tool_id='tu-8')
    a11.tick()
    assert 'busy (running make)' in a11.line()
    a11.post(tool_id='tu-7', exit=2)
    a11.tick()
    assert a11.state_text('heartbeats/pm1tl1').strip() == T
    assert 'running' in a11.line()
    assert 'busy' not in a11.line() and 'stalled-suspect' not in a11.line()


@pytest.mark.ac('AC-40')
@pytest.mark.release('v1.0-blocker')
def test_ac40_row3_refused_call_refreshes_heartbeat(a11):
    a11.config(max_silence_minutes=1)
    a11.later(-120)
    a11.unread(1)
    a11.call('git commit -m x')
    assert a11.state_text('heartbeats/pm1tl1').strip() == '2026-11-19T11:58:00Z'
    a11.at(T)
    a11.tick()
    assert 'busy' not in a11.line()


@pytest.mark.ac('AC-55')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac55_topology_and_urgent_authority(a11, row):
    sender, target = {1: ('pm1tl2', 'pm2tl1'), 2: ('pm1tl1', 'pm1su1'), 3: ('pm1', 'pm1tl1')}[row]
    result = a11.run('send', '--from', sender, '--to', target, '--kind', 'work', '--body', 'x',
                     *(['--urgent'] if row > 1 else []), env={'YGG_NODE': sender})
    assert result.returncode == (0 if row == 3 else 3)
    if row < 3:
        assert ('topology' if row == 1 else 'only the user or the PM sends urgent messages') in result.stderr
    else:
        assert 'urgent' in a11.inbox('pm1tl1').stdout


@pytest.mark.ac('AC-56')
@pytest.mark.release('v1.0-should')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac56_reviewer_fallback(a11, row):
    if row == 1:
        a11.open_review(model='claude-opus-5-5')
        launches = a11.launches('pm1rv1')
        assert len(launches) == 1 and launches[0]['binary'] == 'claude'
        assert 'claude-opus-5-5' in launches[0]['argv']
        assert 'review: same family' in a11.line('WP-1')
    else:
        a11.authorised()
        a11.agent('pm1rv1', binary='codex', mode='capacity' if row == 3 else 'exit0',
                   exit_code=1, stderr='HTTP 429 Too Many Requests' if row == 3 else 'Not logged in')
        a11.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
        a11.tick(2 if row == 2 else 1)
        if row == 2:
            assert [r['binary'] for r in a11.launches('pm1rv1')] == ['codex', 'claude']
            assert "review: same family (the hub's Codex pool is unavailable)" in a11.line('WP-1')
            assert len(a11.alerts('ygg-review-fallback', user=True)) == 1
        else:
            assert present(a11.node('pm1rv1'))['state'] == 'held-capacity'
            assert all(r['binary'] != 'claude' for r in a11.launches('pm1rv1'))
            before = len(a11.launches('pm1rv1'))
            a11.later(299)
            a11.tick()
            assert len(a11.launches('pm1rv1')) == before
            a11.later(301)
            a11.tick()
            assert len(a11.launches('pm1rv1')) == before + 1


@pytest.mark.ac('AC-57')
@pytest.mark.release('v1.0-blocker')
def test_ac57_row1_separate_reviews(a11):
    a11.authorised('WP-1')
    a11.authorised('WP-2', lane='pm1tl2')
    for package in ['WP-1', 'WP-2']:
        a11.run('wp', 'ready', '--from', 'pm1tl1' if package == 'WP-1' else 'pm1tl2', '--package', package)
    a11.agent('pm1rv1', binary='codex', mode='persist')
    a11.tick()
    a11.verdict_between_reviews('rv-0001')
    assert present(a11.wp('WP-1'))['state'] == 'ready-for-test'
    assert present(a11.wp('WP-2'))['state'] == 'in-review'
    a11.tick()
    a11.verdict('rv-0002')
    assert present(a11.wp('WP-2'))['state'] == 'ready-for-test'
    assert len(a11.launches('pm1rv1')) == 2


@pytest.mark.ac('AC-57')
@pytest.mark.release('v1.0-blocker')
def test_ac57_row2_no_verdict_hold(a11):
    a11.held_review()
    assert 'WP-1 review held: the reviewer gave no verdict' in a11.run('status').stdout
    assert len(a11.alerts('ygg-review-held')) == 1
    assert len(a11.launches('pm1rv1')) == 2
    a11.tick(2)
    assert len(a11.launches('pm1rv1')) == 2


@pytest.mark.ac('AC-57')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('verb', ['retry', 'drop'])
def test_ac57_row3_user_retry_drop(a11, verb):
    a11.held_review()
    result = a11.run('review', verb, 'rv-0001', env={'YGG_NODE': 'pm1'})
    assert result.returncode == 3 and 'user-only' in result.stderr
    before = len(a11.launches('pm1rv1'))
    a11.run('review', verb, 'rv-0001')
    a11.tick()
    if verb == 'retry':
        assert len(a11.launches('pm1rv1')) == before + 1
    else:
        assert present(a11.wp())['state'] == 'authorised'
        assert 'review dropped by the user' in a11.inbox('pm1tl1').stdout


@pytest.mark.ac('AC-58')
@pytest.mark.release('v1.0-blocker')
def test_ac58_row1_document_preview(a11):
    a11.quiesce_core_pms()
    create = a11.docs()
    assert create.returncode == 0
    assert 'Document folders are a preview in this release: packages get no per-package review yet.' in create.stdout
    assert not any(n.get('project') == 'docs-p1' and n['role'] == 'reviewer' for n in present(a11.status())['nodes'])
    result = a11.run('wp', 'ready', '--from', 'pm5tl1', '--package', 'WP-1')
    assert result.returncode == 3
    assert 'review of document folders is not built yet; use ygg wp complete' in result.stderr
    result = a11.run('wp', 'complete', '--from', 'pm5tl1', '--package', 'WP-1')
    assert result.returncode == 0
    assert present(a11.wp())['state'] == 'ready-for-test'
    a11.tick()
    assert len(a11.launches('pm5su1')) == 1
    assert a11.launches('pm5rv1') == []


@pytest.mark.ac('AC-58')
@pytest.mark.release('v1.0-blocker')
def test_ac58_row2_in_review_counts_as_wip(a11):
    a11.ready(wip_limit=1)
    a11.authorised()
    a11.package('WP-3', 'pm1tl2', criteria=True)
    a11.run('wp', 'complete', '--from', 'pm1tl1', '--package', 'WP-1')
    assert present(a11.wp())['state'] == 'in-review'
    result = a11.run('wp', 'authorise', 'alpha-p1', '--package', 'WP-3', '--lane', 'pm1tl2')
    assert result.returncode == 3
    assert 'WIP' in result.stderr


@pytest.mark.ac('AC-58')
@pytest.mark.release('v1.0-blocker')
def test_ac58_row3_cancel_stopped_package(a11):
    a11.write(a11.repo_a / 'scripts/a.py', 'x = 1\n')
    a11.trusted_git(a11.repo_a, 'add', 'scripts/a.py')
    a11.clean_trusted_commit(a11.repo_a)
    a11.ready(stages=[{'number': 1, 'name': 'Foundations',
                       'planned_end': '2026-11-19T17:00:00Z'}])
    s2 = a11.accepted('WP-2', file='scripts/c.py')
    a11.authorised('WP-1', build=False)
    a11.commit(file='scripts/a.py', text='x = 2\n', message='C1')
    c1 = a11.ref('HEAD', a11.lane())
    for number in range(2, 7):
        a11.remove(a11.root / 'fake-control/pm1rv1/exit')
        a11.agent('pm1rv1', binary='codex', mode='persist')
        a11.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
        a11.tick()
        a11.wait_launch('pm1rv1')
        a11.verdict(f'rv-{number:04}', 'revise')
    a11.answer_cap('stop')
    result = a11.run('wp', 'cancel', 'alpha-p1', '--package', 'WP-1', env={'YGG_NODE': 'pm1'})
    assert result.returncode == 0
    assert '1 commit dropped' in result.stdout and c1[:7] in result.stdout
    assert present(a11.wp())['state'] == 'cancelled'
    assert 'WP-1' not in a11.inbox('pm1tl1').stdout
    assert a11.ref('ygg/alpha-p1/lane/pm1tl1') == s2
    assert a11.trusted_git(a11.lane(), 'status', '--porcelain').stdout == ''
    assert a11.run('merge', 'lane', 'alpha-p1', '--lane', 'pm1tl1', '--repo', a11.repo_a,
                   env={'YGG_NODE': 'pm1'}).returncode == 0
    a11.home_hook(body='git diff --cached --name-only >> ' +
                  shlex.quote(str(a11.root / 'home-hook-ran')) + '\nexit 0\n')
    a11.close_stage()
    a11.run('stage', 'approve', 'alpha-p1')
    a11.check_finished()
    assert 'merge check: passed' in a11.line('alpha-p1')
    assert a11.run('merge', 'main', 'alpha-p1').returncode == 0
    a11.tick()
    assert present(a11.project())['state'] == 'closed'
    for branch in ['ygg/alpha-p1/_integration', 'main']:
        assert a11.trusted_git(a11.repo_a, 'merge-base', '--is-ancestor', c1, branch).returncode == 1
    assert a11.trusted_git(a11.repo_a, 'show', 'main:scripts/a.py').stdout.splitlines()[:1] == ['x = 1']


@pytest.mark.ac('AC-59')
@pytest.mark.release('v1.0-blocker')
def test_ac59_row1_wrong_review_is_no_verdict(a11):
    a11.open_review()
    a11.verdict('rv-0009')
    assert present(a11.wp())['state'] == 'in-review'
    assert len(a11.launches('pm1rv1')) == 2


@pytest.mark.ac('AC-59')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('change,reason', [('lane', '(the lane moved)'), ('criteria', '(the criteria changed)'),
                                         ('ruling', '(a ruling changed)')])
def test_ac59_row2_stale_review_rechecked(a11, change, reason):
    a11.open_review()
    if change == 'lane':
        a11.clean_trusted_commit()
    elif change == 'criteria':
        a11.replace_criteria()
    else:
        a11.record_ruling()
    head = a11.ref('HEAD', a11.lane())
    before = len(a11.launches('pm1rv1'))
    a11.verdict()
    assert present(a11.wp())['state'] == 'authorised'
    text = a11.inbox('pm1tl1').stdout
    assert len(a11.pending_messages('pm1tl1', 'review')) == 1
    assert 'review rv-0001 is stale' in text and reason in text
    assert len(a11.launches('pm1rv1')) == before
    a11.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
    a11.tick()
    assert len(a11.launches('pm1rv1')) == before + 1
    assert 'rv-0002' in a11.prompt('pm1rv1') and head in a11.prompt('pm1rv1')


@pytest.mark.ac('AC-59')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', [1, 2])
def test_ac59_row3_late_verdict_and_pair_records(a11, run):
    a11.open_review()
    if run == 1:
        a11.verdict()
        a11.late_verdict()
        a11.tick()
        assert present(a11.wp())['state'] == 'ready-for-test'
    else:
        for verdict in ['pass', *['changes'] * 5]:
            result = a11.run('review', 'record', '--from', 'pm1tl1', '--package', 'WP-1',
                             '--round', '1', '--verdict', verdict)
            assert result.returncode == 3
            assert 'WP-1 is being checked by rv-0001' in result.stderr
        assert present(a11.wp())['state'] == 'in-review'
        assert a11.cap_decisions() == []
        assert a11.review_revises() == 0


@pytest.mark.ac('AC-60')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row,answer', [(1, 'accept as is'), (2, 'one more round'), (3, 'stop')])
def test_ac60_cap_answers(a11, row, answer):
    a11.cap()
    if row == 1:
        senior_before = len(a11.launches('pm1su1'))
    a11.answer_cap(answer)
    before = len(a11.launches('pm1rv1'))
    a11.tick(2 if row == 3 else 1)
    assert present(a11.wp())['state'] == {1: 'ready-for-test', 2: 'authorised', 3: 'stopped'}[row]
    if row == 1:
        assert len(a11.launches('pm1su1')) - senior_before == 1
    elif row == 2:
        assert 'unsafe report path' in a11.inbox('pm1tl1').stdout
        a11.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
        a11.tick()
        assert len(a11.launches('pm1rv1')) == before + 1
        a11.verdict('rv-0006', 'revise', findings=[{'file': 'scripts/a.py', 'line': 12, 'text': 'unsafe report path'}])
        assert len(a11.cap_decisions()) == 1
    else:
        assert 'WP-1 stopped by the user; re-plan stage 1.' in a11.inbox().stdout
        assert a11.work_owed('pm1tl1', 'WP-1') is False
        assert a11.work_owed('pm1rv1', 'WP-1') is False


@pytest.mark.ac('AC-61')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac61_review_cap_rules(a11, row):
    if row != 2:
        a11.cap(2 if row == 3 else 5)
        if row == 1:
            result = a11.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
            assert result.returncode == 3
            assert "waits for the user's decision" in result.stderr
        else:
            assert len(a11.cap_decisions()) == 1
    else:
        a11.approve_review()
        before = a11.review_revises()
        head = a11.ref('HEAD', a11.lane())
        a11.run('acceptance', 'record', '--from', 'pm1su1', '--package', 'WP-1', '--sha', head, '--verdict', 'reject')
        a11.tick()
        assert present(a11.wp())['state'] == 'rejected'
        assert 'rejection:WP-1' in a11.receipt_text()
        assert a11.review_revises() == before
        a11.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
        assert 'review round 2' in a11.line('WP-1')


@pytest.mark.ac('AC-62')
@pytest.mark.release('v1.0-should')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac62_close_escalation_and_auto(a11, row):
    a11.accepted('WP-1')
    a11.config(stage_close='default' if row == 1 else 'auto')
    if row == 1:
        a11.agent('pm1', mode='exit0')
        a11.tick()
        a11.wait_exit('pm1')
        before = len(a11.launches('pm1'))
        a11.tick(2)
        assert len(a11.launches('pm1')) == before
        assert 'stage 1 ready to close' in a11.line('alpha-p1')
        assert len(a11.alerts('ygg-stage-ready', user=True)) == 1
    elif row == 2:
        a11.tick()
        assert present(a11.project())['stage']['state'] == 'awaiting-approval'
        assert len(a11.alerts('ygg-stage-approval')) == 1
    else:
        a11.readonly(a11.tracker)
        a11.tick()
        assert len(a11.alerts('ygg-stage-close-refused', user=True)) == 1
        assert 'stage 1 ready to close (close refused:' in a11.line('alpha-p1')


@pytest.mark.ac('AC-63')
@pytest.mark.release('v1.0-should')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac63_not_closable(a11, row):
    if row == 2:
        a11.stage_executing(2)
        a11.config(stage_close='auto')
        a11.tick(3)
        assert present(a11.project())['stage']['state'] == 'executing'
    else:
        a11.accepted('WP-1')
        if row == 1:
            a11.tick()
            a11.run('wp', 'reopen', 'alpha-p1', '--package', 'WP-1', env={'YGG_NODE': 'pm1'})
            assert 'ready to close' not in a11.line('alpha-p1')
        else:
            a11.config(stage_close='pm-only')
        result = a11.run('stage', 'end', 'alpha-p1')
        assert result.returncode == 3
        assert ("stage 1 has packages that aren't accepted" if row == 1 else 'stage close is PM-only for this project') in result.stderr


@pytest.mark.ac('AC-65')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac65_pm_reopen(a11, row):
    a11.accepted()
    if row == 3:
        a11.stage_executing(2)
        a11.accepted()
        a11.close_stage_number(2)
    result = a11.run('wp', 'reopen', 'alpha-p1', '--package', 'WP-2',
                     env={'YGG_NODE': 'pm1tl1' if row == 1 else 'pm1'})
    if row == 1:
        assert result.returncode == 3
        assert 'identity: only the PM of alpha-p1 reopens a package' in result.stderr
        assert present(a11.wp('WP-2'))['state'] == 'accepted'
    elif row == 2:
        assert result.returncode == 0
        assert present(a11.wp('WP-2'))['state'] == 'authorised'
        assert 'WP-2' in a11.inbox('pm1tl1').stdout
        before = len(a11.launches('pm1rv1'))
        a11.tick()
        assert len(a11.launches('pm1rv1')) == before
        assert a11.commit(file='scripts/a.py', text='b').returncode == 0
        result = a11.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-2')
        assert 'review rv-' in result.stdout
    else:
        assert present(a11.project())['stage']['state'] == 'executing'
        assert 'stage 2 reopened (WP-2)' in a11.run('status').stdout
        assert a11.run('stage', 'approve', 'alpha-p1').returncode == 3


@pytest.mark.ac('AC-77')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('row', [1, 2])
def test_ac77_window_bounds(a11, row):
    a11.delayed_given(delay=120, load='0.5' if row == 1 else '9.0', threshold=3.0 if row == 1 else 10.0)
    if row == 2:
        a11.later(299)
        a11.tick()
        assert 'starting' in a11.line()
    a11.later(61 if row == 1 else 301)
    a11.tick()
    assert a11.activation() in present(a11.status())['uncertain_keys']


@pytest.mark.ac('AC-80')
@pytest.mark.release('v1.0-should')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac80_needs_l1(a11, row):
    if row == 3:
        a11.config(alert_forwarder='auto', alert_t3_thread='thr-l1')
        a11.env['YGG_T3_BIN'] = str(a11.root / 'bin/t3')
    a11.later(-1740 if row == 2 else -1860)
    a11.ready()
    a11.send(10)
    a11.at(T)
    a11.tick()
    text = 'alpha-p1 has work waiting and nothing has moved for 30 minutes.'
    if row == 2:
        assert a11.alerts('ygg-needs-l1') == []
    else:
        alerts = a11.alerts('ygg-needs-l1', user=True)
        assert len(alerts) == 1 and alerts[0]['text'] == text
        if row == 1:
            a11.tick()
            assert len(a11.alerts('ygg-needs-l1')) == 1
        else:
            assert len([r for r in a11.calls('t3') if 'thr-l1' in r['argv'] and text in ' '.join(r['argv'])]) == 1


@pytest.mark.ac('AC-88')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2])
def test_ac88_unacked_and_recipient_only(a11, row):
    a11.unread(1)
    if row == 1:
        before = len(a11.launches('pm1tl1'))
        a11.exit_agent('pm1tl1')
        a11.wait_exit('pm1tl1')
        a11.tick()
        assert len(a11.launches('pm1tl1')) == before + 1
        assert 'm-0011' in a11.receipt_text()
    else:
        result = a11.ack(11, caller='pm1')
        assert result.returncode == 3
        assert 'identity: only pm1tl1 acknowledges its own messages' in result.stderr
        assert '1 unread message' in (a11.notice(a11.call('ls')) or '')


@pytest.mark.ac('AC-89')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac89_rulings_context_not_owed(a11, row):
    a11.ready()
    a11.quiesce()
    a11.clear_calls()
    if row > 1:
        a11.start()
    a11.record_ruling()
    if row == 1:
        a11.tick(2)
        assert a11.launches() == []
        a11.run('launch', 'pm1tl1')
        prompt = a11.prompt()
        assert '## User rulings' in prompt
        assert '```' in prompt and 'commit with plain git on the lane' in prompt
    elif row == 2:
        assert a11.notice(a11.call('ls')) == '1 unread message (1 urgent): run ygg inbox pm1tl1 now.'
        inbox = a11.inbox('pm1tl1').stdout
        assert 'ruling' in inbox and 'urgent' in inbox
        assert len(a11.pending_messages('pm1tl1', 'ruling')) == 1
        assert a11.pending_messages('pm1', 'ruling') == []
        assert a11.pending_messages('pm1su1', 'ruling') == []
        assert 'r-0001' not in a11.inbox().stdout
        assert 'r-0001' not in a11.inbox('pm1su1').stdout
    else:
        assert a11.notice(a11.call('ls')) == '1 unread message (1 urgent): run ygg inbox pm1tl1 now.'
        before = len(a11.launches('pm1tl1'))
        a11.exit_agent('pm1tl1')
        a11.wait_exit('pm1tl1')
        a11.tick(2)
        assert len(a11.launches('pm1tl1')) == before


@pytest.mark.ac('AC-90')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2])
def test_ac90_new_node_and_reviewer_rulings(a11, row):
    a11.record_ruling()
    if row == 1:
        a11.ready()
        a11.run('node', 'add', 'pm1tl1w3', '--role', 'worker', '--parent', 'pm1tl1')
        a11.run('launch', 'pm1tl1w3')
        assert a11.prompt('pm1tl1w3').count('commit with plain git on the lane') == 1
    else:
        a11.open_review()
        assert 'commit with plain git on the lane' in a11.prompt('pm1rv1')


@pytest.mark.ac('AC-54')
@pytest.mark.release('v1.0-should')
@pytest.mark.parametrize('row,target,body', [(1, 'pm1tl1', 'use the d6c1 script'), (2, 'pm2tl1', 'x')])
def test_ac54_user_short_send(a11, row, target, body):
    result = a11.run('send', '--to', target, '--body', body)
    assert result.returncode == 0
    text = a11.inbox(target).stdout
    messages = a11.pending_messages(target)
    assert len(messages) == 1 and messages[0]['from'] == 'user'
    assert 'user' in text
    if row == 1:
        assert 'work' in text and body in text
