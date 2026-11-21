"""Rebuild task 2: the reducer (a pure fold of journal facts plus a view over a fresh evidence snapshot),
running alongside today's readers."""

import json

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


def test_fold_is_pure_and_deterministic():
    from yggdrasil import reducer
    entries = [
        {"kind": "inbox", "node": "tl1", "msg_id": "m-0001", "actor": "ygg-app"},
        {"kind": "inbox", "node": "tl1", "msg_id": "m-0002", "actor": "ygg-app", "cleared_at": "x"},
        {"kind": "pause", "scope": "alpha", "intent": "si-0000000000000001", "actor": "ygg-app"},
        {"kind": "resume", "scope": "alpha", "clears": ["si-0000000000000001"], "actor": "ygg-app"},
    ]
    snapshot = json.dumps(entries, sort_keys=True)
    first, second = reducer.fold(entries), reducer.fold(entries)
    assert first == second
    assert json.dumps(entries, sort_keys=True) == snapshot  # the input is never modified
    assert list(first["inbox"]) == ["m-0001"]
    assert first["resumed"] == {"si-0000000000000001"}


def test_fold_cache_is_keyed_by_length_and_last_entry(world, monkeypatch):
    from yggdrasil import ratatoskr, reducer
    calls = []
    real = reducer.fold
    monkeypatch.setattr(reducer, "fold", lambda entries: calls.append(len(entries)) or real(entries))
    reducer._CACHE.clear()
    reducer.fold_programme("alpha")
    reducer.fold_programme("alpha")
    assert len(calls) == 1  # unchanged journal: folded once
    ratatoskr.send("pm1", "tl1", "note", "hello")
    reducer.fold_programme("alpha")
    assert len(calls) == 2  # appended journal: folded again


def test_view_reads_markers_from_the_fresh_snapshot(world):
    from yggdrasil import commands, reducer
    before = reducer.read()
    assert before["paused"]["programmes"] == []
    commands.pause("alpha")
    after = reducer.read()
    assert after["paused"]["programmes"] == ["alpha"]


def test_shadow_view_matches_today_through_a_normal_flow(world):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from yggdrasil import commands, ratatoskr, reducer, status
    ratatoskr.send("pm1", "tl1", "note", "hello")
    assert reducer.shadow_diff(status.build_status()) == {}
    subprocess.run([str(Path(sys.executable).parent / "ygg"), "test", "activation", "su1", "--outcome", "uncertain"],
                   env=dict(os.environ), capture_output=True, text=True)
    assert reducer.shadow_diff(status.build_status()) == {}
    commands.pause("alpha")
    assert reducer.shadow_diff(status.build_status()) == {}
    commands.resume("alpha")
    assert reducer.shadow_diff(status.build_status()) == {}


def test_shadow_log_records_a_disagreement(world, monkeypatch):
    from yggdrasil import reducer, status
    log = world / "shadow.jsonl"
    monkeypatch.setenv("YGG_SHADOW_LOG", str(log))
    status.build_status()
    assert not log.exists()  # agreement writes nothing
    real = status.build_status()
    monkeypatch.setattr(reducer, "read", lambda: dict(reducer.view(reducer.folds(), reducer.evidence()),
                                                       paused={"global": True, "programmes": []}))
    reducer.shadow_log(real)
    record = json.loads(log.read_text().splitlines()[-1])
    assert "paused" in record["diff"]


def test_shadow_log_is_inert_in_managed_sessions(world, monkeypatch):
    from yggdrasil import reducer
    log = world / "shadow.jsonl"
    monkeypatch.setenv("YGG_SHADOW_LOG", str(log))
    monkeypatch.setenv("YGG_NODE", "tl1")
    (world / "state" / "pause-alpha").write_text(util.iso() + "\n")
    reducer.shadow_log({"paused": {"global": False, "programmes": ["alpha"]}})
    assert not log.exists()


# Review round 1: clearing an entry that is not the last one refreshes the fold.
def test_fold_cache_sees_a_clear_in_the_middle(world):
    from yggdrasil import journal, ratatoskr, reducer
    first = ratatoskr.send("pm1", "tl1", "note", "one")
    ratatoskr.send("pm1", "tl1", "note", "two")
    assert first in reducer.fold_programme("alpha")["inbox"]
    journal.clear("alpha", "inbox", msg_id=first)  # not the last entry
    assert first not in reducer.fold_programme("alpha")["inbox"]


def test_journal_revision_advances_on_every_save(world):
    from yggdrasil import journal
    before = journal.revision("alpha")
    journal.add("alpha", "counter", value=1)
    assert journal.revision("alpha") == before + 1


# A due liveness retry is held-load only while admission refuses on the current load; otherwise it is
# queued. Nothing about the decision is stored. The shadow view agrees with status in both states.
def _pending_retry(world, held: bool):
    from yggdrasil import state
    from test_round3 import receipt_file
    receipt_file("act-0042", pid=999999, outcome="confirmed", exited_at="2026-11-16T00:00:01Z", exit_code=1,
                 started_at="2026-11-16T00:00:00Z", retries_used=0)
    state.save_runtime("tl1", {"state": "running", "activation": "act-0042", "outcome": "confirmed"})
    (world / "load").write_text("0.95" if held else "0.1")


def test_shadow_agrees_on_a_load_held_retry(world):
    from yggdrasil import reducer, status
    _pending_retry(world, held=True)
    view = status.build_status()
    assert "tl1" in view["held_launches"]
    assert reducer.shadow_diff(view) == {}


def test_shadow_agrees_on_a_cap_deferred_retry(world):
    from yggdrasil import reducer, status
    _pending_retry(world, held=False)
    view = status.build_status()
    assert "tl1" not in view["held_launches"]
    assert reducer.shadow_diff(view) == {}


# Review round 3: the retry's recorded decision decides, not the load right now.


# Review round 3: revision and entries come from one read of the journal.
def test_journal_snapshot_reads_revision_and_entries_together(world, monkeypatch):
    from yggdrasil import journal, ratatoskr, reducer, util as yutil
    ratatoskr.send("pm1", "tl1", "note", "one")
    reads = []
    real = yutil.read_text
    monkeypatch.setattr(yutil, "read_text", lambda path, default="": reads.append(str(path)) or real(path, default))
    reducer._CACHE.clear()
    journal._PARSED.clear()  # a parse cached by the send would otherwise answer without reading at all
    reducer.fold_programme("alpha")
    journal_reads = [r for r in reads if r.endswith("journal.md")]
    assert len(journal_reads) == 1
