"""Pilot 2, 20 Nov (Linear tracker): Linear reports a guarded document under a newer version than the one its update
answered with, so the next guarded writer that had not refreshed in between was refused ("changed in Linear since it
was read"). The supervisor's gate is such a writer: it stopped with its error sent nowhere, and the launch was held
as uncertain with no trace of why."""

import json
import sys
import time

import pytest

from yggdrasil import util
from test_round2 import world  # noqa: F401  (shared fixture)

JOURNAL = "initiatives/alpha/documents/journal.md"
COUNTERS = "_ygg/counters.json"
ENTRY_1 = {"kind": "event", "at": "2026-11-16T01:00:00Z", "actor": "ygg-app", "event_id": "e-000000000001",
           "node": "tl1", "reason": "test"}
ENTRY_2 = {"kind": "event", "at": "2026-11-16T01:05:00Z", "actor": "ygg-app", "event_id": "e-000000000002",
           "node": "tl1", "reason": "test"}


def journal_text(entries, revision):
    body = "".join("```yaml\n" + util.dump_yaml(e) + "```\n\n" for e in entries)
    return util.render_frontmatter({"schema": "ygg.journal.v1", "programme": "alpha", "revision": revision}, body)


@pytest.fixture
def moving(world, monkeypatch):
    """The Linear tracker over a scripted Linear holding the programme journal (`doc-j`) and the counters (`doc-c`),
    both written by the app and cached as Linear holds them. Every update is answered with one version and Linear
    reports another from then on, as the pilot showed."""
    from yggdrasil import tracker
    from yggdrasil.linear import adapter
    docs, updates = {}, []

    def gql(operation, query, variables=None):
        if operation == "YggViewer":
            return {"viewer": {"id": "app"}}
        assert operation == "YggDocumentUpdate", operation
        updates.append(variables["id"])
        docs[variables["id"]].update(content=variables["input"]["content"], updatedAt="moved-" + str(len(updates)))
        return {"documentUpdate": {"success": True, "document": {"id": variables["id"], "title": "t",
                                                                 "updatedAt": "answered-" + str(len(updates))}}}

    monkeypatch.setattr(adapter, "gql", gql)
    monkeypatch.setattr(adapter, "_read_document", lambda identifier: dict(docs[identifier], id=identifier))
    monkeypatch.setattr(adapter, "initiative_id", lambda name: "init-1")
    monkeypatch.setattr(tracker, "selected", lambda: "linear")
    monkeypatch.setattr(tracker, "root", lambda: adapter.cache_root())
    adapter._remember(**{"viewer": "app", "project:_ygg-control": "ctl-1", "document:init-1:journal": "doc-j",
                         "document:ctl-1:" + COUNTERS: "doc-c"})
    for identifier, relative, text in (("doc-j", JOURNAL, journal_text([ENTRY_1], 1)),
                                       ("doc-c", COUNTERS, '{\n  "activation": 7\n}\n')):
        util.atomic_write(adapter.cache_root() / relative, text)
        docs[identifier] = {"content": adapter.encode(text), "updatedAt": "v0", "creator": {"id": "app"},
                            "updatedBy": {"id": "app"}}
        adapter._remember(**{"guarded:" + identifier: relative, "path:" + identifier: relative,
                             "updated:" + identifier: "v0"})
    return adapter, docs, updates


# (a) Two journal writers in sequence under the journal lock (the conductor's reservation, then the supervisor's
# gate), with no refresh in between: the second one is written, not refused.
def test_a_second_journal_writer_goes_on_after_linear_moved_the_version(moving):
    adapter, docs, updates = moving
    from yggdrasil import journal
    with util.locked():
        journal.add("alpha", "event", event_id="e-000000000011", node="tl1", reason="first")
        journal.add("alpha", "event", event_id="e-000000000012", node="tl1", reason="second")
    assert updates == ["doc-j", "doc-j"]
    held = adapter.decode(docs["doc-j"]["content"])
    assert "e-000000000001" in held and "e-000000000011" in held and "e-000000000012" in held
    assert held == util.read_text(adapter.cache_root() / JOURNAL).rstrip("\n") + "\n"
    assert adapter._known("updated:doc-j") == "answered-2"


# (a) The counters allocation the conductor was refused at 17:14:17, one allocation after the last.
def test_the_next_counter_allocation_goes_on_after_linear_moved_the_version(moving):
    adapter, docs, updates = moving
    from yggdrasil import tracker
    assert tracker.allocate("activation") == "act-0008"
    assert tracker.allocate("activation") == "act-0009"
    assert updates == ["doc-c", "doc-c"]
    assert json.loads(adapter.decode(docs["doc-c"]["content"]))["activation"] == 9


