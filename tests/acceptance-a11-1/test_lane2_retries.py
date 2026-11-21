import json

import pytest


def stalled(d, message, *, user=False):
    assert 'stalled' in d.line('pm1tl1')
    alerts = d.node_alerts('ygg-stall')
    assert len(alerts) == 1
    if user:
        assert alerts[0]['class'] == 'user'
    assert message in d.pending_ids()


@pytest.mark.ac('AC-01')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('first', ['crash', 'exit'])
def test_ac01_row1_every_crash_counts(a11_1, first):
    d = a11_1
    d.retry_given()
    for index, seconds in enumerate([0, 120, 240, 360, 480]):
        d.agent(mode=first if index == 0 else 'crash', exit_code=1)
        d.timed_tick(seconds)
        if index == 1:
            assert 'stalled' in d.line()
    records = d.retry_records()
    assert len(records) == 2
    if first == 'exit':
        assert records[0]['exit_code'] == 1
        assert records[1]['exit_code'] < 0
    stalled(d, 'm-0010', user=True)


@pytest.mark.ac('AC-01')
@pytest.mark.release('v1.0-blocker')
def test_ac01_row2_success_resets_chain(a11_1):
    d = a11_1
    d.retry_given()
    for index in range(10):
        d.agent(mode='exit' if index == 1 else 'crash', exit_code=0)
        d.timed_tick(index * 120)
        if index == 1:
            d.send(11, body='x')
    assert len(d.retry_records()) == 4
    stalled(d, 'm-0011', user=True)


@pytest.mark.ac('AC-01')
@pytest.mark.release('v1.0-blocker')
def test_ac01_row3_stop_release_resume_keeps_count(a11_1):
    d = a11_1
    d.retry_given(limit=2)
    d.agent(mode='crash')
    d.timed_tick(0)
    d.agent(mode='persist')
    d.timed_tick(120, wait=False)
    d.wait_launch('pm1tl1')
    d.run('stop', 'pm1tl1')
    d.wait_exit('pm1tl1')
    d.run('node', 'release', 'pm1tl1', '--as', 'aborted')
    d.run('resume', 'pm1tl1')
    d.agent(mode='crash')
    for seconds in [240, 360, 480, 600, 720, 840]:
        d.timed_tick(seconds)
    assert len(d.retry_records()) == 4
    stalled(d, 'm-0010', user=True)


@pytest.mark.ac('AC-02')
@pytest.mark.release('v1.0-blocker')
def test_ac02_row1_failed_work_stalls_at_limit(a11_1):
    d = a11_1
    d.retry_given()
    d.agent(mode='exit', exit_code=1)
    for seconds in [0, 120, 240, 360, 480]:
        d.timed_tick(seconds)
    records = d.retry_records()
    assert len(records) == 2
    for record in records:
        assert record['exit_code'] == 1
        assert 'm-0010' in json.dumps(record)
    stalled(d, 'm-0010')


@pytest.mark.ac('AC-02')
@pytest.mark.release('v1.0-blocker')
def test_ac02_row2_acked_message_stays_owed_on_failure(a11_1):
    d = a11_1
    d.retry_given()
    d.agent(mode='exit', exit_code=1, auto_ack=False,
            steps=[{'id': 'w1', 'wait': 'go-1'}])
    d.timed_tick(0, wait=False)
    d.wait_launch('pm1tl1')
    d.send(11, body='x')
    d.ack(11)
    d.wait_control('go-1')
    d.wait_exit('pm1tl1')
    d.agent(mode='exit', exit_code=1, auto_ack=False)
    for seconds in [120, 240, 360]:
        d.timed_tick(seconds)
    records = d.retry_records()
    assert len(records) == 2
    assert records[-1]['exit_code'] == 1
    assert 'm-0010' in json.dumps(records[-1]) and 'm-0011' in json.dumps(records[-1])
    stalled(d, 'm-0010')
    assert 'm-0011' in d.pending_ids()


@pytest.mark.ac('AC-02')
@pytest.mark.release('v1.0-blocker')
def test_ac02_row3_resume_does_not_restart_stalled(a11_1):
    d = a11_1
    d.retry_given()
    d.agent(mode='exit', exit_code=1)
    for seconds in [0, 120, 240, 360, 480]:
        d.timed_tick(seconds)
    d.run('stop', 'pm1tl1')
    d.run('resume', 'pm1tl1')
    for seconds in [600, 720, 840]:
        d.timed_tick(seconds)
    assert len(d.retry_records()) == 2
    stalled(d, 'm-0010')
