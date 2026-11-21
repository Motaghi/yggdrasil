import pytest

from driver_a11 import LANE


def dropped(d):
    c1, c2 = d.cancel_commits()
    d.cancel()
    return c1, c2


@pytest.mark.ac('AC-12')
@pytest.mark.release('v1.0-blocker')
def test_ac12_row1_history_guard_refuses_dropped_ancestor(a11_1):
    d = a11_1
    c1, _ = dropped(d)
    d.send(12, body='x')
    result = d.owed_git('reset', '--soft', c1)
    assert result.returncode != 0
    assert "the lane can't go back to the work of cancelled WP-1" in result.stderr
    assert d.ref(LANE) == d.l0


@pytest.mark.ac('AC-12')
@pytest.mark.release('v1.0-blocker')
def test_ac12_row2_same_edit_in_new_commit_is_new_work(a11_1):
    d = a11_1
    dropped(d)
    d.authorise('WP-3')
    result, c5 = d.build(message='C5')
    assert result.returncode == 0
    assert c5
    assert d.ref(LANE) == c5
    d.main_change('docs/notes.md')
    assert d.sync().returncode == 0


@pytest.mark.ac('AC-12')
@pytest.mark.release('v1.0-blocker')
def test_ac12_row3_ready_rechecks_dropped_ancestor(a11_1):
    d = a11_1
    c1, _ = dropped(d)
    d.authorise('WP-3')
    d.trusted_git(d.lane(), 'reset', '--hard', c1)
    reviews = len(d.launches('pm1rv1'))
    result = d.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-3')
    assert result.returncode == 3
    assert 're-check refused' in result.stderr
    assert "the lane can't go back to the work of cancelled WP-1" in result.stderr
    assert len(d.launches('pm1rv1')) == reviews
    assert (d.wp('WP-3') or {}).get('state') == 'authorised'
    assert 'review rv-' not in result.stdout