# (c) A moved version is written over only when Linear holds exactly what the cache holds and the app (or an
# authority actor) changed it last: another user's change, or content the cache never saw, is never written over.
@pytest.mark.parametrize("change", ["another user", "extended"])
def test_a_moved_version_by_another_user_or_with_unseen_content_is_never_written_over(moving, change):
    adapter, docs, updates = moving
    from yggdrasil import journal, state
    before = util.read_text(adapter.cache_root() / JOURNAL)
    if change == "another user":
        docs["doc-j"].update(updatedAt="moved-x", updatedBy={"id": "someone"})  # the same content
    else:
        docs["doc-j"].update(updatedAt="moved-x", content=adapter.encode(journal_text([ENTRY_1, ENTRY_2], 2)))
    with pytest.raises(util.YggError) as caught:
        with util.locked():
            journal.add("alpha", "event", event_id="e-000000000013", node="tl1", reason="third")
    assert updates == []
    if change == "another user":
        assert not isinstance(caught.value, adapter.Changed) and "another Linear user" in caught.value.message
        assert util.read_text(adapter.cache_root() / JOURNAL) == before
        assert [a for a in state.alerts() if a["kind"] == "ygg-linear-foreign"]
        assert adapter._known("updated:doc-j") == "v0"
    else:
        assert isinstance(caught.value, adapter.Changed) and "since it was read" in caught.value.message
        assert "e-000000000002" in util.read_text(adapter.cache_root() / JOURNAL)  # adopted for the re-run


def _reservation(world) -> str:
    """A reserved, starting headless activation of tl1, ready for its supervisor's gate."""
    from yggdrasil import launcher, lifecycle, state, tracker
    activation = tracker.allocate("activation")
    me = lifecycle.own_identity()
    lifecycle.commit("tl1", activation, "reserved", launcher_pid=me["pid"], launcher_started_at=me["started_at"],
                     work={})
    launcher.save_receipt(activation, {
        "node": "tl1", "activation": activation, "launcher": "headless", "lifecycle": True, "outcome": "pending",
        "phase": "starting", "pid": None, "cwd": str(world), "settings": "s.json", "model": "claude-opus-5-5",
        "effort": "high", "cli": "claude-code", "programme": "alpha", "project": "p1", "role": "tl", "pinned": {}})
    state.save_runtime("tl1", {"state": "running", "activation": activation, "outcome": "pending"})
    return activation


def _spawned(activation) -> list:
    from yggdrasil import lifecycle
    return [e for e in lifecycle.facts("alpha", activation) if e.get("phase") == "spawned"]


def _agent_runs(world) -> int:
    log = world / "agent.log"
    return len(log.read_text().splitlines()) if log.exists() else 0


# (c) The gate retries only the refusal of a newer, adopted journal, and at most GATE_ATTEMPTS times; any other
# refusal (here another user's change) ends the supervisor at once, with its reason on standard error.
@pytest.mark.parametrize("refusal", ["foreign", "changed every time"])
def test_the_gate_retries_only_an_adopted_change_and_a_bounded_number_of_times(world, monkeypatch, capsys, refusal):
    from yggdrasil import launcher, lifecycle
    from yggdrasil.linear import adapter
    activation = _reservation(world)
    real, calls = lifecycle.commit, []

    def commit(node_id, key, phase, **fields):
        if phase != "spawned":
            return real(node_id, key, phase, **fields)
        calls.append(phase)
        if refusal == "foreign":
            raise util.YggError(util.EXIT_TRACKER, "linear: " + JOURNAL + " was changed by another Linear user; "
                                "writes to it are held (see the ygg-linear-foreign alert)")
        raise adapter.Changed(util.EXIT_TRACKER, "linear: " + JOURNAL + " changed in Linear since it was read; "
                              "run the command again")

    monkeypatch.setattr(lifecycle, "commit", commit)
    assert launcher.supervise(activation) == util.EXIT_TRACKER
    assert len(calls) == (1 if refusal == "foreign" else launcher.GATE_ATTEMPTS)
    assert not _spawned(activation) and _agent_runs(world) == 0
    assert launcher.receipt(activation).get("pid") is None
    err = capsys.readouterr().err
    assert "supervisor of " + activation + " stopped: " in err
    reason = ("Changed: " if refusal != "foreign" else "YggError: ") + "linear: " + JOURNAL
    assert reason in err and len(err.strip().splitlines()) == 1


