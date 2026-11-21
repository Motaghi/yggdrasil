import json
import re

import pytest

from assertions import present, merge_parents

from driver_a11 import INTEGRATION, T
from test_lane1_hooks import FAULTS


NEXT_STEPS = {
    'ygg-launch-refused': 'Ask your main session to fix the reason shown; Yggdrasil then tries again by itself.',
    'ygg-pid-ready': 'Read the plan and approve it with your main session.',
    'ygg-decision-waiting': 'Answer the decision with your main session.',
    'ygg-stage-approval': 'Read the stage report and approve it with your main session.',
    'ygg-stage-ready': 'Ask your main session to end the stage.',
    'ygg-stage-close-refused': 'Ask your main session what blocked the close.',
    'ygg-merge-ready': 'Tell your main session "merge this" when you\'re ready.',
    'ygg-project-closed': 'Nothing to do; the work is finished.',
    'ygg-needs-l1': 'Open your main session so it can pick the project up again.',
    'ygg-uncertain': 'Ask your main session to check the agent before anything restarts.',
    'ygg-stall': 'Ask your main session to look at the stalled agent.',
    'ygg-stop-held': 'Ask your main session to check the agent, then release and resume it.',
    'ygg-merge-check-failed': 'Nothing to do yet; the team is fixing it.',
    'ygg-agreement-needed': 'Ask your main session to show you the agent rules, then agree.',
    'ygg-review-held': 'Ask your main session whether to retry that review or drop it.',
    'ygg-review-fallback': "Nothing to do now: the hub's Codex pool is unavailable, so this review ran on the same model family. Ask your main session to check the hub if it keeps happening.",
    'ygg-exception': 'Read the exception report and decide with your main session.',
    'ygg-blocker': 'Ask your main session what the team needs from you.',
    'ygg-protection-missing': 'Protect main, or ask your main session to set branch protection to warn.',
    'ygg-tracker-corrupt': 'Stop and ask your main session to repair the tracker.',
}


@pytest.mark.ac('AC-06')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac06_alert_classes(a11, row):
    if row == 1:
        a11.ready()
        a11.trusted_git(a11.repo_a, 'config', 'foo.bar', '1')
        a11.send(10)
        a11.tick()
        alerts = a11.alerts('ygg-launch-refused', user=True, node='pm1tl1')
        assert len(alerts) == 1 and alerts[0]['class'] == 'user'
        assert 'pm1tl1' in json.dumps(alerts[0])
    elif row == 2:
        a11.busy()
        a11.tick()
        assert a11.alerts('ygg-stall') == []
    else:
        a11.decision()
        a11.ready()
        a11.close_stage()
        a11.run('project', 'close', 'alpha-p2', '--premature', '--note', 'stop work')
        alerts = a11.alerts(user=True)
        for kind in ['ygg-decision-waiting', 'ygg-stage-approval', 'ygg-project-closed']:
            found = [r for r in alerts if r['kind'] == kind]
            assert len(found) == 1 and found[0]['class'] == 'user'


@pytest.mark.ac('AC-07')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac07_hourly_summary(a11, row):
    a11.summary_given(decisions={1: 0, 2: 2, 3: 1}[row])
    result = a11.run('summary', '--since', '1h')
    assert result.returncode == 0
    assert result.stdout.splitlines() == ['alpha-p1 in the last hour:',
        'Done: WP-1 Save module; WP-2 Load module.', 'Being checked: WP-3 Export.',
        {1: 'Nothing needs you.', 2: '2 decisions wait for you: run ygg pending.',
         3: '1 decision waits for you: run ygg pending.'}[row]]


@pytest.mark.ac('AC-08')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac08_dashboard(a11, row):
    summary = '\n'.join('summary ' + str(n) for n in range(1, 9 if row == 3 else 5))
    mapping = a11.dashboard_given(summary=None if row == 2 else summary, all_states=row == 1)
    a11.tick()
    text = a11.dashboard()
    if row == 1:
        structure = a11.dashboard_structure()
        assert structure is not None
        assert structure['columns'] == ['to-do', 'working', 'checking', 'done', 'needs-you']
        assert structure['packages'] == mapping
        for name in ['To do', 'Working on it', 'Being checked', 'Done', 'Needs you',
                     'pm1tl1', 'team lead', 'WP-2', 'running pytest', *summary.splitlines()]:
            assert name in text
        assert '<script' not in text and '<form' not in text
    elif row == 2:
        assert 'No summary yet.' in text
    else:
        for line in summary.splitlines()[:5]:
            assert line in text
        assert '(shortened)' in text
        for line in summary.splitlines()[5:]:
            assert line not in text


