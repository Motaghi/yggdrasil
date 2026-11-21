"""Unit tests for the round-14 review findings (dev #2 pair review and QA)."""

import shutil


from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (fixture and helpers)
from test_round3 import receipt_file, uncleared


# 2: acknowledgement clears the inbox entry and every wake it covers in one durable write, and drops the
# synthetic recovery wake once no message is left.
def test_acknowledgement_is_one_write(world, monkeypatch):
    from yggdrasil import journal, ratatoskr, state
    msg = ratatoskr.send("pm1", "tl1", "note", "hello")
    writes = []
    real = journal.save
    monkeypatch.setattr(journal, "save", lambda programme, items: writes.append(1) or real(programme, items))
    ratatoskr.inbox("tl1", ack=msg)
    assert len(writes) == 1
    assert not uncleared("inbox", node="tl1") and not uncleared("wake", node="tl1")
    assert "tl1" not in state.wakes()


# 4: a long reason is truncated by characters before encoding; the status is never cut mid-escape.
def test_long_reason_keeps_whole_characters(world):
    from yggdrasil import commands, rehydrate, state
    job = "job/" + "x" * 60
    status = "failed (exit 2) é"
    commands.event_boat("tl1", job, status)
    shutil.rmtree(world / "state")
    assert rehydrate.replay_journal() == 0
    assert "boat:" + job + ":" + status in state.wakes()["tl1"]["reasons"]
    # Round 15: nothing within the 2400-character limit is cut; beyond it, whole characters go.
    assert state.decode_reason(state.encode_reason("é" * 300)) == "é" * 300
    assert state.decode_reason(state.encode_reason("é" * 500)) == "é" * 400


# 5: PM confirmation clears only the checkpoints in its inbox snapshot; a later one keeps its batch entry.
def test_pm_confirmation_clears_only_consumed_checkpoints(world):
    from yggdrasil import events, ratatoskr, upgrade
    from test_rebuild_t4 import SPAWNED, given
    first = ratatoskr.send("tl1", "pm1", "checkpoint", "first", internal=True)  # registers its batch entry
    given("pm1", "act-0042", SPAWNED, ("exited", {"exit_code": 0}), messages=[first])
    second = ratatoskr.send("tl1", "pm1", "checkpoint", "second", internal=True)  # arrives during the run
    upgrade.compact_journal()  # compaction follows the consuming end, never the launch confirmation
    remaining = util.read_json(events.checkpoints_path(), {})["pm1"]
    assert [item["msg_id"] for item in remaining] == [second]
    assert [e.get("msg_id") for e in uncleared("checkpoint", programme="alpha")] == [second]
