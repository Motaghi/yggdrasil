"""Linear adapter: a changed journal or counter document is adopted only when it extends the cache."""

import json

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)

JOURNAL = "initiatives/alpha/documents/journal.md"
COUNTERS = "_ygg/counters.json"


def journal_text(entries, revision):
    body = "".join("```yaml\n" + util.dump_yaml(e) + "```\n\n" for e in entries)
    return util.render_frontmatter({"schema": "ygg.journal.v1", "programme": "alpha", "revision": revision}, body)


ENTRY_1 = {"kind": "event", "at": "2026-11-16T01:00:00Z", "actor": "ygg-app", "event_id": "e-000000000001",
           "node": "tl1", "reason": "test"}
ENTRY_2 = {"kind": "event", "at": "2026-11-16T01:05:00Z", "actor": "ygg-app", "event_id": "e-000000000002",
           "node": "tl1", "reason": "test"}


@pytest.fixture
def linear(world, monkeypatch):
    """The adapter with one guarded document `doc-1` cached at `relative`, and a scripted Linear side."""
    from yggdrasil import state
    from yggdrasil.linear import adapter
    remote = {"creator": {"id": "app"}, "updatedBy": {"id": "app"}}

    def gql(operation, query, variables=None):
        if operation == "YggViewer":
            return {"viewer": {"id": "app"}}
        assert operation == "YggRevision"
        return {"documents": {"nodes": [{"id": "doc-1", "updatedAt": remote["updatedAt"]}]}}

    monkeypatch.setattr(adapter, "gql", gql)
    monkeypatch.setattr(adapter, "_read_document", lambda identifier: dict(remote, id=identifier))

    def cache(relative, text):
        util.atomic_write(adapter.cache_root() / relative, text)
        adapter._remember(**{"guarded:doc-1": relative, "path:doc-1": relative, "updated:doc-1": "t0"})

    def linear_holds(text, updated):
        remote.update(content=adapter.encode(text), updatedAt=updated)

    def alerts():
        return [a for a in state.alerts() if a["kind"] == "ygg-tracker-rewrite"]

    return adapter, cache, linear_holds, alerts


def test_a_rewritten_journal_is_not_adopted_and_alerts_once(linear):
    adapter, cache, linear_holds, alerts = linear
    original = journal_text([ENTRY_1, ENTRY_2], 2)
    cache(JOURNAL, original)
    linear_holds(journal_text([dict(ENTRY_1, node="pm1"), ENTRY_2], 3), "t1")
    assert adapter.refresh() == []
    assert util.read_text(adapter.cache_root() / JOURNAL) == original  # the cache is kept as it is
    assert len(alerts()) == 1
    adapter.refresh()
    assert len(alerts()) == 1  # one alert for that version, not one per tick
    linear_holds(journal_text([ENTRY_2], 4), "t2")  # an entry removed
    adapter.refresh()
    assert util.read_text(adapter.cache_root() / JOURNAL) == original and len(alerts()) == 2


def test_an_appended_or_cleared_journal_is_adopted(linear):
    adapter, cache, linear_holds, alerts = linear
    cache(JOURNAL, journal_text([ENTRY_1], 1))
    grown = journal_text([dict(ENTRY_1, cleared_at="2026-11-16T01:10:00Z"), ENTRY_2], 2)
    linear_holds(grown, "t1")
    assert adapter.refresh() == [JOURNAL]
    # decode() returns the text with exactly one trailing newline.
    assert util.read_text(adapter.cache_root() / JOURNAL) == grown.rstrip("\n") + "\n" and alerts() == []


def test_a_counter_going_down_is_not_adopted(linear):
    adapter, cache, linear_holds, alerts = linear
    cache(COUNTERS, '{"activation": 12, "message": 4}\n')
    linear_holds('{"activation": 9, "message": 4}\n', "t1")
    assert adapter.refresh() == []
    assert util.read_text(adapter.cache_root() / COUNTERS) == '{"activation": 12, "message": 4}\n'
    assert len(alerts()) == 1
    linear_holds('{"activation": 13, "message": 4, "decision": 1}\n', "t2")
    assert adapter.refresh() == [COUNTERS]  # rising counters (and new kinds) are adopted


def test_a_write_to_a_rewritten_document_is_refused(linear, monkeypatch):
    adapter, cache, linear_holds, alerts = linear
    original = journal_text([ENTRY_1, ENTRY_2], 2)
    cache(JOURNAL, original)
    adapter._remember(**{"document:init-1:journal": "doc-1"})
    linear_holds(journal_text([ENTRY_2], 3), "t1")
    with pytest.raises(util.YggError) as error:
        adapter._put_document("initiative", "init-1", JOURNAL, "journal", journal_text([ENTRY_1, ENTRY_2, ENTRY_2], 3))
    assert error.value.code == util.EXIT_TRACKER and "not adopted" in error.value.message
    assert util.read_text(adapter.cache_root() / JOURNAL) == original and len(alerts()) == 1


def test_the_synced_revision_is_recorded_only_after_the_cache_file(linear, monkeypatch):
    adapter, cache, linear_holds, alerts = linear
    from yggdrasil import tracker
    cache(JOURNAL, journal_text([ENTRY_1], 1))
    monkeypatch.setattr(tracker, "selected", lambda: "linear")
    monkeypatch.setattr(tracker, "root", lambda: adapter.cache_root())
    monkeypatch.setattr(adapter, "push", lambda relative, text: {"updated:doc-1": "t1"})  # Linear committed

    def crash(path, text, mode=None):
        raise OSError("disk gone")
    monkeypatch.setattr(util, "atomic_write", crash)
    with pytest.raises(util.YggError):
        tracker.write_text(adapter.cache_root() / JOURNAL, journal_text([ENTRY_1, ENTRY_2], 2))
    assert adapter._known("updated:doc-1") == "t0"  # still unsynced: the next refresh adopts Linear's version
    monkeypatch.undo()


def test_an_interrupted_write_is_recovered_by_the_next_refresh(linear):
    adapter, cache, linear_holds, alerts = linear
    cache(JOURNAL, journal_text([ENTRY_1], 1))
    grown = journal_text([ENTRY_1, ENTRY_2], 2)
    linear_holds(grown, "t1")  # committed in Linear, the cache write and its revision lost
    assert adapter.refresh() == [JOURNAL]
    assert "e-000000000002" in util.read_text(adapter.cache_root() / JOURNAL) and alerts() == []


