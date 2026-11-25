"""The confirm step and an agent that exits while confirm waits for the transition lock.

Ordering under test: at the end of the first confirm window the agent is alive, so confirm skips its wait for an
exit record; the agent then exits while confirm waits for the transition lock, before its supervisor has written
the exit record (the supervisor writes it under that same lock). Judged at that moment the start looks like a
process that vanished, and confirm used to hold it as uncertain with a false alert. The same ordering applies when
another process (the conductor's reconcile) already marked the start confirmed while the agent ran.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from yggdrasil import launcher

REAL_ALIVE = launcher._alive
REAL_HOLD = launcher.hold_uncertain


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Supervisor:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


@pytest.fixture
def world(monkeypatch):
    """A scripted receipt, lock, clock and agent liveness for one activation."""
    clock = Clock()
    world = {"receipt": {"pid": 4242, "process_started_at": "s:1000.000000", "exited_at": None,
                         "outcome": "pending"},
             "alive": True, "locks": 0, "record_after_lock": True, "held": [], "finalized": [], "clock": clock,
             "lock_free_waits": 0, "exit_code": 0, "die_at_lock": True, "reuse": False, "reused": False}

    @contextmanager
    def locked(*args, **kwargs):
        world["locks"] += 1
        if world["locks"] == 1 and world["die_at_lock"]:
            world["alive"] = False  # the agent exits while confirm waits for the lock; no exit record yet
        world["in_lock"] = True
        try:
            yield
        finally:
            world["in_lock"] = False

    def sleep(seconds):
        clock.sleep(seconds)
        if world["reuse"] and not world.get("in_lock"):
            world["alive"] = world["reused"] = True  # another process now has the agent's old pid
        # The supervisor writes the exit record under the transition lock: only once confirm has released it.
        if not world.get("in_lock") and world["locks"] >= 1 and world["record_after_lock"]:
            world["lock_free_waits"] += 1
            world["receipt"] = dict(world["receipt"], exited_at="2026-11-23T05:00:00Z", exit_code=world["exit_code"])

    monkeypatch.setattr(launcher.util, "locked", locked)
    monkeypatch.setattr(launcher, "receipt", lambda activation: dict(world["receipt"]))
    monkeypatch.setattr(launcher, "update_receipt", lambda activation, **fields: world["receipt"].update(fields))
    # The pid table, not a fixed answer: the pid is alive or not, and its start matches the recorded identity
    # only while it is still the agent's own process.
    monkeypatch.setattr(launcher.util, "pid_alive", lambda pid: world["alive"])
    monkeypatch.setattr(launcher.util, "same_start", lambda pid, started: world["alive"] and not world["reused"])
    monkeypatch.setattr(launcher.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(launcher.time, "sleep", sleep)
    monkeypatch.setattr(launcher.config, "get", lambda key, default=None: 0 if key == "launch_confirm_seconds"
                        else default)
    monkeypatch.setattr(launcher, "hold_uncertain",
                        lambda node, activation, why, **kw: world["held"].append(why) or "uncertain")
    monkeypatch.setattr(launcher, "finalize",
                        lambda node, activation, outcome, **kw: world["finalized"].append(outcome) or outcome)
    return world


def test_an_exit_that_lands_while_confirm_waits_for_the_lock_is_judged_from_its_record(world):
    result = launcher.confirm("pm1", "act-0001", Supervisor())
    assert result["outcome"] == "confirmed"
    assert world["held"] == []  # no false uncertain alert
    assert world["finalized"] == ["confirmed"]
    assert world["lock_free_waits"] >= 1  # the record could only be written outside the lock


def test_no_exit_record_within_the_grace_is_still_held_as_uncertain(world):
    world["record_after_lock"] = False
    result = launcher.confirm("pm1", "act-0001", Supervisor())
    assert result["outcome"] == "uncertain"
    assert world["held"] == ["no process reported within the confirm window"]
    assert world["finalized"] == []  # never a success without an exit record
    assert world["clock"].now - 1000.0 >= 10.0  # the wait is bounded by the existing exit-record grace


def test_a_supervisor_that_is_gone_is_held_at_once_as_before(world):
    world["record_after_lock"] = False
    result = launcher.confirm("pm1", "act-0001", Supervisor(code=1))
    assert result["outcome"] == "uncertain"
    assert world["locks"] == 1  # no second look: nothing can still write the record


def test_with_the_confirm_window_on_the_start_is_left_pending_unchanged(world):
    world["receipt"]["confirm_window_seconds"] = 60.0
    result = launcher.confirm("pm1", "act-0001", Supervisor())
    assert result["outcome"] == "pending"
    assert world["locks"] == 1 and world["held"] == [] and world["finalized"] == []
    assert world["receipt"].get("confirm_left_pending_at")


@pytest.fixture
def confirmed(world):
    world["receipt"]["outcome"] = "confirmed"  # the conductor confirmed it while the agent ran
    return world


@pytest.mark.parametrize("code, outcome", [(0, "confirmed"), (1, "failed")])
def test_an_already_confirmed_start_whose_exit_lands_during_the_lock_wait_is_judged_from_its_record(
        confirmed, code, outcome):
    confirmed["exit_code"] = code
    result = launcher.confirm("pm1", "act-0001", Supervisor())
    assert result["outcome"] == outcome
    assert confirmed["held"] == []  # no false uncertain alert
    assert confirmed["finalized"] == []  # already recorded as confirmed: nothing is finalized again
    assert confirmed["lock_free_waits"] >= 1


def test_an_already_confirmed_start_without_an_exit_record_within_the_grace_is_still_held(confirmed):
    confirmed["record_after_lock"] = False
    result = launcher.confirm("pm1", "act-0001", Supervisor())
    assert result["outcome"] == "uncertain"
    assert confirmed["held"] == ["the process is gone and no exit was recorded"]
    assert confirmed["clock"].now - 1000.0 >= 10.0


@pytest.mark.parametrize("supervisor_code, window", [(1, None), (None, 60.0)], ids=["supervisor-gone", "window-on"])
def test_an_already_confirmed_start_is_held_at_once_as_before(confirmed, supervisor_code, window):
    confirmed["record_after_lock"] = False
    if window:
        confirmed["receipt"]["confirm_window_seconds"] = window
    result = launcher.confirm("pm1", "act-0001", Supervisor(code=supervisor_code))
    assert result["outcome"] == "uncertain"
    assert confirmed["locks"] == 1 and confirmed["held"] == ["the process is gone and no exit was recorded"]


# ---------------------------------------------------------------- pid reuse after the agent was seen gone

@pytest.fixture
def reuse(world, monkeypatch):
    """The real liveness check, over a scripted pid table: the receipt has no recorded start identity (its lookup
    failed at launch), the supervisor stays alive without ever writing the exit record, and once the agent is
    gone another process takes its pid."""
    def no_identity_check(*args):
        raise AssertionError("no identity is recorded: it can't be compared")
    monkeypatch.setattr(launcher.util, "same_start", no_identity_check)
    world["receipt"]["process_started_at"] = None
    world["record_after_lock"] = False
    world["reuse"] = True
    return world


def test_a_reused_pid_after_a_death_at_the_lock_is_held_never_confirmed(reuse):
    result = launcher.confirm("pm1", "act-0001", Supervisor())
    assert result["outcome"] == "uncertain"
    assert reuse["held"] == ["no process reported within the confirm window"]
    assert reuse["finalized"] == []  # never a success without an exit record
    assert reuse["alive"] is True  # the pid did look alive again


def test_a_reused_pid_of_an_already_confirmed_start_is_held_never_confirmed(reuse):
    reuse["receipt"]["outcome"] = "confirmed"
    result = launcher.confirm("pm1", "act-0001", Supervisor())
    assert result["outcome"] == "uncertain"
    assert reuse["held"] == ["the process is gone and no exit was recorded"]
    assert reuse["finalized"] == []


@pytest.mark.parametrize("outcome, why", [("pending", "no process reported within the confirm window"),
                                          ("confirmed", "the process is gone and no exit was recorded")])
def test_a_reused_pid_after_a_death_seen_before_the_lock_is_held(reuse, outcome, why):
    reuse["receipt"]["outcome"] = outcome
    reuse["alive"] = False  # gone already when confirm first checks it
    reuse["die_at_lock"] = False
    result = launcher.confirm("pm1", "act-0001", Supervisor())
    assert result["outcome"] == "uncertain"
    assert reuse["held"] == [why] and reuse["finalized"] == []
    assert reuse["locks"] == 1  # one grace was spent already: held on the first look


def test_after_a_death_an_exit_record_still_decides(reuse):
    reuse["record_after_lock"] = True  # the record lands, as well as the pid being reused
    result = launcher.confirm("pm1", "act-0001", Supervisor())
    assert result["outcome"] == "confirmed" and reuse["held"] == []
    assert reuse["finalized"] == ["confirmed"]


@pytest.mark.parametrize("fields, alive, same, expected", [
    ({"process_started_at": None}, True, None, "uncertain"),  # a live pid of unknown identity: not proof
    ({}, True, True, "confirmed"),  # its own process, by its recorded identity
    ({}, True, False, "uncertain"),  # the pid now belongs to another process
    ({"exited_at": "2026-11-23T05:00:00Z", "exit_code": 1}, True, True, "failed"),  # the exit record decides
    ({"exited_at": "bad", "exit_code": False}, True, True, "uncertain"),  # a record that can't be read
], ids=["no-identity", "identity-matches", "reused", "exit-record", "malformed-record"])
def test_hold_uncertain_confirms_only_from_proof(monkeypatch, fields, alive, same, expected):
    """The real hold path over a confirmed receipt."""
    held = []
    record = dict({"outcome": "confirmed", "pid": 4242, "exited_at": None, "process_started_at": "s:1.0"}, **fields)
    monkeypatch.setattr(launcher, "hold_uncertain", REAL_HOLD)
    monkeypatch.setattr(launcher, "receipt", lambda activation: dict(record))
    monkeypatch.setattr(launcher.util, "pid_alive", lambda pid: alive)
    monkeypatch.setattr(launcher.util, "same_start", lambda pid, started: same)
    monkeypatch.setattr(launcher, "mark_uncertain",
                        lambda node, activation, why, **kw: held.append(why) or record.update(outcome="uncertain"))
    assert launcher.hold_uncertain("pm1", "act-0001", "w") == expected
    assert held == (["w"] if expected == "uncertain" else [])


# ---------------------------------------------------------------- one grace in total, and a supervisor that ends

class EndingSupervisor:
    """A supervisor that ends, without writing the exit record, after `polls` looks at it."""
    def __init__(self, polls):
        self.polls = polls

    def poll(self):
        self.polls -= 1
        return None if self.polls >= 0 else 1


@pytest.mark.parametrize("seen", ["before-the-lock", "at-the-lock"])
def test_every_wait_for_the_exit_record_shares_one_grace(world, seen):
    world["record_after_lock"] = False
    if seen == "before-the-lock":
        world["alive"], world["die_at_lock"] = False, False
    result = launcher.confirm("pm1", "act-0001", Supervisor())
    assert result["outcome"] == "uncertain"
    waited = world["clock"].now - 1000.0
    assert 10.0 <= waited < 10.0 + 0.2  # one grace of max(10 s, launch_confirm_seconds), never two


@pytest.mark.parametrize("seen", ["before-the-lock", "at-the-lock"])
def test_a_supervisor_that_ends_during_the_wait_is_held_at_once(world, seen):
    world["record_after_lock"] = False
    if seen == "before-the-lock":
        world["alive"], world["die_at_lock"] = False, False
    result = launcher.confirm("pm1", "act-0001", EndingSupervisor(polls=3))
    assert result["outcome"] == "uncertain"
    assert world["held"] == ["no process reported within the confirm window"]
    assert world["clock"].now - 1000.0 < 0.5  # not after the full grace


# ---------------------------------------------------------------- an exit record written after the conductor's read

@pytest.mark.parametrize("recorded", [True, False], ids=["record-landed", "control-no-record"])
def test_an_exit_record_written_after_the_conductor_read_the_receipt_raises_no_alert(monkeypatch, recorded):
    """The conductor reads the receipt (no exit record yet); the agent then exits and its supervisor writes the
    record and ends; the conductor's settle, judging from that read, finds both gone and holds the start. Under
    the lock the record is there: nothing is held and no alert is raised. Without a record it is held, as before."""
    from contextlib import nullcontext

    from yggdrasil import conductor, lifecycle, t3
    stale = {"activation": "act-0001", "node": "pm1", "outcome": "confirmed", "pid": 4242, "exited_at": None,
             "lifecycle": True}
    fresh = dict(stale, exited_at="2026-11-23T05:00:00Z", exit_code=0) if recorded else dict(stale)
    saved, alerts = [], []
    monkeypatch.setattr(launcher.util, "locked", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(launcher, "receipt", lambda activation: dict(fresh))
    monkeypatch.setattr(launcher, "save_receipt", lambda activation, data, **kw: saved.append(data))
    monkeypatch.setattr(launcher, "_alert_uncertain", lambda node, activation, why: alerts.append(why))
    monkeypatch.setattr(launcher.state, "runtime",
                        lambda node: {"activation": "act-0001", "state": "running", "outcome": "confirmed"})
    monkeypatch.setattr(launcher.state, "save_runtime", lambda node, runtime: saved.append(runtime))
    monkeypatch.setattr(conductor, "_programme_of", lambda node: "alpha")
    monkeypatch.setattr(lifecycle, "facts", lambda programme, activation: [{"phase": "reserved"}, {"phase": "spawned"}])
    monkeypatch.setattr(lifecycle, "mirror_terminal", lambda node, activation: None)
    monkeypatch.setattr(lifecycle, "settle", lambda node, activation, data, found: "unsettled")  # both gone, by `data`
    monkeypatch.setattr(t3, "is_t3", lambda node, activation: False)
    conductor._reconcile_lifecycle("pm1", "act-0001", {"activation": "act-0001", "state": "running"}, stale)
    if recorded:
        assert alerts == [] and saved == []
    else:
        assert alerts == ["spawned, no agent evidence and no exit record"]
        assert saved and saved[0]["outcome"] == "uncertain"


# ---------------------------------------------------------------- one rule: proof is an exit record or the agent itself

IDENTITY = {"recorded": "s:1000.000000", "missing": None}
EXIT_RECORDS = {"valid": {"exited_at": "2026-11-23T05:00:00Z", "exit_code": 1},
                "malformed": {"exited_at": "bad", "exit_code": False},
                "absent": {}}


def _expected(identity, pid, record):
    if record == "valid":
        return "failed", "failed"  # the record decides, whatever the pid shows
    if record == "malformed":
        return "malformed", "uncertain"
    if pid == "alive":
        return ("alive", "confirmed") if identity == "recorded" else ("unknown", "uncertain")
    if pid == "reused" and identity == "missing":
        return "unknown", "uncertain"  # a live pid of unknown identity proves nothing
    return "dead", "uncertain"


@pytest.mark.parametrize("supervisor", ["alive", "gone"])
@pytest.mark.parametrize("record", ["valid", "malformed", "absent"])
@pytest.mark.parametrize("pid", ["alive", "dead", "reused"])
@pytest.mark.parametrize("identity", ["recorded", "missing"])
def test_a_start_is_confirmed_only_by_an_exit_record_or_its_own_live_process(world, identity, pid, record,
                                                                            supervisor):
    world["receipt"].update(process_started_at=IDENTITY[identity], **EXIT_RECORDS[record])
    world["record_after_lock"] = world["die_at_lock"] = False
    world["alive"] = pid in ("alive", "reused")
    world["reused"] = pid == "reused"
    evidence, outcome = _expected(identity, pid, record)
    if record == "valid":
        evidence = "failed"
    assert launcher.start_evidence(dict(world["receipt"])) == evidence
    result = launcher.confirm("pm1", "act-0001", Supervisor(code=None if supervisor == "alive" else 1))
    assert result["outcome"] == outcome
    assert (world["finalized"] == []) == (outcome == "uncertain")  # never finalized without proof
    assert world["clock"].now - 1000.0 <= 10.0 + 0.2  # at most one grace


# ---------------------------------------------------------------- QA's schedules on a701934, as named regressions

def _pid_table(monkeypatch, answers):
    """pid_alive answers in order (the last one repeats); no recorded identity, so no start is compared."""
    answers = list(answers)
    monkeypatch.setattr(launcher.util, "pid_alive", lambda pid: answers.pop(0) if len(answers) > 1 else answers[0])

    def no_identity_check(*args):
        raise AssertionError("no identity is recorded: it can't be compared")
    monkeypatch.setattr(launcher.util, "same_start", no_identity_check)


def test_regression_qa1_alive_before_the_lock_gone_at_it_supervisor_gone_then_reused(world, monkeypatch):
    """Alive before the lock, gone at the locked look, supervisor gone (no second look), and the pid reused before
    the real hold path reads it: held, never confirmed."""
    world["receipt"].update(outcome="confirmed", process_started_at=None)
    world["die_at_lock"] = world["record_after_lock"] = False
    _pid_table(monkeypatch, [True, False, True])
    monkeypatch.setattr(launcher, "hold_uncertain", REAL_HOLD)
    marked = []
    monkeypatch.setattr(launcher, "mark_uncertain", lambda node, activation, why, **kw: marked.append(why) or
                        world["receipt"].update(outcome="uncertain"))
    result = launcher.confirm("pm1", "act-0001", Supervisor(code=1))
    assert result["outcome"] == "uncertain" and marked == ["the process is gone and no exit was recorded"]
    assert world["receipt"].get("exited_at") is None


@pytest.mark.parametrize("outcome", ["pending", "confirmed"])
def test_regression_qa2_no_supervisor_death_before_the_lock_then_reused(world, monkeypatch, outcome):
    """With no supervisor, a death seen before the lock is still recorded; the pid reused before the lock does not
    confirm it on either branch, and the pending branch finalizes nothing."""
    world["receipt"].update(outcome=outcome, process_started_at=None)
    world["die_at_lock"] = world["record_after_lock"] = False
    _pid_table(monkeypatch, [False, True])
    result = launcher.confirm("pm1", "act-0001", None)
    assert result["outcome"] == "uncertain"
    assert world["finalized"] == [] and len(world["held"]) == 1


def test_regression_qa3_a_malformed_exit_record_never_skips_the_hold(monkeypatch):
    """`exited_at` that isn't a time and an `exit_code` of False: the real mark_uncertain holds and alerts."""
    from contextlib import nullcontext
    record = {"activation": "act-0001", "node": "pm1", "outcome": "confirmed", "pid": 4242,
              "exited_at": "bad", "exit_code": False}
    saved, alerts = [], []
    monkeypatch.setattr(launcher.util, "locked", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(launcher, "receipt", lambda activation: dict(record))
    monkeypatch.setattr(launcher, "save_receipt", lambda activation, data, **kw: saved.append(data))
    monkeypatch.setattr(launcher, "_alert_uncertain", lambda node, activation, why: alerts.append(why))
    monkeypatch.setattr(launcher.state, "runtime", lambda node: {"activation": "act-0001", "state": "running"})
    monkeypatch.setattr(launcher.state, "save_runtime", lambda node, runtime: None)
    assert launcher.mark_uncertain("pm1", "act-0001", "why", allow_confirmed=True) is True
    assert alerts == ["why"] and saved[0]["outcome"] == "uncertain"


# ---------------------------------------------------------------- the confirm window on: the decision as in v1.0

def _window_on_expected(identity, pid, record, supervisor):
    """What the v1.0 decision (a121f3d) gives with the window on: `classify` over `_alive`, which trusts a live
    pid without a recorded identity; a start without an outcome stays pending while its supervisor lives."""
    if record == "valid":
        return "failed"
    if record == "malformed":
        return "confirmed"  # `exit_code` False reads as 0 there; the real finalize holds it (the real-file test)
    if pid == "alive" or (pid == "reused" and identity == "missing"):
        return "confirmed"
    return "pending" if supervisor == "alive" else "uncertain"


@pytest.mark.parametrize("supervisor", ["alive", "gone"])
@pytest.mark.parametrize("record", ["valid", "malformed", "absent"])
@pytest.mark.parametrize("pid", ["alive", "dead", "reused"])
@pytest.mark.parametrize("identity", ["recorded", "missing"])
def test_with_the_window_on_the_decision_is_the_v1_0_one(world, identity, pid, record, supervisor):
    world["receipt"].update(process_started_at=IDENTITY[identity], confirm_window_seconds=90.0,
                            **EXIT_RECORDS[record])
    world["record_after_lock"] = world["die_at_lock"] = False
    world["alive"] = pid in ("alive", "reused")
    world["reused"] = pid == "reused"
    expected = _window_on_expected(identity, pid, record, supervisor)
    result = launcher.confirm("pm1", "act-0001", Supervisor(code=None if supervisor == "alive" else 1))
    assert result["outcome"] == expected
    assert world["finalized"] == ([expected] if expected not in ("pending", "uncertain") else [])
    assert world["locks"] == 1  # one look, as in v1.0
    assert world["clock"].now - 1000.0 <= 10.0 + 0.2


EXPLICIT_ENDS = {
    "exit-0": ({"exited_at": "2026-11-23T05:00:00Z", "exit_code": 0}, "confirmed"),
    "capacity": ({"exited_at": "2026-11-23T05:00:00Z", "exit_code": 1, "stderr_tail": "rate limit exceeded"},
                 "capacity"),
    "abort": ({"exited_at": "2026-11-23T05:00:00Z", "exit_code": None, "phase": "aborted", "outcome": "aborted"},
              "aborted"),
    "release": ({"exited_at": "2026-11-23T05:00:00Z", "exit_code": None, "released_as": "failed",
                 "outcome": "confirmed", "class": "failed"}, "failed"),
    "reset": ({"exited_at": "2026-11-23T05:00:00Z", "exit_code": None, "reset_at": "2026-11-23T05:00:00Z"},
              "failed"),
}


@pytest.mark.parametrize("window", [None, 90.0], ids=["window-off", "window-on"])
@pytest.mark.parametrize("end", sorted(EXPLICIT_ENDS))
def test_valid_and_explicit_ends_are_judged_from_their_record(world, end, window):
    fields, expected = EXPLICIT_ENDS[end]
    world["receipt"].update(confirm_window_seconds=window, **fields)
    world["record_after_lock"] = world["die_at_lock"] = False
    world["alive"] = False
    result = launcher.confirm("pm1", "act-0001", Supervisor())
    assert result["outcome"] == expected and world["held"] == []
