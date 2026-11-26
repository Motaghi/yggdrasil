from contextlib import ExitStack
from pathlib import Path
import uuid

import pytest

from driver import CodexDriver


REFUSAL = 'pid-ready is only for a new, rejected or replanned PID'


def project_state(d):
    return next((row.get('state') for row in (d.status() or {}).get('projects', [])
                 if row.get('id') == 'alpha-p1'), None)


def pending_rows(d):
    pending = d.pending() or {}
    return pending.get('decisions', []) if isinstance(pending, dict) else pending


def pid_ready_alerts(d):
    return [row for row in (d.command_json('alerts', '--json') or [])
            if row.get('kind') == 'ygg-pid-ready']


def pid_ready_write(d):
    return d.run('tracker', 'write', '--as', 'pm1', '--project', 'alpha-p1',
                 '--kind', 'pid-ready', '--body', 'ready')


@pytest.fixture
def upgrade_factory():
    with ExitStack() as stack:
        def create(*, replan_before_upgrade=False):
            root = Path(__file__).resolve().parent / '.scratch' / uuid.uuid4().hex
            d = stack.enter_context(CodexDriver(root))
            with d.v100_cli():
                d.standard(codex=False, approved=False)
                # The supplied adapter uses the core ready flow underlying A11.ready.
                approved = d.ready()
                if not d.is_stub:
                    assert approved.returncode == 0, approved.stderr
                    assert project_state(d) == 'executing'
                raised = d.run('exception', 'raise', '--from', 'pm1',
                               '--reason', 'scope moved', '--plan', 'new plan')
                exceptions = [row for row in pending_rows(d)
                              if row.get('project') == 'alpha-p1'
                              and row.get('kind') == 'exception']
                if not d.is_stub:
                    assert raised.returncode == 0, raised.stderr
                    assert len(exceptions) == 1, exceptions
                exception = exceptions[0]['id'] if exceptions else ''
                rejected = d.run('exception', 'decide', exception, '--plan', 'reject')
                question = f'Exception {exception} plan rejected: what next?'
                decisions = [row for row in pending_rows(d)
                             if row.get('project') == 'alpha-p1'
                             and row.get('kind') == 'decision'
                             and row.get('question') == question]
                if not d.is_stub:
                    assert rejected.returncode == 0, rejected.stderr
                    assert project_state(d) == 'exception'
                    assert len(decisions) == 1, decisions
                decision = decisions[0]['id'] if decisions else ''
                if replan_before_upgrade:
                    answered = d.run('decide', decision, '--option', 'replan')
                    if not d.is_stub:
                        assert answered.returncode == 0, answered.stderr
            return d, exception, decision
        yield create


@pytest.mark.ac('AC-57')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac57_row1_legacy_followup_replan_after_upgrade(upgrade_factory):
    d, _, decision = upgrade_factory()
    before = len(pid_ready_alerts(d))
    decided = d.run('decide', decision, '--option', 'replan')
    written = pid_ready_write(d)
    assert decided.returncode == 0
    assert written.returncode == 0
    assert project_state(d) == 'pid-ready'
    assert len(pid_ready_alerts(d)) - before == 1


@pytest.mark.ac('AC-57')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac57_row2_legacy_followup_replan_before_upgrade(upgrade_factory):
    d, _, _ = upgrade_factory(replan_before_upgrade=True)
    before = len(pid_ready_alerts(d))
    written = pid_ready_write(d)
    assert written.returncode == 0
    assert project_state(d) == 'pid-ready'
    assert len(pid_ready_alerts(d)) - before == 1


@pytest.mark.ac('AC-57')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac57_row3_open_legacy_followup_refuses_pid_ready(upgrade_factory):
    d, _, _ = upgrade_factory()
    before = len(pid_ready_alerts(d))
    written = pid_ready_write(d)
    assert written.returncode == 3
    assert REFUSAL in written.stderr
    assert project_state(d) == 'exception'
    assert len(pid_ready_alerts(d)) - before == 0


@pytest.mark.ac('AC-58')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac58_row1_new_lookalike_replan_refuses_pid_ready(upgrade_factory):
    d, exception, _ = upgrade_factory()
    question = f'Exception {exception} plan rejected: what next?'
    existing = {row['id'] for row in pending_rows(d)}
    raised = d.run('decision', 'raise', '--from', 'pm1', '--question', question,
                   '--option', 'replan', '--option', 'close', '--recommended', 'replan',
                   '--reversible', 'no', '--aligns', 'no')
    lookalikes = [row for row in pending_rows(d)
                  if row.get('project') == 'alpha-p1' and row.get('kind') == 'decision'
                  and row.get('question') == question and row.get('id') not in existing]
    if not d.is_stub:
        assert raised.returncode == 0, raised.stderr
        assert len(lookalikes) == 1, lookalikes
    lookalike = lookalikes[0]['id'] if lookalikes else ''
    decided = d.run('decide', lookalike, '--option', 'replan')
    if not d.is_stub:
        assert decided.returncode == 0, decided.stderr
    before = len(pid_ready_alerts(d))
    written = pid_ready_write(d)
    assert written.returncode == 3
    assert REFUSAL in written.stderr
    assert project_state(d) == 'exception'
    assert len(pid_ready_alerts(d)) - before == 0


@pytest.mark.ac('AC-58')
@pytest.mark.release('v1.1.1-mandatory')
def test_ac58_row2_closed_legacy_followup_refuses_pid_ready(upgrade_factory):
    d, _, decision = upgrade_factory()
    decided = d.run('decide', decision, '--option', 'close')
    if not d.is_stub:
        assert decided.returncode == 0, decided.stderr
    before = len(pid_ready_alerts(d))
    written = pid_ready_write(d)
    assert written.returncode == 3
    assert REFUSAL in written.stderr
    assert project_state(d) != 'pid-ready'
    assert len(pid_ready_alerts(d)) - before == 0