def test_refresh_holds_the_journal_lock(linear, monkeypatch):
    adapter, cache, linear_holds, alerts = linear
    cache(JOURNAL, journal_text([ENTRY_1], 1))
    linear_holds(journal_text([ENTRY_1, ENTRY_2], 2), "t1")
    seen = []
    real = adapter._adopt

    def adopt(identifier, relative, remote):
        seen.append(1 if util.lock_held("journal") else 0)
        return real(identifier, relative, remote)
    monkeypatch.setattr(adapter, "_adopt", adopt)
    adapter.refresh()
    assert seen and all(depth >= 1 for depth in seen)


def test_a_first_created_journal_is_recovered_after_a_crash_before_the_cache_write(world, monkeypatch):
    from yggdrasil.linear import adapter
    created = journal_text([ENTRY_1], 1)
    calls = []

    def gql(operation, query, variables=None):
        calls.append(operation)
        if operation == "YggDocumentCreate":
            return {"documentCreate": {"success": True,
                                       "document": {"id": "doc-9", "title": "journal", "updatedAt": "t1"}}}
        if operation == "YggRevision":
            return {"documents": {"nodes": [{"id": "doc-9", "updatedAt": "t1"}]}}
        if operation == "YggViewer":
            return {"viewer": {"id": "app"}}
        raise AssertionError(operation)
    monkeypatch.setattr(adapter, "gql", gql)
    monkeypatch.setattr(adapter, "_find_document", lambda *args: None)
    monkeypatch.setattr(adapter, "_read_document",
                        lambda identifier: {"id": identifier, "updatedAt": "t1", "content": adapter.encode(created),
                                            "creator": {"id": "app"}, "updatedBy": {"id": "app"}})
    after = adapter._put_document("initiative", "init-1", JOURNAL, "journal", created)
    # The process dies here: Linear holds the document, the cache file and its synced revision were never written.
    assert after == {"updated:doc-9": "t1"} and adapter._known("guarded:doc-9") == JOURNAL
    assert adapter.refresh() == [JOURNAL]
    assert "e-000000000001" in util.read_text(adapter.cache_root() / JOURNAL)


@pytest.fixture
def workspace(world, monkeypatch):
    """A scripted Linear workspace for pull(): one initiative `alpha` with the documents in `docs`."""
    from yggdrasil.linear import adapter
    docs = []

    def gql(operation, query, variables=None):
        if operation == "YggViewer":
            return {"viewer": {"id": "app"}}
        raise AssertionError(operation)

    registry = {"id": "reg", "title": "_ygg/linear-registry.json", "updatedAt": "r1",
                "content": adapter.encode('{"initiatives": {"alpha": "init-1"}, "projects": {}, "issues": {}}\n'),
                "creator": {"id": "app"}, "updatedBy": {"id": "app"}}

    def pages(operation, query, variables, path):
        if operation == "YggPullInitiatives":
            return [{"id": "init-1", "name": "alpha", "creator": {"id": "app"}}] + list(extra_initiatives)
        if operation == "YggPullDocuments" and variables["id"] == "control":
            return [registry]
        if operation == "YggPullDocuments" and variables["id"] == "init-1":
            return list(docs)
        return []
    extra_initiatives = []
    monkeypatch.setattr(adapter, "gql", gql)
    monkeypatch.setattr(adapter, "pages", pages)
    monkeypatch.setattr(adapter, "project_id", lambda name, programme=None: "control")
    monkeypatch.setattr(adapter, "team_id", lambda: "team")

    def doc(title, text, author="app", updated="t1"):
        docs.append({"id": "d-" + str(len(docs)), "title": title, "content": adapter.encode(text),
                     "updatedAt": updated, "creator": {"id": author}, "updatedBy": {"id": author}})
    doc.initiatives = extra_initiatives
    return adapter, doc


def test_pull_adopts_only_documents_the_app_wrote(workspace):
    from yggdrasil import state
    adapter, doc = workspace
    doc("initiative.json", '{"id": "alpha"}\n')
    doc("nodes.json", '{"nodes": []}\n', author="someone-else")
    counts = adapter.pull()
    assert counts["documents"] == 2 and counts["skipped"] == 1  # the registry and initiative.json
    assert (adapter.cache_root() / "initiatives/alpha/initiative.json").exists()
    assert not (adapter.cache_root() / "initiatives/alpha/nodes.json").exists()
    assert [a for a in state.alerts() if a["kind"] == "ygg-linear-foreign"]


def test_pull_never_writes_outside_the_cache(workspace, world):
    adapter, doc = workspace
    doc("../../escape.json", '{"x": 1}\n')
    doc("documents/../../../escape2.md", "x\n")
    assert adapter.pull()["skipped"] == 2
    assert not list(world.rglob("escape*"))


def test_pull_keeps_a_cached_journal_that_linear_rewrote(workspace):
    adapter, doc = workspace
    original = journal_text([ENTRY_1, ENTRY_2], 2)
    util.atomic_write(adapter.cache_root() / JOURNAL, original)
    doc("journal", journal_text([ENTRY_2], 3))
    adapter.pull()
    assert util.read_text(adapter.cache_root() / JOURNAL) == original



def test_pull_refuses_a_duplicate_or_unregistered_initiative(workspace):
    adapter, doc = workspace
    doc("initiative.json", '{"id": "alpha"}\n')
    doc.initiatives.append({"id": "init-2", "name": "alpha", "creator": {"id": "app"}})  # a second "alpha"
    doc.initiatives.append({"id": "init-3", "name": "gamma", "creator": {"id": "app"}})  # never registered
    counts = adapter.pull()
    assert counts["skipped"] >= 2
    assert adapter._known("initiative:gamma") is None


def _issue_workspace(world, monkeypatch, registered, description, linear_id="iss-1"):
    from yggdrasil.linear import adapter
    reg = {"initiatives": {}, "projects": {"p1": "proj-1"},
           "issues": {"p1/WP-1": {"linear": registered[0], "sha256": registered[1]}}}

    def gql(operation, query, variables=None):
        assert operation == "YggViewer"
        return {"viewer": {"id": "app"}}

    def pages(operation, query, variables, path):
        if operation == "YggPullDocuments" and variables["id"] == "control":
            return [{"id": "reg", "title": "_ygg/linear-registry.json", "updatedAt": "r1",
                     "content": adapter.encode(json.dumps(reg)), "creator": {"id": "app"}, "updatedBy": {"id": "app"}}]
        if operation == "YggPullProjects":
            return [{"id": "proj-1", "name": "p1", "creator": {"id": "app"}}]
        if operation == "YggPullIssues":
            return [{"id": linear_id, "description": adapter.encode(description), "creator": {"id": "app"}}]
        return []
    monkeypatch.setattr(adapter, "gql", gql)
    monkeypatch.setattr(adapter, "pages", pages)
    monkeypatch.setattr(adapter, "project_id", lambda name, programme=None: "control")
    monkeypatch.setattr(adapter, "team_id", lambda: "team")
    return adapter