# An adopted journal that already holds `spawned` for this activation (another supervisor got there first) stops
# the gate: no second `spawned` fact and no second agent.
def test_a_gate_retry_that_finds_spawned_in_the_adopted_journal_starts_nothing(world, monkeypatch):
    from yggdrasil import launcher, lifecycle
    from yggdrasil.linear import adapter
    activation = _reservation(world)
    real, calls = lifecycle.commit, []

    def commit(node_id, key, phase, **fields):
        if phase == "spawned":
            calls.append(phase)
            if len(calls) == 1:
                real(node_id, key, "spawned", supervisor_pid=1, supervisor_started_at="other", agent_nonce="x")
                raise adapter.Changed(util.EXIT_TRACKER, "linear: " + JOURNAL + " changed in Linear since it was "
                                      "read; run the command again")
        return real(node_id, key, phase, **fields)

    monkeypatch.setattr(lifecycle, "commit", commit)
    assert launcher.supervise(activation) == 0
    assert calls == ["spawned"] and len(_spawned(activation)) == 1 and _agent_runs(world) == 0


# The stop summary in supervisor.err is one line, with a credential variable's value and secret-looking text removed.
def test_the_supervisor_stop_summary_is_one_line_with_secrets_removed(world, monkeypatch, capsys):
    from yggdrasil import launcher
    activation = _reservation(world)
    monkeypatch.setenv("LINEAR_API_KEY", "zz-fake-cred-0123456789")

    def boom(_activation):
        raise util.YggError(util.EXIT_TRACKER, "linear: refused\nkey zz-fake-cred-0123456789\nsecond line")

    monkeypatch.setattr(launcher, "_supervise", boom)
    assert launcher.supervise(activation) == util.EXIT_TRACKER
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert "zz-fake-cred-0123456789" not in err and "[REDACTED]" in err


# A gate retried after an adopted change checks again before it commits: an activation that ended in the meantime
# is never spawned, and one still open is spawned once, with one agent.
@pytest.mark.parametrize("meanwhile", ["ended", "nothing"])
def test_a_retried_gate_checks_again_and_spawns_at_most_once(world, monkeypatch, meanwhile):
    from yggdrasil import launcher, lifecycle
    from yggdrasil.linear import adapter
    activation = _reservation(world)
    real, calls = lifecycle.commit, []

    def commit(node_id, key, phase, **fields):
        if phase == "spawned":
            calls.append(phase)
            if len(calls) == 1:
                if meanwhile == "ended":
                    real(node_id, key, "aborted", reason="released")  # what the adopted journal holds
                raise adapter.Changed(util.EXIT_TRACKER, "linear: " + JOURNAL + " changed in Linear since it was "
                                      "read; run the command again")
        return real(node_id, key, phase, **fields)

    monkeypatch.setattr(lifecycle, "commit", commit)
    assert launcher.supervise(activation) == 0
    if meanwhile == "ended":
        assert calls == ["spawned"] and not _spawned(activation) and _agent_runs(world) == 0
    else:
        assert calls == ["spawned", "spawned"] and len(_spawned(activation)) == 1 and _agent_runs(world) == 1


# (b) The real launch path: a supervisor whose gate commit fails leaves its reason in its launch folder's
# supervisor.err (owner-only), and its activation is held as uncertain, never silently lost.
def test_a_supervisor_whose_gate_commit_fails_leaves_supervisor_err_and_is_held(world, monkeypatch):
    from yggdrasil import launcher, lifecycle, state
    folder = world / "bin"
    folder.mkdir()
    python = folder / "python"
    python.write_text("#!" + sys.executable + "\nimport sys\nfrom yggdrasil import cli, lifecycle, util\n"
                      "real = lifecycle.commit\n"
                      "def commit(node_id, activation, phase, **fields):\n"
                      "    if phase == 'spawned':\n"
                      "        raise util.YggError(util.EXIT_TRACKER, 'linear: the gate journal write was refused "
                      "(unit test)')\n"
                      "    return real(node_id, activation, phase, **fields)\n"
                      "lifecycle.commit = commit\n"
                      "sys.exit(cli.main(sys.argv[3:]))\n")  # argv: -m yggdrasil _supervise <activation>
    python.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(python))
    result = launcher.launch("tl1")
    assert result["outcome"] == "uncertain" and result["started"] is False
    activation = result["activation"]
    err = state.root() / "launch" / activation / "supervisor.err"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and "refused (unit test)" not in util.read_text(err):
        time.sleep(0.05)
    text = util.read_text(err)
    assert "supervisor of " + activation + " stopped: YggError: linear: the gate journal write was refused" in text
    assert err.stat().st_mode & 0o777 == 0o600
    assert not _spawned(activation) and _agent_runs(world) == 0
    assert launcher.receipt(activation)["outcome"] == "uncertain"
    assert lifecycle.is_uncertain("tl1", activation)