@pytest.mark.ac('AC-09')
@pytest.mark.release('fallback-v1.1')
def test_ac09_row1_serves_file(a11):
    line = a11.server()
    assert re.fullmatch(r'Serving http://127\.0\.0\.1:\d+/ \(read-only\)\. Press Ctrl-C to stop\.', line)
    assert a11.http() == (200, a11.dashboard())


@pytest.mark.ac('AC-09')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE'])
def test_ac09_row2_read_only(a11, method):
    a11.server()
    before = a11.state_and_tracker_snapshot()
    response = a11.http(method=method)
    assert response is not None and response[0] == 405
    assert a11.state_and_tracker_snapshot() == before


@pytest.mark.ac('AC-09')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('path', ['/x', '/../alerts.jsonl', '/%2e%2e/state', '/dashboard/beta.html'])
def test_ac09_row3_paths_refused(a11, path):
    a11.server()
    assert a11.http(path) == (404, 'Not found')


@pytest.mark.ac('AC-24')
@pytest.mark.release('fallback-v1.1')
def test_ac24_row1_doctor_options(a11):
    a11.config(tracker='files', launcher='headless')
    a11.remove(a11.root / 'bin/t3')
    result, data = a11.run_json('doctor', '--json')
    assert result.returncode == 0 and data is not None
    assert data['tracker']['active'] == 'files'
    assert {'name': 'files', 'ready': True} in data['tracker']['options']
    assert {'name': 'linear', 'ready': False, 'reason': 'no Linear token'} in data['tracker']['options']
    assert data['launcher']['active'] == 'headless'
    assert {'name': 'headless', 'ready': True} in data['launcher']['options']
    assert {'name': 't3', 'ready': False, 'reason': 't3 command not found'} in data['launcher']['options']


@pytest.mark.ac('AC-24')
@pytest.mark.release('fallback-v1.1')
def test_ac24_row2_doctor_no_denylist(a11):
    a11.profile(leak_denylist=None)
    assert 'Leak check: no denylist set; lane commits are checked for secret-shaped tokens only.' in a11.run('doctor').stdout


@pytest.mark.ac('AC-27')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac27_home_merge_check(a11, row):
    a11.merge_given(check=False, hook='pass' if row == 1 else 'fail-once' if row == 2 else 'main-only')
    before = a11.ref('main')
    branches = a11.trusted_git(a11.repo_a, 'branch', '--list').stdout
    a11.check_finished()
    if row == 1:
        assert 'module1.txt' in a11.read(a11.root / 'home-hook-ran')
        assert 'merge check: passed (' + a11.ref(INTEGRATION)[:7] + ')' in a11.line('alpha-p1')
        result = a11.run('merge', 'main', 'alpha-p1', '--print')
        assert result.returncode == 0
        assert 'git -C ' + str(a11.repo_a) + ' merge --no-ff ygg/alpha-p1/_integration' in result.stdout
        assert a11.ref('main') == before
    else:
        assert 'merge check: failed' in a11.line('alpha-p1')
        if row == 3:
            assert a11.trusted_git(a11.repo_a, 'branch', '--list').stdout == branches
        else:
            assert '6 tests failed' in a11.inbox().stdout
            alerts = a11.alerts('ygg-merge-check-failed', user=True)
            assert len(alerts) == 1
            assert alerts[0]['text'] == "The project's own checks failed before the merge into main. The team is fixing it."
            result = a11.run('merge', 'main', 'alpha-p1', '--repo', a11.repo_a)
            assert result.returncode == 3 and 'the merge check failed; the PM is fixing it' in result.stderr
            assert a11.ref('main') == before
            a11.run('wp', 'reopen', 'alpha-p1', '--package', 'WP-1', env={'YGG_NODE': 'pm1'})
            assert present(a11.wp())['state'] == 'authorised'
            assert present(a11.project())['state'] == 'executing'
            assert present(a11.project())['stage']['state'] == 'approved'
            a11.redo_package()
            a11.check_finished()
            assert present(a11.project())['state'] == 'ready-to-merge'
            assert len(a11.alerts('ygg-merge-ready')) == 1