ISSUE = "---\nid: WP-1\nkind: work-package\ntitle: Save\n---\n\n## Records\n\n## Comments\n\n"


def test_pull_adopts_an_issue_only_as_the_app_last_wrote_it(world, monkeypatch):
    import json  # noqa: F401
    from yggdrasil.linear.adapter import _sha
    adapter = _issue_workspace(world, monkeypatch, ("iss-1", _sha(ISSUE)), ISSUE)
    assert adapter.pull()["issues"] == 1
    edited = ISSUE.replace("title: Save", "title: Save, approved by the user")
    adapter = _issue_workspace(world, monkeypatch, ("iss-1", _sha(ISSUE)), edited)
    (adapter.cache_root() / "projects/p1/issues/WP-1.md").unlink()
    counts = adapter.pull()
    assert counts["issues"] == 0 and counts["skipped"] == 1  # edited in Linear after the app wrote it
    assert not (adapter.cache_root() / "projects/p1/issues/WP-1.md").exists()


def test_pull_refuses_a_header_naming_another_issue(world, monkeypatch):
    from yggdrasil.linear.adapter import _sha
    adapter = _issue_workspace(world, monkeypatch, ("iss-1", _sha(ISSUE)), ISSUE, linear_id="iss-other")
    counts = adapter.pull()
    assert counts["issues"] == 0 and counts["skipped"] == 1
    assert not (adapter.cache_root() / "projects/p1/issues/WP-1.md").exists()


def test_the_write_path_only_updates_a_registered_issue(world, monkeypatch):
    from yggdrasil.linear import adapter
    assert adapter._issue_id("p1", "WP-1") is None  # no title or header search: an unregistered record is new
    util.atomic_write(adapter.cache_root() / adapter.REGISTRY,
                      '{"initiatives": {}, "projects": {}, "issues": {"p1/WP-1": {"linear": "iss-1", "sha256": "x"}}}\n')
    assert adapter._issue_id("p1", "WP-1") == "iss-1"


def test_a_foreign_project_with_our_name_is_refused(world, monkeypatch):
    from yggdrasil.linear import adapter

    def gql(operation, query, variables=None):
        if operation == "YggViewer":
            return {"viewer": {"id": "app"}}
        if operation == "YggProjectFind":
            return {"team": {"projects": {"nodes": [{"id": "p-x", "name": "p9", "creator": {"id": "someone"}}]}}}
        raise AssertionError(operation)
    monkeypatch.setattr(adapter, "gql", gql)
    monkeypatch.setattr(adapter, "team_id", lambda: "team")
    with pytest.raises(util.YggError):
        adapter.project_id("p9")


def test_a_failed_comment_never_hides_the_record_the_issue_holds(world, monkeypatch):
    from yggdrasil import tracker
    from yggdrasil.linear import adapter
    calls = []

    def gql(operation, query, variables=None):
        calls.append(operation)
        if operation == "YggComment":
            raise util.YggError(util.EXIT_TRACKER, "linear transient: comment failed")
        if operation == "YggIssueUpdate":
            return {"issueUpdate": {"success": True, "issue": {"id": "iss-1", "updatedAt": "t1"}}}
        if operation == "YggIssueRead":
            return {"issue": {"id": "iss-1", "updatedAt": "t1", "description": adapter.encode(ISSUE)}}
        raise AssertionError(operation)
    monkeypatch.setattr(adapter, "gql", gql)
    monkeypatch.setattr(adapter, "pages", lambda *args: [])
    monkeypatch.setattr(adapter, "project_id", lambda name, programme=None: "proj-1")
    monkeypatch.setattr(adapter, "label_id", lambda name: "label-" + name)
    monkeypatch.setattr(adapter, "_register", lambda *args: None)
    monkeypatch.setattr(tracker, "selected", lambda: "linear")
    monkeypatch.setattr(tracker, "root", lambda: adapter.cache_root())
    adapter._remember(**{"issue:p1:WP-1": "iss-1", "issue-updated:iss-1": "t1"})
    path = adapter.cache_root() / "projects/p1/issues/WP-1.md"
    util.atomic_write(path, ISSUE)
    stamped = ISSUE.replace("## Records\n\n", "## Records\n\n```yaml\nkind: criteria\nbody: owed\n```\n\n")
    with pytest.raises(util.YggError):
        tracker.write_text(path, stamped)
    assert util.read_text(path) == stamped  # Linear holds it, and so does the cache: nothing is hidden
    assert json.loads(adapter._known("comments-due:p1/WP-1")) == ["r0"]
    calls.clear()
    monkeypatch.setattr(adapter, "gql", lambda operation, query, variables=None: calls.append(operation) or
                        {"issueUpdate": {"success": True, "issue": {"id": "iss-1", "updatedAt": "t1"}},
                         "commentCreate": {"success": True},
                         "issue": {"id": "iss-1", "updatedAt": "t1", "description": adapter.encode(stamped)}})
    tracker.write_text(path, stamped)
    assert calls.count("YggComment") == 1 and json.loads(adapter._known("comments-due:p1/WP-1")) == []


def test_pull_never_adopts_a_pending_only_match(world, monkeypatch):
    from yggdrasil.linear.adapter import _sha
    newer = ISSUE.replace("title: Save", "title: Save v2")
    adapter = _issue_workspace(world, monkeypatch, ("iss-1", _sha(ISSUE)), newer)
    reg_path = None  # the registry entry of a write that registered `pending` and then stopped

    def pages(operation, query, variables, path, real=adapter.pages):
        if operation == "YggPullDocuments" and variables["id"] == "control":
            reg = {"initiatives": {}, "projects": {"p1": "proj-1"},
                   "issues": {"p1/WP-1": {"linear": "iss-1", "sha256": _sha(ISSUE), "pending": _sha(newer)}}}
            return [{"id": "reg", "title": "_ygg/linear-registry.json", "updatedAt": "r2",
                     "content": adapter.encode(json.dumps(reg)), "creator": {"id": "app"}, "updatedBy": {"id": "app"}}]
        return real(operation, query, variables, path)
    monkeypatch.setattr(adapter, "pages", pages)
    counts = adapter.pull()  # a pending hash is no evidence that the write reached Linear: refused, alerted
    assert reg_path is None and counts["issues"] == 0 and counts["skipped"] == 1


