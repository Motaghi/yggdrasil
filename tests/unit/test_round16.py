"""Unit tests for the round-16 review findings: durable checkpoint batches, batch-wake retirement on
acknowledgement, load holds with retry lineage, and oversized Boat reasons."""

import itertools
import shutil
from collections import Counter

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (fixture and helpers)
from test_round3 import receipt_file, uncleared
from test_round12 import CAPS, launches


def settle(world):
    """Run ticks until the setup wakes are spent, then start a fresh launch log."""
    from yggdrasil import conductor
    for _ in range(6):
        before = sum(launches(world).values())
        conductor.tick()
        if sum(launches(world).values()) == before:
            break
    (world / "agent.log").unlink(missing_ok=True)


# Property: a checkpoint to a PM (delivered, possibly acknowledged before or after a held tick, under
# every load, cap and recovery pattern) launches the PM exactly once if unread at launch time, else never.
@pytest.mark.parametrize("load,cap,recovery,ack", list(itertools.product(
    ["low", "high-then-low"], list(CAPS), ["none", "once", "every-tick"], ["none", "before", "after-first-tick"])))
def test_checkpoint_delivery_ack_and_replay_launch_the_pm_at_most_once(world, load, cap, recovery, ack):
    from yggdrasil import conductor, ratatoskr, state
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nload_max_age_seconds: 999999999\n"
                                       "checkpoint_batch_hours: 0\n" + CAPS[cap])
    clear_caches()
    settle(world)
    msg = ratatoskr.send("tl1", "pm1", "checkpoint", "WP-1 halfway")
    if ack == "before":
        ratatoskr.inbox("pm1", ack=msg)
    (world / "load").write_text("0.95" if load == "high-then-low" else "0.1")
    conductor.tick()
    if ack == "after-first-tick":
        ratatoskr.inbox("pm1", ack=msg)
    if load == "high-then-low":
        (world / "load").write_text("0.1")
        conductor.tick()
    for number in range(6):
        if recovery == "every-tick" or (recovery == "once" and number == 0):
            shutil.rmtree(world / "state")
        conductor.tick()
    launched_before_ack = ack == "none" or (ack == "after-first-tick" and load == "low")
    assert launches(world) == (Counter({"pm1": 1}) if launched_before_ack else Counter())
    assert "pm1" not in state.wakes()
    from yggdrasil import upgrade
    upgrade.compact_journal()  # compaction of what the launches consumed (hourly on the tick)
    assert not uncleared("checkpoint")


# 1: the batch entry is journalled in the same write as the checkpoint's inbox entry.
def test_checkpoint_delivery_journals_inbox_and_batch_in_one_write(world, monkeypatch):
    from yggdrasil import events, journal, ratatoskr
    writes = []
    real = journal.save
    monkeypatch.setattr(journal, "save", lambda programme, items: writes.append(
        sorted(e["kind"] for e in items if not e.get("cleared_at") and e.get("kind") in ("inbox", "checkpoint")))
        or real(programme, items))
    monkeypatch.setattr(events, "note_checkpoint", lambda *a, **k: pytest.fail("no separate registration"))
    msg = ratatoskr.send("tl1", "pm1", "checkpoint", "WP-1 halfway")
    assert writes == [["checkpoint", "inbox"]]
    assert [i["msg_id"] for i in util.read_json(events.checkpoints_path(), {})["pm1"]] == [msg]


# 1: a batch consumed by a confirmed PM activation never comes back, even when its journal clear was lost.
def test_consumed_batch_is_never_restored(world):
    from yggdrasil import events, journal, ratatoskr, rehydrate
    msg = ratatoskr.send("tl1", "pm1", "checkpoint", "WP-1 halfway")
    from yggdrasil import upgrade
    from test_rebuild_t4 import SPAWNED, given
    given("pm1", "act-0042", SPAWNED, ("exited", {"exit_code": 0}), messages=[msg])
    upgrade.compact_journal()
    assert not uncleared("checkpoint")
    for _ in range(3):
        shutil.rmtree(world / "state")
        rehydrate.replay_journal()
        assert "pm1" not in (util.read_json(events.checkpoints_path(), {}) or {})
    # A lost clear: the entry is still on record, but the receipt on record says it was consumed.
    receipt_file("act-0042", node="pm1", outcome="confirmed", phase="done", pid=999999, process_started_at="x",
                 exited_at=util.iso(), exit_code=0, consumed={"messages": [msg], "wakes": [], "wake_ids": []})
    journal.add("alpha", "checkpoint", pm="pm1", lane="tl1", msg_id=msg)
    events.checkpoints_path().unlink(missing_ok=True)
    rehydrate.replay_journal()
    assert "pm1" not in (util.read_json(events.checkpoints_path(), {}) or {})


