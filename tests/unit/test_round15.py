"""Unit tests for the round-15 review findings (dev #2 pair review and QA)."""

import shutil

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (fixture and helpers)
from test_round3 import receipt_file, uncleared


# 2: a checkpoint acknowledged by the PM during the run leaves the batch; confirmation does not keep it.
def test_checkpoint_acked_during_the_run_leaves_the_batch(world):
    from yggdrasil import events, ratatoskr, upgrade
    from test_rebuild_t4 import SPAWNED, given
    first = ratatoskr.send("tl1", "pm1", "checkpoint", "first", internal=True)
    given("pm1", "act-0042", SPAWNED, ("exited", {"exit_code": 0}), messages=[first])
    second = ratatoskr.send("tl1", "pm1", "checkpoint", "second", internal=True)
    ratatoskr.inbox("pm1", ack=second)  # read during the run
    upgrade.compact_journal()
    assert "pm1" not in util.read_json(events.checkpoints_path(), {})
    assert not uncleared("checkpoint", programme="alpha")


# 2: `ygg send --kind checkpoint` registers batching exactly like the tracker path. Round 16: rehydrate
# restores batches from the journal only, so an inbox-only legacy checkpoint is not repaired locally.
def test_send_checkpoint_registers_and_rehydrate_repairs_batching(world, monkeypatch):
    from yggdrasil import events, journal, ratatoskr, rehydrate
    msg = ratatoskr.send("tl1", "pm1", "checkpoint", "WP-1 is halfway complete")
    batch = util.read_json(events.checkpoints_path(), {})["pm1"]
    assert [(i["lane"], i["msg_id"]) for i in batch] == [("tl1", msg)]
    journal.clear_where("alpha", lambda e: e.get("kind") == "checkpoint")
    other = "m-0099"
    journal.add("alpha", "inbox", node="pm1", msg_id=other, sender="tl1", message_kind="checkpoint", body="later",
                provenance={"type": "agent", "origin": "tl1"})
    shutil.rmtree(world / "state")
    rehydrate.replay_journal()
    assert "pm1" not in (util.read_json(events.checkpoints_path(), {}) or {})


# 4: an ASCII reason that round-tripped before (a 200-character job plus status) still round-trips.
def test_previous_ascii_reasons_still_round_trip(world):
    from yggdrasil import commands, rehydrate, state
    job = "x" * 200
    commands.event_boat("tl1", job, "done")
    shutil.rmtree(world / "state")
    assert rehydrate.replay_journal() == 0
    assert "boat:" + job + ":done" in state.wakes()["tl1"]["reasons"]