def test_a_cached_container_id_repairs_its_missing_registration(world, monkeypatch):
    from yggdrasil.linear import adapter
    written = []
    monkeypatch.setattr(adapter, "_put_document", lambda container, cid, relative, title, text: written.append(text))
    monkeypatch.setattr(adapter, "project_id", lambda name, programme=None: "control")
    adapter._remember(**{"initiative:alpha": "init-7"})  # an interrupted create cached the id, never registered
    assert adapter.initiative_id("alpha") == "init-7"
    assert written and json.loads(written[-1])["initiatives"] == {"alpha": "init-7"}
    assert adapter._registry()["initiatives"] == {"alpha": "init-7"}


def test_a_malformed_registry_is_never_replaced(world, monkeypatch):
    from yggdrasil import state
    from yggdrasil.linear import adapter
    written = []
    monkeypatch.setattr(adapter, "_put_document", lambda *args: written.append(args))
    util.atomic_write(adapter.cache_root() / adapter.REGISTRY, '{"issues": ["not", "a", "mapping"]')
    with pytest.raises(util.YggError) as error:
        adapter._register("issues", "p1/WP-1", {"linear": "iss-1", "sha256": "x"})
    assert error.value.code == util.EXIT_TRACKER and written == []
    assert [a for a in state.alerts() if a["kind"] == "ygg-tracker-rewrite"]


def _registry_with(adapter, entry):
    util.atomic_write(adapter.cache_root() / adapter.REGISTRY,
                      json.dumps({"initiatives": {}, "projects": {}, "issues": {"p1/WP-1": entry}}))


def _scripted_issue(adapter, monkeypatch, remote, *, fail_update=False):
    """`remote` is Linear's issue (updatedAt, description); registry writes go only to the cache file."""
    def put_document(container, cid, relative, title, text):
        pass
    monkeypatch.setattr(adapter, "_put_document", put_document)
    monkeypatch.setattr(adapter, "project_id", lambda name, programme=None: "proj-1")

    def gql(operation, query, variables=None):
        if operation == "YggIssueRead":
            return {"issue": dict(remote, id="iss-1")}
        if operation == "YggIssueUpdate":
            if fail_update == "rejected":  # Linear answers that it did not apply the update
                return {"issueUpdate": {"success": False}}
            remote.update(description=variables["input"]["description"], updatedAt="t-new")
            if fail_update == "incomplete":  # applied, but the answer names no issue
                return {"issueUpdate": {"success": True, "issue": None}}
            return {"issueUpdate": {"success": True, "issue": {"id": "iss-1", "updatedAt": "t-new"}}}
        raise AssertionError(operation)
    monkeypatch.setattr(adapter, "gql", gql)


def test_a_failed_update_then_a_forged_edit_of_its_text_is_refused(world, monkeypatch):
    from yggdrasil.linear import adapter
    newer = ISSUE.replace("title: Save", "title: Save v2")
    remote = {"updatedAt": "t1", "description": adapter.encode(ISSUE)}
    _registry_with(adapter, {"linear": "iss-1", "sha256": adapter._sha(ISSUE), "updated_at": "t1"})
    adapter._remember(**{"issue:p1:WP-1": "iss-1", "issue-updated:iss-1": "t1"})
    util.atomic_write(adapter.cache_root() / "projects/p1/issues/WP-1.md", ISSUE)
    _scripted_issue(adapter, monkeypatch, remote, fail_update="rejected")
    monkeypatch.setattr(adapter, "label_id", lambda name: "l-" + name)
    with pytest.raises(util.YggError):
        adapter._put_issue("p1", "WP-1", newer, ISSUE)
    entry = adapter._registry()["issues"]["p1/WP-1"]
    assert "pending" not in entry  # restored: a write Linear refused leaves no wider check behind
    remote.update(description=adapter.encode(newer), updatedAt="t-forged")  # someone writes the same text
    with pytest.raises(util.YggError) as error:
        adapter._resolve_issue("p1", "WP-1", "iss-1", "p1/WP-1")
    assert "someone other than the app" in error.value.message


def test_a_write_that_landed_before_its_confirmation_is_adopted_by_the_next_write(world, monkeypatch):
    from yggdrasil.linear import adapter
    newer = ISSUE.replace("title: Save", "title: Save v2")
    _registry_with(adapter, {"linear": "iss-1", "sha256": adapter._sha(ISSUE), "updated_at": "t1",
                             "pending": adapter._sha(newer), "pending_from": "t1"})
    adapter._remember(**{"issue:p1:WP-1": "iss-1", "issue-updated:iss-1": "t1"})
    util.atomic_write(adapter.cache_root() / "projects/p1/issues/WP-1.md", ISSUE)  # the cache never got it
    remote = {"updatedAt": "t2", "description": adapter.encode(newer)}
    _scripted_issue(adapter, monkeypatch, remote)
    with pytest.raises(util.YggError) as error:
        adapter._resolve_issue("p1", "WP-1", "iss-1", "p1/WP-1")
    assert "run the command again" in error.value.message
    assert "Save v2" in util.read_text(adapter.cache_root() / "projects/p1/issues/WP-1.md")
    entry = adapter._registry()["issues"]["p1/WP-1"]
    assert entry["sha256"] == adapter._sha(newer) and "pending" not in entry
    assert adapter._resolve_issue("p1", "WP-1", "iss-1", "p1/WP-1")["sha256"] == adapter._sha(newer)


def test_a_pending_write_that_never_landed_is_forgotten(world, monkeypatch):
    from yggdrasil.linear import adapter
    newer = ISSUE.replace("title: Save", "title: Save v2")
    _registry_with(adapter, {"linear": "iss-1", "sha256": adapter._sha(ISSUE), "updated_at": "t1",
                             "pending": adapter._sha(newer), "pending_from": "t1"})
    adapter._remember(**{"issue:p1:WP-1": "iss-1", "issue-updated:iss-1": "t1"})
    util.atomic_write(adapter.cache_root() / "projects/p1/issues/WP-1.md", ISSUE)
    _scripted_issue(adapter, monkeypatch, {"updatedAt": "t1", "description": adapter.encode(ISSUE)})
    entry = adapter._resolve_issue("p1", "WP-1", "iss-1", "p1/WP-1")
    assert entry["sha256"] == adapter._sha(ISSUE) and "pending" not in adapter._registry()["issues"]["p1/WP-1"]


