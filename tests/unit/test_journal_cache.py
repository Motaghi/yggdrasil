"""Journal reads: each file is parsed at most once while it is provably unchanged, and every change is seen."""

import os
import time

import pytest

from yggdrasil import journal, util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


def _activation_entries(count):
    items, number = [], 0
    while len(items) < count:
        number += 1
        key = "act-%06d" % number
        base = {"kind": "activation", "at": "2026-11-16T02:00:00Z", "actor": "ygg-app", "key": key, "node": "tl1"}
        items.append(dict(base, phase="reserved", retries_used=0, work={"messages": [], "retries": []}))
        items.append(dict(base, phase="spawned"))
        items.append(dict(base, phase="exited", exit_code=1 if number % 5 == 0 else 0))
    return items[:count]


def test_status_on_a_2000_entry_journal_is_fast(world):
    from yggdrasil import status
    journal.save("alpha", _activation_entries(2000))
    started = time.perf_counter()
    status.build_status()
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, elapsed


def test_a_write_through_the_module_is_seen(world):
    journal.save("alpha", _activation_entries(3))
    assert len(journal.entries("alpha")) == 3
    journal.add("alpha", "chain-reset", node="tl1")
    assert [e["kind"] for e in journal.entries("alpha")][-1] == "chain-reset"
    assert len(journal.trusted_entries("alpha")[0]) == 4


def test_an_external_append_is_seen(world):
    journal.save("alpha", _activation_entries(3))
    assert len(journal.entries("alpha")) == 3
    with open(journal.path_for("alpha"), "a") as handle:  # another process appending a block
        handle.write("```yaml\nkind: chain-reset\nnode: tl1\nactor: ygg-app\nat: '2026-11-16T02:00:00Z'\n```\n\n")
    assert len(journal.entries("alpha")) == 4
    assert len(journal.trusted_entries("alpha")[0]) == 4


def test_an_external_replace_with_the_same_size_and_time_is_seen(world):
    journal.save("alpha", _activation_entries(3))
    path = journal.path_for("alpha")
    before = os.stat(path)
    assert journal.entries("alpha")[0]["key"] == "act-000001"
    text = path.read_text().replace("act-000001", "act-000009")
    replacement = path.with_name("journal.md.new")
    replacement.write_text(text)
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(replacement, path)  # a new inode, same size and modification time
    assert os.stat(path).st_size == before.st_size and os.stat(path).st_mtime_ns == before.st_mtime_ns
    assert journal.entries("alpha")[0]["key"] == "act-000009"


def test_a_change_made_before_a_lock_is_taken_is_seen_inside_it(world):
    journal.save("alpha", _activation_entries(3))
    assert len(journal.entries("alpha")) == 3
    with open(journal.path_for("alpha"), "a") as handle:
        handle.write("```yaml\nkind: chain-reset\nnode: tl1\nactor: ygg-app\nat: '2026-11-16T02:00:00Z'\n```\n\n")
    with util.locked():
        assert len(journal.entries("alpha")) == 4


def test_entries_are_a_private_copy(world):
    journal.save("alpha", _activation_entries(3))
    mine = journal.entries("alpha")
    mine[0]["cleared_at"] = "2026-11-16T03:00:00Z"
    mine[0]["work"]["messages"].append("m-0001")
    mine.append({"kind": "chain-reset"})
    fresh = journal.entries("alpha")
    assert len(fresh) == 3 and "cleared_at" not in fresh[0] and fresh[0]["work"]["messages"] == []


def test_a_missing_journal_reads_as_empty_and_its_creation_is_seen(world):
    assert journal.entries("alpha") == []
    journal.add("alpha", "chain-reset", node="tl1")
    assert len(journal.entries("alpha")) == 1


HEADER = "---\nschema: ygg.journal.v1\nprogramme: alpha\nrevision: 1\n---\n"


def _raw_journal(*blocks):
    path = journal.path_for("alpha")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "".join("```yaml\n" + block + "```\n\n" for block in blocks))


STOP = "scope: '*'\nintent: si-0123456789abcdef\nactor: ygg-app\nat: '2026-11-16T02:00:00Z'\n"


def test_a_tab_after_a_key_is_refused_as_before(world):
    _raw_journal("kind:\tstop\n" + STOP)
    assert journal.entries("alpha") == []
    assert journal.trusted_entries("alpha") == ([], 0)


def test_a_yaml_directive_block_is_read_as_before(world):
    _raw_journal("%YAML 1.3\n---\nkind: stop\n" + STOP)
    assert [e["kind"] for e in journal.trusted_entries("alpha")[0]] == ["stop"]


def test_a_change_to_a_snapshot_never_reaches_later_reads(world):
    journal.save("alpha", _activation_entries(3))
    journal.snapshot("alpha")[1][0]["node"] = "pm1"
    assert journal.entries("alpha")[0]["node"] == "tl1"
    assert journal.snapshot("alpha")[1][0]["node"] == "tl1"


def test_a_change_to_trusted_entries_never_reaches_later_reads(world):
    journal.save("alpha", _activation_entries(3))
    journal.trusted_entries("alpha")[0][0]["node"] = "pm1"
    assert journal.entries("alpha")[0]["node"] == "tl1"
    assert journal.trusted_entries("alpha")[0][0]["node"] == "tl1"


OMAP = "kind: chain-reset\nnode: tl1\nactor: ygg-app\nat: '2026-11-16T02:00:00Z'\nmetadata: !!omap [{a: [original]}]\n"
CYCLE = "kind: chain-reset\nnode: tl1\nactor: ygg-app\nat: '2026-11-16T02:00:00Z'\nmetadata: &cycle [*cycle]\n"


def test_lists_inside_an_ordered_map_are_detached_from_entries(world):
    _raw_journal(OMAP)
    journal.entries("alpha")[0]["metadata"][0][1].append("changed")
    assert journal.entries("alpha")[0]["metadata"] == [("a", ["original"])]


def test_lists_inside_an_ordered_map_are_detached_from_snapshots(world):
    _raw_journal(OMAP)
    journal.snapshot("alpha")[1][0]["metadata"][0][1].append("changed")
    journal.trusted_entries("alpha")[0][0]["metadata"][0][1].append("changed")
    assert journal.snapshot("alpha")[1][0]["metadata"] == [("a", ["original"])]


def test_a_recursive_alias_is_copied_with_its_cycle(world):
    _raw_journal(CYCLE)
    first = journal.entries("alpha")[0]["metadata"]
    assert first[0] is first
    again = journal.entries("alpha")[0]["metadata"]
    assert again[0] is again and again is not first


def test_a_recursive_alias_passes_the_cache_guard(world):
    _raw_journal(CYCLE)
    for _ in range(3):  # cache hits in test mode re-check the parse; a cycle must not break that
        snap = journal.snapshot("alpha")[1][0]["metadata"]
        trusted = journal.trusted_entries("alpha")[0][0]["metadata"]
        assert snap[0] is snap and trusted[0] is trusted
