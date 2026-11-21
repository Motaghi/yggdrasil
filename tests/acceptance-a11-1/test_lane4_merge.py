import shlex

import pytest

from driver_a11 import INTEGRATION, LANE


@pytest.mark.ac('AC-04')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('syncs', [False, True])
def test_ac04_row1_newest_acceptance_and_recorded_syncs_merge(a11_1, syncs):
    d = a11_1
    _, s2, head = d.two_accepted(syncs=syncs)
    result = d.merge_lane()
    assert result.returncode == 0
    assert d.contains(INTEGRATION, s2) is True
    if syncs:
        assert d.contains(INTEGRATION, head) is True
    assert (d.wp('WP-1') or {}).get('state') == 'merged'
    assert (d.wp('WP-2') or {}).get('state') == 'merged'


@pytest.mark.ac('AC-04')
@pytest.mark.release('v1.0-blocker')
def test_ac04_row2_explicit_accept_as_is_can_merge(a11_1):
    d = a11_1
    d.authorise()
    d.build()
    d.cap_current()
    h5 = d.ref(LANE)
    d.answer_cap('accept as is')
    result = d.accept(head=h5)
    assert result.returncode == 0
    assert (d.wp() or {}).get('state') == 'accepted'
    assert d.merge_lane().returncode == 0
    assert d.contains(INTEGRATION, h5) is True
    assert (d.wp() or {}).get('state') == 'merged'


@pytest.mark.ac('AC-04')
@pytest.mark.release('v1.0-blocker')
def test_ac04_row3_older_accepted_head_cannot_merge(a11_1):
    d = a11_1
    s1, _, _ = d.two_accepted()
    d.trusted_git(d.lane(), 'reset', '--hard', s1)
    integration = d.ref(INTEGRATION)
    result = d.merge_lane()
    assert result.returncode == 3
    assert "the lane head isn't the accepted commit" in result.stderr
    assert d.ref(INTEGRATION) == integration
    assert (d.wp('WP-1') or {}).get('state') == 'accepted'
    assert (d.wp('WP-2') or {}).get('state') == 'accepted'


@pytest.mark.ac('AC-07')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2])
def test_ac07_moved_head_leaves_ready_during_recheck(a11_1, row):
    d = a11_1
    d.merge_base()
    d.home_hook(body='sleep 30\nexit 0\n')
    if row == 1:
        d.main_change()
    else:
        d.integration_change()
    main = d.ref('main')
    d.tick()
    assert (d.project() or {}).get('state') == 'executing'
    assert 'ready to merge' not in d.line('alpha-p1')
    assert 'merge check:' in d.line('alpha-p1')
    result = d.merge_main()
    assert result.returncode == 3
    assert 'run the merge check first: ygg merge check alpha-p1 --repo ' + str(d.repo_a) in result.stderr
    assert d.ref('main') == main
    d.check_finished(timeout=40)
    assert (d.project() or {}).get('state') == 'ready-to-merge'
    assert len(d.alerts('ygg-merge-ready')) == 1


@pytest.mark.ac('AC-07')
@pytest.mark.release('v1.0-blocker')
def test_ac07_row3_failed_later_check_leaves_ready(a11_1):
    d = a11_1
    marker = shlex.quote(str(d.root / 'first-check'))
    hook = 'if test -e ' + marker + '; then echo "2 tests failed"; exit 1; fi\ntouch ' + marker + '\nexit 0\n'
    d.merge_base(hook=hook)
    main = d.ref('main')
    d.agent('pm1', mode='persist', auto_ack=False)
    d.run('merge', 'check', 'alpha-p1', '--repo', d.repo_a, env={'YGG_NODE': 'pm1'})
    d.check_finished()
    assert (d.project() or {}).get('state') == 'executing'
    assert 'merge check: failed' in d.line('alpha-p1')
    alerts = d.alerts('ygg-merge-check-failed')
    assert len(alerts) == 1 and alerts[0]['class'] == 'user'
    assert len(d.pending_ids('pm1')) == 1
    assert '2 tests failed' in d.inbox('pm1').stdout
    result = d.merge_main()
    assert result.returncode == 3 and 'the merge check failed; the PM is fixing it' in result.stderr
    assert d.ref('main') == main


def clash(d):
    d.merge_base(check=False)
    d.main_change('scripts/b.py', 'main\n')
    main = d.ref('main')
    d.check_finished()
    return main


@pytest.mark.ac('AC-08')
@pytest.mark.release('v1.0-blocker')
def test_ac08_row1_main_clash_has_plain_user_alert(a11_1):
    d = a11_1
    main = clash(d)
    before = len(d.launches('pm1'))
    d.tick(2)
    assert 'merge check: failed (main clashes with the work)' in d.line('alpha-p1')
    assert d.exists(d.root / 'home-hook-ran') is False
    alerts = d.alerts('ygg-merge-check-failed')
    assert len(alerts) == 1 and alerts[0]['class'] == 'user'
    assert alerts[0]['text'] == "alpha-p1's work clashes with recent changes on main, and the team can't settle that in this release."
    assert alerts[0]['next_step'] == "Ask your main session to combine main with the project's work by hand; the checks then run again by themselves."
    assert d.pending_messages('pm1') == []
    assert len(d.launches('pm1')) == before
    result = d.merge_main()
    assert result.returncode == 3 and 'the merge check failed: main clashes with the work' in result.stderr
    assert d.ref('main') == main


@pytest.mark.ac('AC-08')
@pytest.mark.release('v1.0-blocker')
def test_ac08_row2_user_combines_main_and_project_finishes(a11_1):
    d = a11_1
    clash(d)
    merge = d.combine_main()
    d.check_finished()
    assert 'merge check: passed' in d.line('alpha-p1')
    assert (d.project() or {}).get('state') == 'ready-to-merge'
    assert len(d.alerts('ygg-merge-ready')) == 1
    assert d.merge_main().returncode == 0
    d.tick()
    assert (d.project() or {}).get('state') == 'closed'
    assert d.contains('main', merge) is True