def test_an_empty_existing_registry_is_refused_and_alerted_once(world, monkeypatch):
    from yggdrasil import state
    from yggdrasil.linear import adapter
    monkeypatch.setattr(adapter, "_put_document", lambda *args: None)
    util.atomic_write(adapter.cache_root() / adapter.REGISTRY, "  \n")
    for _ in range(2):
        with pytest.raises(util.YggError):
            adapter._register("projects", "p1", "proj-1")
    assert len([a for a in state.alerts() if a["kind"] == "ygg-tracker-rewrite"]) == 1


def test_an_update_answered_without_success_is_a_failed_write(world, monkeypatch):
    from yggdrasil.linear import adapter
    newer = ISSUE.replace("title: Save", "title: Save v2")
    remote = {"updatedAt": "t1", "description": adapter.encode(ISSUE)}
    _registry_with(adapter, {"linear": "iss-1", "sha256": adapter._sha(ISSUE), "updated_at": "t1"})
    adapter._remember(**{"issue:p1:WP-1": "iss-1", "issue-updated:iss-1": "t1"})
    util.atomic_write(adapter.cache_root() / "projects/p1/issues/WP-1.md", ISSUE)
    _scripted_issue(adapter, monkeypatch, remote)
    monkeypatch.setattr(adapter, "label_id", lambda name: "l-" + name)
    real = adapter.gql

    def gql(operation, query, variables=None):
        if operation == "YggIssueUpdate":
            return {"issueUpdate": {"success": False, "issue": None}}
        return real(operation, query, variables)
    monkeypatch.setattr(adapter, "gql", gql)
    with pytest.raises(util.YggError) as error:
        adapter._put_issue("p1", "WP-1", newer, ISSUE)
    assert error.value.code == util.EXIT_TRACKER
    assert adapter._registry()["issues"]["p1/WP-1"] == {"linear": "iss-1", "sha256": adapter._sha(ISSUE),
                                                        "updated_at": "t1"}


@pytest.mark.parametrize("entry", [None, [], "x", 5, {"sha256": "x"}, {"linear": "iss-1"}])
def test_a_malformed_registry_entry_is_refused(world, monkeypatch, entry):
    from yggdrasil.linear import adapter
    monkeypatch.setattr(adapter, "_put_document", lambda *args: None)
    util.atomic_write(adapter.cache_root() / adapter.REGISTRY,
                      json.dumps({"initiatives": {}, "projects": {}, "issues": {"p1/WP-1": entry}}))
    with pytest.raises(util.YggError):
        adapter._registry()


def test_an_adopted_version_is_recorded_only_after_its_content_is_cached(world, monkeypatch):
    from yggdrasil.linear import adapter
    newer = ISSUE.replace("title: Save", "title: Save v2")
    _registry_with(adapter, {"linear": "iss-1", "sha256": adapter._sha(newer), "updated_at": "t2"})
    adapter._remember(**{"issue:p1:WP-1": "iss-1", "issue-updated:iss-1": "t1"})
    util.atomic_write(adapter.cache_root() / "projects/p1/issues/WP-1.md", ISSUE)
    _scripted_issue(adapter, monkeypatch, {"updatedAt": "t2", "description": adapter.encode(newer)})
    real = util.atomic_write

    def crash(path, text, mode=None):
        if str(path).endswith("WP-1.md"):
            raise OSError("disk gone")
        return real(path, text, mode)
    monkeypatch.setattr(adapter.util, "atomic_write", crash)
    with pytest.raises(OSError):
        adapter._resolve_issue("p1", "WP-1", "iss-1", "p1/WP-1")
    assert adapter._known("issue-updated:iss-1") == "t1"  # still unsynced: the next write adopts it again


@pytest.mark.parametrize("field", ["projectUpdateCreate", "commentCreate", "initiativeToProjectCreate",
                                   "initiativeCreate", "projectCreate", "issueLabelCreate",
                                   "projectMilestoneCreate"])
def test_every_mutation_without_success_is_a_failed_write(world, field):
    from yggdrasil.linear import adapter
    with pytest.raises(util.YggError) as error:
        adapter._succeeded({field: {"success": False}}, field)
    assert error.value.code == util.EXIT_TRACKER
    entity = {"initiativeCreate": "initiative", "projectCreate": "project", "issueLabelCreate": "issueLabel",
              "projectMilestoneCreate": "projectMilestone"}.get(field)
    if entity:
        with pytest.raises(util.YggError):
            adapter._succeeded({field: {"success": True, entity: None}}, field, entity, version=False)


def test_a_highlight_whose_update_is_refused_writes_nothing_locally(world, monkeypatch):
    from yggdrasil import tracker
    from yggdrasil.linear import adapter
    monkeypatch.setattr(adapter, "pages", lambda *args: [])
    monkeypatch.setattr(adapter, "gql", lambda operation, query, variables=None:
                        {"projectUpdateCreate": {"success": False}})
    monkeypatch.setattr(adapter, "project_id", lambda name, programme=None: "proj-1")
    monkeypatch.setattr(tracker, "selected", lambda: "linear")
    monkeypatch.setattr(tracker, "root", lambda: adapter.cache_root())
    path = adapter.cache_root() / "projects/p1/updates/20261118-highlight.md"
    with pytest.raises(util.YggError):
        tracker.write_text(path, "---\nhealth: atRisk\n---\nslipping\n")
    assert not path.exists()



def _foreign_alerts():
    from yggdrasil import state
    return [a for a in state.alerts() if a["kind"] == "ygg-linear-foreign"]


@pytest.mark.parametrize("change", ["append", "clear"])
def test_a_journal_change_by_another_linear_user_is_not_adopted(linear, change):
    adapter, cache, linear_holds, alerts = linear
    original = journal_text([ENTRY_1], 1)
    cache(JOURNAL, original)
    records = [ENTRY_1, dict(ENTRY_2, actor="user")] if change == "append" else \
        [dict(ENTRY_1, cleared_at="2026-11-16T01:10:00Z")]
    linear_holds(journal_text(records, 2), "t1")
    from yggdrasil.linear import adapter as module
    original_read = module._read_document
    module._read_document = lambda identifier: dict(original_read(identifier), updatedBy={"id": "other-user"})
    try:
        assert adapter.refresh() == []
        adapter.refresh()
    finally:
        module._read_document = original_read
    assert util.read_text(adapter.cache_root() / JOURNAL) == original  # the cache is kept as it is
    assert len(_foreign_alerts()) == 1  # once for that version


