import re

import pytest


@pytest.mark.release('v1.1-mandatory')
@pytest.mark.ac('AC-08')
def test_ac08_live_http_page_changes_and_recorded_server_stops(d):
    result, folder, pages = d.held(http=True)
    assert len(pages) == 2
    assert result.returncode == 0
    line = next(line for line in result.stdout.splitlines()
                if line.startswith('demo: dashboard http://127.0.0.1:'))
    assert re.fullmatch(r'demo: dashboard http://127\.0\.0\.1:\d+/ \(refreshes every 2 s\)', line)
    for status, body in pages:
        assert status == 200
        assert '<meta http-equiv="refresh" content="2">' in body
    assert 'data-package="WP-1"' not in pages[0][1]
    columns = d.package_columns(pages[1][1])
    assert columns['WP-1'] == 'done'
    assert columns['WP-2'] == 'done'
    assert folder is not None
    assert d.exists(folder / 'dashboard.pid')
    recorded = d.pid_pair(folder / 'dashboard.pid')
    assert recorded is not None
    assert d.live_pairs([recorded]) == []


@pytest.mark.release('v1.1-optional')
@pytest.mark.ac('AC-15')
def test_ac15_fenced_spaced_approval_is_read_once_without_hold(d):
    statuses, launches, alerts, _ = d.codex_verdict(
        '```json\n{"review": "rv-0001", "verdict": " Approve "}\n```')
    assert len(statuses) == 1
    line = next(line for line in statuses[0].splitlines() if line.startswith('WP-1: '))
    assert 'review round 1, approved' in line
    assert sum(row.get('review') == 'rv-0001' for row in launches) == 1
    assert not any(row.get('kind') == 'ygg-review-held' for row in alerts)


@pytest.mark.release('v1.1-optional')
@pytest.mark.ac('AC-15')
def test_ac15_missing_review_key_revises_with_one_findings_message(d):
    statuses, _, _, messages = d.codex_verdict(
        '{"verdict": "revise", "findings": [{"file": "a.py", "line": 3, "text": "off by one"}]}')
    assert len(statuses) == 1
    line = next(line for line in statuses[0].splitlines() if line.startswith('WP-1: '))
    assert 'review round 1, revise' in line
    assert sum(row.get('to') == 'pm1tl1' and 'a.py:3 off by one' in row.get('body', '')
               for row in messages) == 1


@pytest.mark.release('v1.1-optional')
@pytest.mark.ac('AC-15')
def test_ac15_other_review_verdict_is_never_approved_and_holds_after_two_tries(d):
    statuses, _, alerts, _ = d.codex_verdict(
        '{"review": "rv-0002", "verdict": "approve"}', attempts=2)
    assert len(statuses) == 2
    for status in statuses:
        line = next(line for line in status.splitlines() if line.startswith('WP-1: '))
        assert 'approved' not in line
    assert 'WP-1 review held: the reviewer gave no verdict' in statuses[-1].splitlines()
    assert sum(row.get('kind') == 'ygg-review-held' for row in alerts) == 1


@pytest.mark.release('v1.1-optional')
@pytest.mark.ac('AC-20')
def test_ac20_changed_testing_details_send_one_urgent_notice_with_no_change_control(d):
    changed = d.testing_details(change=True)
    notices = [row for row in changed if row.get('to') == 'pm1su1' and row.get('urgent') is True
               and 'testing details of WP-1 changed (criteria version 2)' in row.get('body', '')]
    assert len(notices) == 1
    unchanged = d.testing_details(change=False)
    assert not any(row.get('to') == 'pm1su1'
                   and 'testing details of WP-1 changed' in row.get('body', '')
                   for row in unchanged)