# 1: a legacy unread checkpoint with no batch entry (the round-15 upgrade) is not repaired locally, so
# repeated recovery and ticks never relaunch the PM for it.
def test_legacy_checkpoint_without_batch_entry_does_not_relaunch_the_pm(world):
    from yggdrasil import conductor, journal
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\ncheckpoint_batch_hours: 0\n")
    clear_caches()
    settle(world)
    journal.add("alpha", "inbox", node="pm1", msg_id="m-0099", sender="tl1", message_kind="checkpoint",
                body="legacy", provenance={"type": "agent", "origin": "tl1"})
    for _ in range(4):
        shutil.rmtree(world / "state")
        conductor.tick()
    assert launches(world)["pm1"] == 0


# 2: acknowledging the last checkpoint retires the queued batch wake in the same write; replay keeps it gone.
def test_ack_of_last_checkpoint_clears_its_batch_wake(world, monkeypatch):
    from yggdrasil import journal, ratatoskr, rehydrate, state
    msg = ratatoskr.send("tl1", "pm1", "checkpoint", "WP-1 halfway")
    assert "checkpoints:" + msg in state.wakes()["pm1"]["reasons"]
    writes = []
    real = journal.save
    monkeypatch.setattr(journal, "save", lambda programme, items: writes.append(1) or real(programme, items))
    ratatoskr.inbox("pm1", ack=msg)
    assert len(writes) == 1
    assert "checkpoints:" + msg not in (state.wakes().get("pm1") or {}).get("reasons", [])
    assert not [e for e in uncleared("event", node="pm1") if state.decode_reason(e["reason"]).startswith("checkpoints")]
    assert not uncleared("checkpoint")
    shutil.rmtree(world / "state")
    rehydrate.replay_journal()
    assert "checkpoints:" + msg not in (state.wakes().get("pm1") or {}).get("reasons", [])


# 2: acknowledging one checkpoint while another is still batched keeps the other report's wake (round 17:
# each report owns its wake).
def test_ack_of_one_checkpoint_keeps_the_batch_wake_for_the_rest(world):
    from yggdrasil import ratatoskr, state
    first = ratatoskr.send("tl1", "pm1", "checkpoint", "first")
    second = ratatoskr.send("su1", "pm1", "checkpoint", "second")
    ratatoskr.inbox("pm1", ack=first)
    reasons = state.wakes()["pm1"]["reasons"]
    assert "checkpoints:" + second in reasons and "checkpoints:" + first not in reasons
    assert len(uncleared("checkpoint")) == 1


# 3: acknowledging the last unread message keeps a load hold that backs another outstanding wake.
def test_ack_keeps_a_load_hold_backing_other_work(world):
    from yggdrasil import journal, ratatoskr, state
    state.wake("tl1", "stage-approved")
    journal.add("alpha", "held-launch", node="tl1", reason="load 0.95")
    state.set_state("tl1", "held-load", held="load 0.95")
    msg = ratatoskr.send("pm1", "tl1", "note", "unrelated")
    ratatoskr.inbox("tl1", ack=msg)
    assert uncleared("held-launch", node="tl1")
    assert uncleared("event", node="tl1")
    assert "stage-approved" in state.wakes()["tl1"]["reasons"]


# 4: an oversized Boat event is refused at the command, with nothing queued or journalled.
def test_oversized_boat_reason_is_refused(world):
    from yggdrasil import commands, state
    with pytest.raises(util.YggError, match="too long"):
        commands.event_boat("tl1", "x" * 2400, "done")
    assert "tl1" not in state.wakes() and not uncleared("event", node="tl1")
    commands.event_boat("tl1", "x" * 2380, "done")
    assert state.decode_reason(uncleared("event", node="tl1")[0]["reason"]).endswith(":done")