@pytest.mark.ac('AC-28')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac28_alert_delivery(a11, row):
    a11.config(alert_forwarder='auto')
    if row == 1:
        a11.auto_alerts(headless=True)
    if row == 3:
        a11.profile(alert_command=[str(a11.root / 'bin/phone-push')])
    a11.raise_alert('ygg-launch-refused', node='pm1tl1')
    a11.tick()
    binary = {1: 't3', 2: 'osascript', 3: 'phone-push'}[row]
    calls = a11.calls(binary)
    assert len(calls) == 1
    if row == 1:
        assert 'thr-l1' in calls[0]['argv']
        assert 'Launch of pm1tl1 refused' in ' '.join(calls[0]['argv'])
        assert a11.calls('osascript') == []
    elif row == 2:
        assert 'Launch of pm1tl1 refused' in ' '.join(calls[0]['argv'])
    else:
        assert json.loads(calls[0]['stdin'])['kind'] == 'ygg-launch-refused'


@pytest.mark.ac('AC-43')
@pytest.mark.release('v1.0-blocker')
def test_ac43_row1_log_alert(a11):
    a11.raise_alert('ygg-corrupt-evidence')
    assert a11.alerts('ygg-corrupt-evidence', user=True) == []
    alerts = a11.alerts('ygg-corrupt-evidence')
    assert len(alerts) == 1 and alerts[0]['class'] == 'log'


@pytest.mark.ac('AC-43')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('kind', list(NEXT_STEPS))
def test_ac43_row2_every_alert_kind(a11, kind):
    variants = ([({'escalated': False}, 'log'), ({'escalated': True}, 'user')] if kind == 'ygg-stage-ready' else
                [({'age_seconds': 20}, 'log'), ({'age_seconds': 301}, 'user')] if kind == 'ygg-uncertain' else [({}, 'user')])
    for fields, _ in variants:
        a11.raise_alert(kind, **fields)
    alerts = a11.alerts(kind)
    assert len(alerts) == len(variants)
    for alert, (_, expected) in zip(alerts, variants):
        assert alert['class'] == expected
        if expected == 'user':
            assert alert['next_step'] == NEXT_STEPS[kind]


@pytest.mark.ac('AC-43')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('off', [False, True])
def test_ac43_row3_uncertain_escalates_after_300(a11, off):
    if off:
        a11.release_off('confirm_window')
    a11.delayed_given(delay=120, quiesce_pms=True)
    a11.later(62)
    a11.tick()
    alerts = a11.alerts('ygg-uncertain')
    assert len(alerts) == 1 and alerts[0]['class'] == 'log'
    a11.later(301)
    a11.tick()
    assert [r['class'] for r in a11.alerts('ygg-uncertain')] == ['log', 'user']
    assert a11.launches('pm1tl1') == []


@pytest.mark.ac('AC-44')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('row', [1, 2])
def test_ac44_summary_empty_and_title(a11, row):
    title = 'A' * 39 + '\n' + 'B' * 40
    if row == 1:
        a11.later(3601)
    else:
        a11.summary_given(title=title)
    result = a11.run('summary', '--since', '1h')
    assert result.returncode == 0
    if row == 1:
        assert result.stdout.splitlines() == ['No change in the last hour.']
    else:
        assert title.replace('\n', ' ')[:60] + '...' in result.stdout


@pytest.mark.ac('AC-45')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('row', [1, 2])
def test_ac45_dashboard_safe_text(a11, row):
    token = 'gho_' + 'A' * 36
    title = token if row == 1 else '<script>x</script><a href="https://e.example">'
    a11.package_state('WP-1', 'planned', title=title)
    a11.tick()
    text = a11.dashboard()
    if row == 1:
        assert token not in text and token[:8] not in text
        a11.package_state('WP-2', 'planned', title='<script>x</script><a href="https://e.example">')
        a11.tick()
        assert '&lt;script&gt;x&lt;/script&gt;&lt;a href=&quot;https://e.example&quot;&gt;' in a11.dashboard()
    else:
        assert '&lt;script&gt;x&lt;/script&gt;&lt;a href=&quot;https://e.example&quot;&gt;' in text
        assert '<script' not in text
        assert not re.search(r'(?:src|href)\s*=\s*[\"\']?(?:https?:|//)', text)
        assert 'url(' not in text