def test_a_foreign_document_found_by_title_is_never_written(world, monkeypatch):
    from yggdrasil.linear import adapter
    changes = []
    monkeypatch.setattr(adapter, "pages", lambda operation, query, variables, path: [
        {"id": "foreign-doc", "title": "brief", "creator": {"id": "other-user"}}])

    def gql(operation, query, variables=None):
        if operation == "YggViewer":
            return {"viewer": {"id": "app"}}
        if operation in ("YggDocumentUpdate", "YggDocumentCreate"):
            changes.append(variables)
        raise AssertionError(operation)

    monkeypatch.setattr(adapter, "gql", gql)
    with pytest.raises(util.YggError):
        adapter._put_document("project", "owned-project", "projects/p1/documents/brief.md", "brief", "new brief\n")
    assert changes == []
    assert adapter._known("document:owned-project:brief") is None
    assert len(_foreign_alerts()) == 1


def test_an_owned_document_last_changed_by_another_user_is_never_written(world, monkeypatch):
    from yggdrasil.linear import adapter
    changes = []
    adapter._remember(**{"document:owned-project:brief": "doc-9"})

    def gql(operation, query, variables=None):
        if operation == "YggViewer":
            return {"viewer": {"id": "app"}}
        if operation == "YggDocumentRead":
            return {"document": {"id": "doc-9", "updatedAt": "t1", "content": "x", "creator": {"id": "app"},
                                 "updatedBy": {"id": "other-user"}}}
        changes.append(operation)
        raise AssertionError(operation)

    monkeypatch.setattr(adapter, "gql", gql)
    with pytest.raises(util.YggError):
        adapter._put_document("project", "owned-project", "projects/p1/documents/brief.md", "brief", "new brief\n")
    assert changes == []
    assert len(_foreign_alerts()) == 1


@pytest.mark.parametrize("cached", [False, True])
def test_a_foreign_document_is_alerted_before_its_repeat_is_suppressed(world, monkeypatch, cached):
    from yggdrasil import state
    from yggdrasil.linear import adapter
    order = []
    monkeypatch.setattr(state, "alert", lambda kind, *a, **k: order.append(("alert", kind)))
    original = adapter._remember
    monkeypatch.setattr(adapter, "_remember", lambda **values: order.append(("mark", sorted(values))) or
                        original(**values))
    if cached:
        original(**{"document:owned-project:brief": "doc-9"})
    monkeypatch.setattr(adapter, "pages", lambda *args: [{"id": "f", "title": "brief", "creator": {"id": "other"}}])

    def gql(operation, query, variables=None):
        if operation == "YggViewer":
            return {"viewer": {"id": "app"}}
        if operation == "YggDocumentRead":
            return {"document": {"id": "doc-9", "updatedAt": "t1", "content": "x", "creator": {"id": "other"}}}
        raise AssertionError(operation)

    monkeypatch.setattr(adapter, "gql", gql)
    for _ in range(2):
        with pytest.raises(util.YggError):
            adapter._put_document("project", "owned-project", "projects/p1/documents/brief.md", "brief", "x\n")
    alerts = [i for i, item in enumerate(order) if item == ("alert", "ygg-linear-foreign")]
    marks = [i for i, item in enumerate(order) if item[0] == "mark" and
             any(k.startswith(("rejected:", "foreign-title:")) for k in item[1])]
    assert len(alerts) == 1 and marks and alerts[0] < marks[0]


# Landing review round 2: a version whose last updater is unknown (a deleted account reads as null) is
# never adopted or written over, and an ambiguous issue update keeps the obligation it may have committed.
@pytest.mark.parametrize("change", ["append", "clear"])
def test_a_version_with_an_unknown_last_updater_is_not_adopted(linear, monkeypatch, change):
    from yggdrasil import journal, reducer
    adapter, cache, linear_holds, alerts = linear
    original = journal_text([ENTRY_1], 1)
    cache(JOURNAL, original)
    records = [ENTRY_1, ENTRY_2] if change == "append" else [dict(ENTRY_1, cleared_at="2026-11-16T01:10:00Z")]
    linear_holds(journal_text(records, 2), "t1")
    read = adapter._read_document
    monkeypatch.setattr(adapter, "_read_document", lambda identifier: dict(read(identifier), updatedBy=None))
    changed = adapter.refresh()
    cached = util.read_text(adapter.cache_root() / JOURNAL)
    trusted, _ = journal.trusted_entries("alpha", journal._parse_blocks(cached))
    assert cached == original, {"changed": changed, "events": sorted(reducer.fold(trusted)["events"])}
    from yggdrasil import state
    assert len([a for a in state.alerts() if a["kind"] == "ygg-linear-foreign"]) == 1


def test_a_document_with_an_unknown_last_updater_is_never_written(world, monkeypatch):
    from yggdrasil.linear import adapter
    mutations = []
    adapter._remember(**{"document:owned-project:brief": "doc-9"})

    def gql(operation, query, variables=None):
        if operation == "YggViewer":
            return {"viewer": {"id": "app"}}
        if operation == "YggDocumentRead":
            return {"document": {"id": "doc-9", "updatedAt": "t1", "content": "foreign edit",
                                 "creator": {"id": "app"}, "updatedBy": None}}
        if operation == "YggDocumentUpdate":
            mutations.append(variables)
            return {"documentUpdate": {"success": True, "document": {"id": "doc-9", "updatedAt": "t2"}}}
        raise AssertionError(operation)

    monkeypatch.setattr(adapter, "gql", gql)
    with pytest.raises(util.YggError):
        adapter._put_document("project", "owned-project", "projects/p1/documents/brief.md", "brief", "replacement\n")
    assert mutations == []


