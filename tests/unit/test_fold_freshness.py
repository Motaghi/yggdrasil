"""The programme fold is never stale: it is reused only while the journal file's stat key and the authority
actors are exactly what it was made from, and refolded on any other change (review perf2 round 2)."""

import os
from pathlib import Path

import pytest
import yaml

from yggdrasil import commands, journal, reducer, util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


@pytest.fixture
def clean(world):
    journal._PARSED.clear()
    journal._TRUSTED.clear()
    reducer._CACHE.clear()
    return world


def message(number, actor="ygg-app"):
    return {"kind": "inbox", "actor": actor, "at": "2026-11-16T02:00:00Z", "node": "tl1", "sender": "pm1",
            "msg_id": "m-%04d" % number, "message_kind": "note"}


def append_blocks(*entries):
    """An append made outside the journal module."""
    with journal.path_for("alpha").open("a") as handle:
        for entry in entries:
            handle.write("```yaml\n" + util.dump_yaml(entry) + "```\n\n")
        handle.flush()
        os.fsync(handle.fileno())


def assert_fresh():
    trusted, _ = journal.trusted_entries("alpha")
    expected = reducer.fold(trusted)
    actual = reducer.fold_programme("alpha")
    assert actual == expected, {"actual_inbox": actual["inbox"], "expected_inbox": expected["inbox"]}


def set_actors(*actors):
    path = Path(os.environ["YGG_CONFIG"])
    value = yaml.safe_load(path.read_text())
    value["authority"] = {"actors": list(actors)}
    path.write_text(yaml.safe_dump(value))
    clear_caches()
    assert set(commands.authority_actors()) == set(actors)


@pytest.mark.parametrize("operation", ["add", "add_many", "clear", "clear_where", "replace", "save"])
def test_a_write_through_the_module_refreshes_the_fold(clean, operation):
    journal.save("alpha", [message(1), message(2)])
    reducer.fold_programme("alpha")
    fields = message(3)
    fields.pop("kind")
    if operation == "add":
        journal.add("alpha", "inbox", **fields)
    elif operation == "add_many":
        journal.add_many("alpha", [("inbox", fields)])
    elif operation == "clear":
        journal.clear("alpha", "inbox", msg_id="m-0001")
    elif operation == "clear_where":
        journal.clear_where("alpha", lambda e: e.get("msg_id") == "m-0001")
    elif operation == "replace":
        journal.replace("alpha", "inbox", {"msg_id": "m-0001"}, **fields)
    else:
        journal.save("alpha", [message(3), message(2)])
    assert_fresh()


def test_an_external_append_with_a_new_last_entry_refreshes_the_fold(clean):
    journal.save("alpha", [message(1)])
    reducer.fold_programme("alpha")
    append_blocks(message(2))
    assert_fresh()


def test_an_external_append_that_repeats_the_last_entry_refreshes_the_fold(clean):
    journal.save("alpha", [message(1)])
    reducer.fold_programme("alpha")
    before = journal.file_key("alpha")
    append_blocks(message(2), message(1))  # the same revision and the same last entry as before
    assert journal.file_key("alpha") != before
    assert "m-0002" in {e["msg_id"] for e in journal.trusted_entries("alpha")[0]}
    assert_fresh()


@pytest.mark.parametrize("operation", ["replace", "inplace"])
@pytest.mark.parametrize("which", ["first", "last"])
def test_an_edit_keeping_size_and_mtime_refreshes_the_fold(clean, operation, which):
    journal.save("alpha", [message(1), message(2)])
    reducer.fold_programme("alpha")
    target = journal.path_for("alpha")
    before = target.stat()
    before_key = journal.file_key("alpha")
    old, new = ("m-0001", "m-0003") if which == "first" else ("m-0002", "m-0004")
    text = target.read_text().replace(old, new)
    if operation == "replace":
        temporary = target.with_suffix(".replacement")
        temporary.write_text(text)
        os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(temporary, target)
    else:
        target.write_text(text)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert target.stat().st_size == before.st_size
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    assert journal.file_key("alpha") != before_key
    assert new in {e["msg_id"] for e in journal.trusted_entries("alpha")[0]}
    assert_fresh()


@pytest.mark.parametrize("direction", ["grant", "revoke"])
def test_an_authority_change_refreshes_an_entry_that_is_not_the_last(clean, direction):
    set_actors("user", *(["delegate"] if direction == "revoke" else []))
    journal.save("alpha", [message(1, "delegate"), message(2)])
    before = reducer.fold_programme("alpha")
    assert ("m-0001" in before["inbox"]) == (direction == "revoke")
    set_actors("user", *(["delegate"] if direction == "grant" else []))
    assert_fresh()