@pytest.mark.ac('AC-46')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac46_dashboard_updates(a11, row):
    if row == 1:
        a11.record_ruling()
    elif row == 2:
        a11.package_state('WP-3', 'in-review')
        a11.tick()
        a11.package_state('WP-3', 'accepted')
    else:
        a11.decision(number=3)
    a11.tick()
    if row == 1:
        text = a11.dashboard()
        assert 'Rulings' in text and 'Ruling r-0001: commit with plain git on the lane' in text
    elif row == 2:
        assert a11.dashboard_structure() is not None
        assert a11.dashboard_structure()['packages']['WP-3'] == 'done'
    else:
        assert 'Ask your main session about D-0003.' in a11.dashboard()


@pytest.mark.ac('AC-47')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac47_dashboard_freshness(a11, row):
    if row == 1:
        a11.at('2026-11-18T13:05:00Z')
        a11.profile(timezone='Europe/Copenhagen')
    elif row == 2:
        a11.run('pause')
    a11.tick()
    if row == 3:
        a11.later(46)
        a11.run('dashboard', 'alpha', '--no-open')
    text = a11.dashboard()
    if row == 1:
        assert 'Updated 2026-11-18 14:05' in text
    elif row == 2:
        assert text.splitlines()[:1] == ['Paused: everything.']
    else:
        assert "Yggdrasil isn't running. This page may be out of date." in text


@pytest.mark.ac('AC-48')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('row', [1, 2])
def test_ac48_open_dashboard(a11, row):
    result = a11.run('dashboard', 'alpha', *(['--no-open'] if row == 2 else []))
    assert result.returncode == 0
    path = str(a11.state / 'dashboard/alpha.html')
    assert result.stdout.strip() == 'Dashboard: ' + path
    calls = a11.calls('open')
    assert len(calls) == (1 if row == 1 else 0)
    if row == 1:
        assert calls[0]['argv'] == [path]


@pytest.mark.ac('AC-49')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('row', [1, 2])
def test_ac49_allowed_host(a11, row):
    if row == 2:
        a11.config(dashboard_allowed_hosts=['box.example.ts.net'])
    a11.server()
    result = a11.http(host='evil.example' if row == 1 else 'box.example.ts.net')
    if row == 1:
        assert result == (403, 'Forbidden')
    else:
        assert result is not None and result[0] == 200


@pytest.mark.ac('AC-50')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('row', [1, 2])
def test_ac50_loopback_user_only(a11, row):
    if row == 1:
        a11.server()
        assert a11.external_connect() is False
    else:
        result = a11.run('dashboard', 'serve', 'alpha', '--port', '0', env={'YGG_NODE': 'pm1tl1'})
        assert result.returncode == 3 and 'user-only' in result.stderr


@pytest.mark.ac('AC-83')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', ['never', 'main-moved', 'integration-moved'])
def test_ac83_row1_pass_bound_to_heads(a11, run):
    a11.merge_given(check=run != 'never')
    if run == 'main-moved':
        a11.clean_trusted_commit(a11.repo_a)
    elif run == 'integration-moved':
        a11.move_integration()
    result = a11.run('merge', 'main', 'alpha-p1', '--repo', a11.repo_a)
    assert result.returncode == 3
    assert 'run the merge check first: ygg merge check alpha-p1 --repo ' + str(a11.repo_a) in result.stderr
    a11.tick()
    assert a11.merge_check_started() is True


@pytest.mark.ac('AC-83')
@pytest.mark.release('v1.0-blocker')
def test_ac83_row2_background_check(a11):
    a11.merge_given(check=False, hook='slow')
    a11.tick()
    for action in [lambda: a11.tick(), lambda: a11.run('send', '--from', 'pm1', '--to', 'pm1tl1', '--kind', 'work', '--body', 'x', env={'YGG_NODE': 'pm1'}),
                   lambda: a11.tick(), lambda: a11.tick()]:
        _, elapsed = a11.elapsed(action)
        assert elapsed <= 5
    a11.check_finished(timeout=35)
    assert a11.read(a11.root / 'home-hook-ran') == 'x'


@pytest.mark.ac('AC-83')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', ['timeout', 'changed-files'])
def test_ac83_row3_failed_check(a11, run):
    if run == 'timeout':
        a11.config(merge_check_timeout_minutes=1)
    a11.merge_given(check=False, hook=run)
    if run == 'timeout':
        a11.tick()
        a11.later(61)
    a11.check_finished(timeout=10)
    assert {'timeout': 'merge check: failed (timed out after 1 minute)',
            'changed-files': "merge check: failed (the project's checks changed files)"}[run] in a11.line('alpha-p1')
    assert 'merge check: passed' not in a11.line('alpha-p1')


