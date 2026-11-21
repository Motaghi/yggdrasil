"""Landing review finding 1: a package reopened after the merge into main keeps its project open."""

from test_round2 import package, world  # noqa: F401  (shared fixture)

from yggdrasil import mergecheck, reducer, tracker


def _approve_all_stages():
    data = tracker.milestones("p1")
    for stage in data.get("stages") or []:
        stage["state"] = "approved"
    data["all_approved_at"] = "2026-11-16T02:00:00Z"
    tracker.save_milestones("p1", data)


def _set(package_id, value):
    issue = tracker.read_issue("p1", package_id)
    issue.header["state"] = value
    issue.save()


def _merged_into_main(monkeypatch, project_state="ready-to-merge"):
    project = tracker.project("p1")
    project["state"] = project_state
    tracker.save_project(project)
    monkeypatch.setattr(mergecheck, "is_git_project", lambda project: True)
    monkeypatch.setattr(mergecheck, "approved_repo_paths", lambda project_id: [])
    monkeypatch.setattr(mergecheck, "pair", lambda repo, project_id: ("a" * 40, "b" * 40))
    monkeypatch.setattr(mergecheck, "contains", lambda repo, older, newer: True)  # main holds the integration head
    monkeypatch.setattr(mergecheck, "record", lambda project_id: {"state": "passed"})


def test_a_package_reopened_after_merge_main_keeps_the_project_open_and_its_work_owed(world, monkeypatch):
    package("WP-1")
    _approve_all_stages()
    _merged_into_main(monkeypatch)
    monkeypatch.setattr(mergecheck, "approved_repo_paths", lambda project_id: ["repo"])
    _set("WP-1", "authorised")  # the PM's reopen, after merge main and before the tick
    project = tracker.project("p1")
    project["state"] = "executing"  # a reopen sends the project back to executing
    tracker.save_project(project)
    mergecheck._project_step(tracker.project("p1"))
    assert tracker.project("p1")["state"] != "closed"
    assert any(i for i in reducer.owed("tl1"))  # WP-1's work stays owed


def test_close_rechecks_the_packages_under_the_lock(world, monkeypatch):
    package("WP-1")
    _approve_all_stages()
    _merged_into_main(monkeypatch, "executing")
    mergecheck._close("p1")  # WP-1 is authorised, not merged
    assert tracker.project("p1")["state"] == "executing"
    _set("WP-1", "merged")
    mergecheck._close("p1")
    assert tracker.project("p1")["state"] == "closed"