def test_a_missing_and_recreated_journal_refreshes_the_fold(clean):
    journal.save("alpha", [message(1)])
    reducer.fold_programme("alpha")
    journal.path_for("alpha").unlink()
    assert reducer.fold_programme("alpha")["inbox"] == {}
    journal.save("alpha", [message(2)])
    assert_fresh()


def test_an_unchanged_fold_does_not_read_the_journal(clean, monkeypatch):
    journal.save("alpha", [message(1)])
    before = reducer.fold_programme("alpha")

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected journal read")
    monkeypatch.setattr(journal, "keyed_snapshot", forbidden)
    monkeypatch.setattr(util, "read_text", forbidden)
    assert reducer.fold_programme("alpha") is before


@pytest.mark.parametrize("mode", ["0", "1"])
@pytest.mark.parametrize("reader", ["entries", "snapshot", "trusted_entries", "keyed_snapshot"])
def test_public_readers_detach_yaml_graphs(clean, monkeypatch, mode, reader):
    monkeypatch.setenv("YGG_TEST_MODE", mode)
    target = journal.path_for("alpha")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nrevision: 1\n---\n```yaml\nkind: chain-reset\nactor: ygg-app\nnode: tl1\n"
                      "metadata: !!omap [{a: [original]}]\ncycle: &x [*x]\n```\n")
    result = getattr(journal, reader)("alpha")
    values = {"entries": lambda: result, "snapshot": lambda: result[1], "trusted_entries": lambda: result[0],
              "keyed_snapshot": lambda: result[2]}[reader]()
    values[0]["metadata"][0][1].append("changed")
    values[0]["cycle"].append("changed")
    fresh = journal.entries("alpha")[0]
    assert fresh["metadata"] == [("a", ["original"])]
    assert len(fresh["cycle"]) == 1 and fresh["cycle"][0] is fresh["cycle"]


def test_the_pickle_guard_detects_nested_and_cyclic_mutation(clean):
    target = journal.path_for("alpha")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nrevision: 1\n---\n```yaml\nkind: chain-reset\nactor: ygg-app\nnode: tl1\n"
                      "metadata: !!omap [{a: [original]}]\ncycle: &x [*x]\n```\n")
    journal.entries("alpha")
    cached = journal._PARSED[os.path.abspath(target)][2]
    cached[0]["metadata"][0][1].append("changed")
    cached[0]["cycle"].append("changed")
    with pytest.raises(RuntimeError, match="changed cached journal"):
        journal.entries("alpha")


TAIL = {"kind": "counter", "actor": "ygg-app", "value": 1, "at": "2026-11-16T02:00:00Z"}


def test_an_externally_added_message_is_a_candidate_at_once(clean):
    from yggdrasil import conductor, status
    journal.save("alpha", [TAIL])
    reducer.fold_programme("alpha")
    append_blocks(message(1), TAIL)
    with util.locked():
        warm_candidates = conductor.candidates()
        node = next(n for n in status.build_status()["nodes"] if n["id"] == "tl1")
        reducer._CACHE.clear()
        cold_candidates = conductor.candidates()
    assert "tl1" in cold_candidates
    assert warm_candidates == cold_candidates, {"warm": warm_candidates, "cold": cold_candidates,
                                                "warm_state": node["state"], "held_reason": node["held_reason"]}


def test_an_externally_consumed_message_is_not_owed_again(clean):
    from yggdrasil import conductor
    journal.save("alpha", [message(1), TAIL])
    reducer.fold_programme("alpha")
    base = {"kind": "activation", "actor": "ygg-app", "at": "2026-11-16T02:00:00Z", "node": "tl1",
            "key": "act-000099"}
    append_blocks(dict(base, phase="reserved", work={"messages": ["m-0001"]}), dict(base, phase="spawned"),
                  dict(base, phase="exited", exit_code=0), TAIL)
    with util.locked():
        warm_owed = reducer.owed("tl1")
        warm_candidates = conductor.candidates()
        reducer._CACHE.clear()
        cold_owed = reducer.owed("tl1")
        cold_candidates = conductor.candidates()
    assert not cold_owed and "tl1" not in cold_candidates
    assert warm_owed == cold_owed, {"warm_owed": warm_owed, "cold_owed": cold_owed,
                                    "warm_candidates": warm_candidates, "cold_candidates": cold_candidates}