@pytest.mark.ac('AC-84')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2])
def test_ac84_no_checks_and_local_merge(a11, row):
    a11.merge_given(hook='absent', merge_mode='local' if row == 2 else 'prepare')
    if row == 1:
        assert 'merge check: passed (no project checks)' in a11.line('alpha-p1')
    else:
        result = a11.run('merge', 'main', 'alpha-p1', '--repo', a11.repo_a)
        assert result.returncode == 0
        assert merge_parents(a11.parents('main'))[1] == a11.ref(INTEGRATION)


@pytest.mark.ac('AC-84')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('failed', [False, True])
def test_ac84_row3_user_check_environment(a11, failed):
    a11.profile(timezone='Europe/Copenhagen')
    a11.config(merge_check_path=str(a11.root / 'userbin') + ':/usr/bin:/bin')
    a11.executable(a11.root / 'userbin/setup', 'exit 1\n' if failed else 'touch ' + str(a11.root / 'setup-ran') + '\n')
    a11.merge_given(check=False, hook='environment', setup=[str(a11.root / 'userbin/setup')])
    a11.check_finished()
    if failed:
        assert 'merge check: failed (setup failed)' in a11.line('alpha-p1')
        assert not a11.exists(a11.root / 'hook-env')
    else:
        assert a11.exists(a11.root / 'setup-ran')
        text = a11.read(a11.root / 'hook-env')
        assert 'TZ=Europe/Copenhagen' in text
        assert 'PATH=' + str(a11.root / 'userbin') + ':/usr/bin:/bin' in text


@pytest.mark.ac('AC-85')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', ['identities', 'two-repos', 'zero-repos'])
def test_ac85_row1_merge_authority_and_one_repo(a11, run):
    if run != 'identities':
        a11.pid(repos=[{'path': str(a11.repo_a)}, {'path': str(a11.repo_b)}] if run == 'two-repos' else [])
        result = a11.run('pid', 'approve', 'alpha-p1')
        assert result.returncode == 3
        assert ('projects with more than one repo come in v1.1' if run == 'two-repos' else 'a git project needs one repo') in result.stderr
        assert present(a11.project())['state'] != 'executing'
    else:
        a11.merge_given()
        before = a11.ref('main')
        result = a11.run('merge', 'check', 'alpha-p1', '--repo', a11.repo_a, env={'YGG_NODE': 'pm1tl1'})
        assert result.returncode == 3 and 'identity: only the PM of alpha-p1 merges' in result.stderr
        for node in ['pm1', 'pm1tl1']:
            result = a11.run('merge', 'main', 'alpha-p1', env={'YGG_NODE': node})
            assert result.returncode == 3 and 'user-only' in result.stderr
            assert a11.ref('main') == before
        assert a11.run('merge', 'main', 'alpha-p1').returncode == 0
        assert merge_parents(a11.parents('main'))[1] == a11.ref(INTEGRATION)


@pytest.mark.ac('AC-85')
@pytest.mark.release('v1.0-blocker')
def test_ac85_row2_all_lane_packages_accepted(a11):
    a11.accepted('WP-1')
    a11.package('WP-3', criteria=True, authorise=True)
    before = a11.ref(INTEGRATION)
    result = a11.run('merge', 'lane', 'alpha-p1', '--lane', 'pm1tl1', '--repo', a11.repo_a, env={'YGG_NODE': 'pm1'})
    assert result.returncode == 3
    assert 'WP-3 on lane pm1tl1 is not accepted' in result.stderr
    assert a11.ref(INTEGRATION) == before


@pytest.mark.ac('AC-85')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('fault,text', FAULTS + [('past-accepted', "the lane head isn't the accepted commit")])
def test_ac85_row3_trusted_merge_recheck(a11, fault, text):
    if fault == 'past-accepted':
        a11.accepted('WP-1')
        a11.clean_trusted_commit()
    else:
        a11.fault_commit(fault, accepted=True)
    before = a11.ref(INTEGRATION)
    result = a11.run('merge', 'lane', 'alpha-p1', '--lane', 'pm1tl1', '--repo', a11.repo_a, env={'YGG_NODE': 'pm1'})
    assert result.returncode == 3
    assert a11.ref(INTEGRATION) == before
    assert text in result.stderr
    if fault != 'past-accepted':
        assert 're-check refused' in result.stderr


