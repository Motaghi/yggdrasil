"""Rebuild task 7: records from before the rebuild are upgraded without losing an obligation or an
interlock, the replaced state-root stores are deleted, and settled obligation stamps are compacted."""

from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)
from test_round3 import uncleared
from test_rebuild_t4 import SPAWNED, given


def test_a_legacy_wake_becomes_an_event_and_stays_owed(world):
    from yggdrasil import journal, reducer, state, upgrade
    journal.add("alpha", "wake", node="tl1", reason=state.encode_reason("criteria:WP-1"), wake_id="w-0123456789ab")
    assert upgrade.journal_entries() == 1
    assert not uncleared("wake")
    [event] = uncleared("event", node="tl1")
    assert state.decode_reason(event["reason"]) == "criteria:WP-1"
    assert "criteria:WP-1" in [i["reason"] for i in reducer.owed("tl1")]
    assert upgrade.journal_entries() == 0  # idempotent


def test_a_legacy_hold_becomes_an_owed_launch(world):
    from yggdrasil import journal, reducer, upgrade
    journal.add("alpha", "held-launch", node="tl1", reason="load 0.95")
    upgrade.journal_entries()
    assert not uncleared("held-launch")
    assert "held-launch" in [i["reason"] for i in reducer.owed("tl1")]


def test_a_legacy_pause_without_an_id_keeps_blocking_and_can_be_resumed(world):
    from yggdrasil import commands, interlock, journal, upgrade
    journal.add("alpha", "pause", scope="alpha")  # written before intent ids existed
    assert interlock.paused("alpha")
    upgrade.journal_entries()
    [entry] = uncleared("pause")
    assert entry["intent"].startswith("si-") and interlock.paused("alpha")
    commands.resume("alpha")
    assert not interlock.paused("alpha")


def test_the_replaced_state_root_stores_are_deleted(world):
    from yggdrasil import state, upgrade
    for name in ("wakes.json", "stops.json", "pause", "pause-alpha"):
        (state.root() / name).write_text("{}\n")
    upgrade.state_root_stores()
    assert not [p for p in ("wakes.json", "stops.json", "pause", "pause-alpha") if (state.root() / p).exists()]


def test_compaction_drops_only_consumed_stamps_and_owed_work_is_unchanged(world):
    from yggdrasil import commands, reducer, tracker, upgrade
    commands.tracker_write("pm1", "p1", "work-package", "Build", stage="1", package="WP-9")
    commands.tracker_write("su1", "p1", "criteria", "Test", package="WP-9")
    commands.wp_authorise("p1", "WP-9", "tl1")
    stamps = {s["reason"]: s["id"] for s in tracker.read_issue("p1", "WP-9").header["owed"]}
    authorised = stamps["authorised:WP-9"]  # owed to the paired senior user
    before = {n: [i["id"] for i in items] for n, items in reducer.owed_all().items()}
    given("su1", "act-0042", SPAWNED, ("exited", {"exit_code": 0}), records=[authorised])
    expected = {n: [i for i in ids if i != authorised] for n, ids in before.items()}
    expected = {n: ids for n, ids in expected.items() if ids}
    assert upgrade.compact_stamps() == 1
    left = {s["reason"] for s in tracker.read_issue("p1", "WP-9").header["owed"]}
    assert "authorised:WP-9" not in left and "dispatch:WP-9" in left
    assert {n: [i["id"] for i in items] for n, items in reducer.owed_all().items()} == expected
    assert upgrade.compact_stamps() == 0


def test_a_dispatch_stamp_already_given_is_never_delivered_late(world, monkeypatch):
    from yggdrasil import commands, journal, ratatoskr, reducer, tracker, upgrade
    commands.tracker_write("pm1", "p1", "work-package", "Build", stage="1", package="WP-9")
    commands.tracker_write("su1", "p1", "criteria", "Test", package="WP-9")
    monkeypatch.setattr(ratatoskr, "deliver_dispatches", lambda project_id=None: None)  # crash before sending
    commands.wp_authorise("p1", "WP-9", "tl1")
    monkeypatch.undo()
    [dispatch] = [s["id"] for s in tracker.read_issue("p1", "WP-9").header["owed"]
                  if s["reason"].startswith("dispatch:")]
    given("tl1", "act-0042", SPAWNED, ("exited", {"exit_code": 0}), records=[dispatch])
    ratatoskr.deliver_dispatches()  # late: the lane already ran with the stamp
    assert not [e for e in journal.entries("alpha") if e.get("kind") == "inbox" and e.get("dispatch")]
    assert dispatch not in [i["id"] for i in reducer.owed("tl1")]
    assert upgrade.compact_stamps() >= 1
    assert dispatch not in [s["id"] for s in tracker.read_issue("p1", "WP-9").header.get("owed") or []]


def test_the_tick_compacts_at_most_once_an_hour_and_rehydrate_always(world, monkeypatch):
    from yggdrasil import upgrade
    calls = []
    monkeypatch.setattr(upgrade, "compact_stamps", lambda: calls.append(1) or 0)
    upgrade.run(tick=True)
    upgrade.run(tick=True)
    assert len(calls) == 1
    upgrade.run()  # rehydrate: always
    assert len(calls) == 2
    monkeypatch.setenv("YGG_NOW", "2026-11-16T03:00:01Z")
    upgrade.run(tick=True)
    assert len(calls) == 3