def test_an_ambiguous_committed_issue_update_keeps_its_owed_work(world, monkeypatch):
    import shutil
    from yggdrasil import commands, reducer, tracker
    from yggdrasil.linear import adapter
    from yggdrasil.linear.client import LinearError
    commands.tracker_write("pm1", "p1", "work-package", "Build", stage="1", package="WP-1")
    commands.tracker_write("su1", "p1", "criteria", "Test", package="WP-1")
    original = (world / "tracker/projects/p1/issues/WP-1.md").read_text()
    shutil.copytree(world / "tracker", adapter.cache_root(), dirs_exist_ok=True)
    issue_path = adapter.cache_root() / "projects/p1/issues/WP-1.md"
    util.atomic_write(issue_path, original)
    _registry_with(adapter, {"linear": "iss-1", "sha256": adapter._sha(original), "updated_at": "t1"})
    adapter._remember(**{"issue:p1:WP-1": "iss-1", "issue-updated:iss-1": "t1"})
    remote = {"updatedAt": "t1", "description": adapter.encode(original)}
    monkeypatch.setattr(adapter, "project_id", lambda *args: "proj-1")
    monkeypatch.setattr(adapter, "label_id", lambda name: "l-" + name)
    monkeypatch.setattr(adapter, "milestone_id", lambda *args: "stage-1")
    monkeypatch.setattr(adapter, "pages", lambda *args: [])
    monkeypatch.setattr(adapter, "_put_document", lambda *args: {})
    monkeypatch.setattr(tracker, "selected", lambda: "linear")
    monkeypatch.setattr(tracker, "root", adapter.cache_root)

    def gql(operation, query, variables=None):
        if operation == "YggIssueRead":
            return {"issue": dict(remote, id="iss-1")}
        if operation == "YggIssueUpdate":
            remote.update(description=variables["input"]["description"], updatedAt="t2")
            raise LinearError("ambiguous", "issueUpdate committed, but the response was lost")
        raise AssertionError(operation)

    monkeypatch.setattr(adapter, "gql", gql)
    with pytest.raises(LinearError):
        commands.wp_authorise("p1", "WP-1", "tl1")
    header, _ = util.split_frontmatter(adapter.decode(remote["description"]))
    assert header["state"] == "authorised"
    remote_owed = {item["id"] for item in header["owed"] if item["node"] == "tl1"}
    assert remote_owed
    assert adapter._registry()["issues"]["p1/WP-1"].get("pending") == adapter._sha(adapter.decode(remote["description"]))
    adapter.refresh()  # the next tick reconciles the write that landed
    assert "pending" not in adapter._registry()["issues"]["p1/WP-1"]
    assert any(item["id"] in remote_owed for item in reducer.owed_all().get("tl1", []))


# Landing review round 3: pending evidence goes only on proof the update was not applied, and a confirmed
# version stays due for reconciliation until the cache holds it.
def _issue_world(adapter, monkeypatch, remote, *, fail_update=False, pending=None):
    entry = {"linear": "iss-1", "sha256": adapter._sha(ISSUE), "updated_at": "t1"}
    if pending:
        entry.update(pending=adapter._sha(pending), pending_from="t1")
    _registry_with(adapter, entry)
    adapter._remember(**{"issue:p1:WP-1": "iss-1", "issue-updated:iss-1": "t1"})
    util.atomic_write(adapter.cache_root() / "projects/p1/issues/WP-1.md", ISSUE)
    _scripted_issue(adapter, monkeypatch, remote, fail_update=fail_update)
    monkeypatch.setattr(adapter, "label_id", lambda name: "l-" + name)


@pytest.mark.parametrize("failure", ["incomplete", "error", "unclassified"])
def test_an_update_without_proof_it_failed_keeps_its_pending_hash(world, monkeypatch, failure):
    from yggdrasil.linear import adapter
    from yggdrasil.linear.client import LinearError
    newer = ISSUE.replace("title: Save", "title: Save v2")
    remote = {"updatedAt": "t1", "description": adapter.encode(ISSUE)}
    _issue_world(adapter, monkeypatch, remote, fail_update="incomplete" if failure == "incomplete" else False)
    if failure != "incomplete":
        scripted = adapter.gql

        def gql(operation, query, variables=None):
            if operation == "YggIssueUpdate":
                remote.update(description=variables["input"]["description"], updatedAt="t-new")
                if failure == "error":  # HTTP 200 with an execution error after the commit
                    raise LinearError("invalid", "response resolver failed after commit")
                raise util.YggError(util.EXIT_TRACKER, "linear: something unexpected")
            return scripted(operation, query, variables)
        monkeypatch.setattr(adapter, "gql", gql)
    with pytest.raises(util.YggError):
        adapter._put_issue("p1", "WP-1", newer, ISSUE)
    assert adapter._registry()["issues"]["p1/WP-1"]["pending"] == adapter._sha(newer)
    assert adapter.refresh() == ["projects/p1/issues/WP-1.md"]  # the next tick adopts the write that landed
    assert "Save v2" in util.read_text(adapter.cache_root() / "projects/p1/issues/WP-1.md")
    assert "pending" not in adapter._registry()["issues"]["p1/WP-1"]


def test_an_update_rejected_before_execution_forgets_its_pending_hash(world, monkeypatch):
    from yggdrasil.linear import adapter
    from yggdrasil.linear.client import LinearError
    remote = {"updatedAt": "t1", "description": adapter.encode(ISSUE)}
    _issue_world(adapter, monkeypatch, remote)
    scripted = adapter.gql

    def gql(operation, query, variables=None):
        if operation == "YggIssueUpdate":
            error = LinearError("invalid", "Variable $input got an invalid value")
            error.status = 400
            raise error
        return scripted(operation, query, variables)
    monkeypatch.setattr(adapter, "gql", gql)
    with pytest.raises(util.YggError):
        adapter._put_issue("p1", "WP-1", ISSUE.replace("title: Save", "title: Save v2"), ISSUE)
    assert "pending" not in adapter._registry()["issues"]["p1/WP-1"]


def test_a_crash_after_the_confirmation_before_the_cache_write_is_recovered(world, monkeypatch):
    from yggdrasil.linear import adapter
    newer = ISSUE.replace("title: Save", "title: Save v2")
    remote = {"updatedAt": "t1", "description": adapter.encode(ISSUE)}
    _issue_world(adapter, monkeypatch, remote)
    adapter._put_issue("p1", "WP-1", newer, ISSUE)  # confirmed; the process dies before the cache write
    entry = adapter._registry()["issues"]["p1/WP-1"]
    assert entry["sha256"] == adapter._sha(newer) and "pending" not in entry
    assert adapter.pending_issues() == ["p1/WP-1"]  # still due: the cache does not hold it yet
    assert adapter.refresh() == ["projects/p1/issues/WP-1.md"]
    assert "Save v2" in util.read_text(adapter.cache_root() / "projects/p1/issues/WP-1.md")
    assert adapter.pending_issues() == []


def test_a_crash_in_resolve_after_the_confirmation_before_the_cache_write_is_recovered(world, monkeypatch):
    from yggdrasil.linear import adapter
    newer = ISSUE.replace("title: Save", "title: Save v2")
    _issue_world(adapter, monkeypatch, {"updatedAt": "t2", "description": adapter.encode(newer)}, pending=newer)
    real = util.atomic_write

    def crash(path, text):
        if str(path).endswith("issues/WP-1.md"):
            raise KeyboardInterrupt("the process dies before the cache write")
        real(path, text)
    monkeypatch.setattr(util, "atomic_write", crash)
    with pytest.raises(KeyboardInterrupt):
        adapter._resolve_issue("p1", "WP-1", "iss-1", "p1/WP-1")
    monkeypatch.setattr(util, "atomic_write", real)
    entry = adapter._registry()["issues"]["p1/WP-1"]
    assert entry["sha256"] == adapter._sha(newer) and "pending" not in entry
    assert adapter.pending_issues() == ["p1/WP-1"]
    assert adapter.refresh() == ["projects/p1/issues/WP-1.md"]
    assert "Save v2" in util.read_text(adapter.cache_root() / "projects/p1/issues/WP-1.md")
    assert adapter.pending_issues() == []