@pytest.mark.ac('AC-86')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row,mac_fails', [(1, False), (2, False), (3, False), (3, True)])
def test_ac86_fallback_and_hang(a11, row, mac_fails):
    a11.auto_alerts(headless=True)
    if row == 3:
        a11.quiesce_core_pms()
    if row == 1:
        a11.fakes(t3={'exit': 1})
    elif row == 3:
        a11.config(alert_send_timeout_seconds=2, alert_tick_budget_seconds=5)
        a11.fakes(t3={'sleep_seconds': 600}, osascript={'exit': 1 if mac_fails else 0})
    a11.raise_alert('ygg-corrupt-evidence' if row == 2 else 'ygg-launch-refused', node='pm1tl1')
    if row == 2:
        a11.tick(2)
        assert a11.notifications() == []
    else:
        _, elapsed = a11.elapsed(lambda: a11.tick())
        if row == 3:
            assert elapsed <= 10
        records = a11.delivery()
        assert [r['state'] for r in records if r['channel'] == 't3'] == ['pending', 'sent', 'timed-out' if row == 3 else 'failed']
        assert [r['state'] for r in records if r['channel'] == 'osascript'] == ['pending', 'sent', 'failed' if mac_fails else 'delivered']
        a11.fakes(t3={'sleep_seconds': 0, 'exit': 0}, osascript={'exit': 0})
        a11.later(15)
        a11.tick()
        assert len(a11.calls('t3')) == 1
        assert len(a11.calls('osascript')) == (2 if mac_fails else 1)
        if mac_fails:
            assert a11.delivery()[-1]['state'] == 'delivered'


@pytest.mark.ac('AC-87')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('interrupted', [False, True])
def test_ac87_row1_delivery_restart(a11, interrupted):
    a11.auto_alerts(headless=True)
    a11.quiesce_core_pms()
    a11.raise_alert('ygg-launch-refused', node='pm1tl1')
    if interrupted:
        a11.fakes(t3={'sleep_seconds': 600})
        a11.kill_conductor_during_send()
    else:
        a11.tick()
    a11.fakes(t3={'sleep_seconds': 0, 'exit': 0})
    a11.restart_conductor()
    a11.tick(2)
    assert len(a11.calls('t3')) == (2 if interrupted else 1)
    assert a11.delivery()[-1]['state'] == 'delivered'


@pytest.mark.ac('AC-87')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('jsonl', [True, False])
def test_ac87_row2_backoff_abandonment(a11, jsonl):
    if jsonl:
        a11.config(alert_forwarder='jsonl', launcher='headless')
    else:
        a11.auto_alerts(headless=True)
        a11.config(alert_max_attempts=2)
        a11.fakes(t3={'exit': 1}, osascript={'exit': 1})
    a11.raise_alert('ygg-launch-refused', node='pm1tl1')
    for seconds in [0, 5, 15, 45]:
        a11.later(seconds)
        a11.tick()
        if not jsonl:
            assert len(a11.calls('t3')) == (1 if seconds < 15 else 2)
            if seconds == 0:
                assert 'pending' in json.dumps(a11.delivery())
    if jsonl:
        assert a11.notifications() == []
    else:
        assert len(a11.calls('osascript')) == 2
        assert 'abandoned' in json.dumps(a11.delivery())
        alerts = a11.alerts('ygg-alert-undelivered')
        assert len(alerts) == 1 and alerts[0]['class'] == 'log'
        text = "1 alert couldn't be delivered; ask your main session to check it"
        assert text in a11.run('status').stdout
        a11.fakes(t3={'exit': 0}, osascript={'exit': 0})
        a11.raise_alert('ygg-stage-approval')
        a11.later(60)
        a11.tick()
        assert text not in a11.run('status').stdout


@pytest.mark.ac('AC-87')
@pytest.mark.release('v1.0-blocker')
def test_ac87_row3_next_steps_sent(a11):
    a11.auto_alerts(headless=True)
    for kind in ['ygg-launch-refused', 'ygg-stage-approval']:
        a11.raise_alert(kind, node='pm1tl1')
    a11.tick()
    calls = a11.calls('t3')
    assert len(calls) == 2
    for kind in ['ygg-launch-refused', 'ygg-stage-approval']:
        assert any(' '.join(call['argv']).endswith(NEXT_STEPS[kind]) for call in calls)