# Landing review round 4: the reconciliation matrix. Pending evidence and the unsynced mark go only on a
# complete read that settles the issue; a failed, partial or ambiguous read leaves both for the next tick.
NEWER = ISSUE.replace("title: Save", "title: Save v2")
FAILED_READS = {
    "unreachable": lambda: (_ for _ in ()).throw(__import__("yggdrasil.linear.client", fromlist=["x"]).LinearError(
        "unreachable", "connection refused")),
    "forbidden": lambda: (_ for _ in ()).throw(util.YggError(util.EXIT_TRACKER, "linear auth: forbidden")),
    "no-issue": lambda: {"issue": None},
    "no-version": lambda: {"issue": {"id": "iss-1", "description": None}},
    "no-description": lambda: {"issue": {"id": "iss-1", "updatedAt": "t2"}},
    "another-id": lambda: {"issue": {"id": "iss-9", "updatedAt": "t2", "description": "x"}},
}


def _reconcile_world(adapter, monkeypatch, state):
    """`pending`: the write crashed before its confirmation; `unsynced`: confirmed, the cache never got it."""
    remote = {"updatedAt": "t2", "description": adapter.encode(NEWER)}
    if state == "pending":
        _issue_world(adapter, monkeypatch, remote, pending=NEWER)
    else:
        _issue_world(adapter, monkeypatch, remote)
        _registry_with(adapter, {"linear": "iss-1", "sha256": adapter._sha(NEWER), "updated_at": "t2"})
        adapter._remember(**{adapter.UNSYNCED + "p1/WP-1": "iss-1"})
    return remote


def _snapshot(adapter):
    return (adapter._registry()["issues"]["p1/WP-1"], adapter.pending_issues(),
            util.read_text(adapter.cache_root() / "projects/p1/issues/WP-1.md"))


@pytest.mark.parametrize("state", ["pending", "unsynced"])
@pytest.mark.parametrize("read", sorted(FAILED_READS))
def test_a_failed_or_partial_reconciliation_read_changes_nothing(world, monkeypatch, state, read):
    from yggdrasil.linear import adapter
    _reconcile_world(adapter, monkeypatch, state)
    before = _snapshot(adapter)
    scripted = adapter.gql
    monkeypatch.setattr(adapter, "gql", lambda op, q, v=None: FAILED_READS[read]() if op == "YggIssueRead"
                        else scripted(op, q, v))
    assert adapter.refresh() == []
    assert _snapshot(adapter) == before  # pending, the mark and the cache are all kept
    monkeypatch.setattr(adapter, "gql", scripted)  # the next tick reads Linear completely
    assert adapter.refresh() == ["projects/p1/issues/WP-1.md"]
    entry, due, cached = _snapshot(adapter)
    assert "Save v2" in cached and "pending" not in entry and due == []


@pytest.mark.parametrize("state", ["pending", "unsynced"])
def test_a_read_showing_the_write_landed_adopts_it(world, monkeypatch, state):
    from yggdrasil.linear import adapter
    _reconcile_world(adapter, monkeypatch, state)
    assert adapter.refresh() == ["projects/p1/issues/WP-1.md"]
    entry, due, cached = _snapshot(adapter)
    assert adapter._sha(cached) == adapter._sha(NEWER) and entry["sha256"] == adapter._sha(NEWER)
    assert "pending" not in entry and due == []
    assert adapter._known("issue-updated:iss-1") == "t2"


def test_a_read_showing_the_write_did_not_land_forgets_it(world, monkeypatch):
    from yggdrasil.linear import adapter
    _issue_world(adapter, monkeypatch, {"updatedAt": "t1", "description": adapter.encode(ISSUE)}, pending=NEWER)
    assert adapter.refresh() == []
    entry, due, cached = _snapshot(adapter)
    assert cached == ISSUE and entry["sha256"] == adapter._sha(ISSUE) and "pending" not in entry and due == []


@pytest.mark.parametrize("state", ["pending", "unsynced"])
def test_a_read_showing_a_foreign_version_settles_with_one_alert(world, monkeypatch, state):
    from yggdrasil import state as ystate
    from yggdrasil.linear import adapter
    remote = _reconcile_world(adapter, monkeypatch, state)
    remote.update(description=adapter.encode(ISSUE.replace("title: Save", "title: edited by a person")), updatedAt="t3")
    adapter.refresh()
    adapter.refresh()
    _, due, cached = _snapshot(adapter)
    assert cached == ISSUE and due == []
    assert len([a for a in ystate.alerts() if a["kind"] == "ygg-linear-foreign"]) == 1


@pytest.mark.parametrize("state", ["pending", "unsynced"])
def test_a_crash_at_any_reconciliation_write_is_recovered_by_the_next_tick(world, monkeypatch, state):
    from yggdrasil.linear import adapter
    real_atomic, real_json = util.atomic_write, util.write_json
    crashed_at = 0
    for fault in range(1, 20):
        import shutil
        shutil.rmtree(adapter.cache_root(), ignore_errors=True)  # a fresh start: no cache, no local ids
        adapter._ids_path().unlink(missing_ok=True)
        _reconcile_world(adapter, monkeypatch, state)
        writes = [0]

        def crashing(real):
            def write(*args, **kwargs):
                writes[0] += 1
                if writes[0] == fault:
                    raise KeyboardInterrupt("crash at reconciliation write " + str(fault))
                return real(*args, **kwargs)
            return write
        monkeypatch.setattr(util, "atomic_write", crashing(real_atomic))
        monkeypatch.setattr(util, "write_json", crashing(real_json))
        try:
            adapter.refresh()
            finished = True
        except KeyboardInterrupt:
            finished = False
        monkeypatch.setattr(util, "atomic_write", real_atomic)
        monkeypatch.setattr(util, "write_json", real_json)
        adapter.refresh()  # the next tick
        entry, due, cached = _snapshot(adapter)
        assert adapter._sha(cached) == adapter._sha(NEWER) and "pending" not in entry and due == [], (fault, entry, due)
        if finished:
            break
        crashed_at = fault
    assert crashed_at >= 2  # the reconciliation made several durable writes, each one crashed once
