"""`ygg merge lane` resolves `--repo` once to the approved repo path and uses only that path afterwards, so a
link the caller retargets after the checks never changes which repository Yggdrasil's git reads."""

import os
import subprocess

import pytest

from yggdrasil import gitops, mergecheck, quarantine, util


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.hooksPath=/dev/null", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo(path, lane_branch):
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("a\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "base")
    if lane_branch:
        _git(path, "branch", "ygg/p1/lane/tl1")
        _git(path, "branch", "ygg/p1/_integration")
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    home = _repo(tmp_path / "home", lane_branch=True)
    other = _repo(tmp_path / "other", lane_branch=False)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    link = scratch / "repo"
    link.symlink_to(home, target_is_directory=True)
    monkeypatch.setattr("yggdrasil.commands._project_for", lambda name: {"id": name, "pm": "pm1",
                                                                          "programme": "alpha"})
    monkeypatch.setattr(gitops, "approved_repos", lambda project: [{"path": str(home)}])
    monkeypatch.setattr(quarantine, "refuse_box_repo", lambda path, programme: None)
    monkeypatch.setattr("yggdrasil.tracker.issues", lambda project: [])
    monkeypatch.setattr("yggdrasil.envelopes.effective_envelope", lambda project: None)
    monkeypatch.setattr("yggdrasil.t3.close_lane", lambda project, lane: None)
    return home, other, link


def test_a_linked_repo_runs_every_later_step_on_the_approved_path(world, monkeypatch):
    home, other, link = world
    used = []

    def helper(path, label):
        used.append(("helper", path))
        # The caller retargets its link as soon as the checks have passed.
        os.unlink(link)
        link.symlink_to(other, target_is_directory=True)
    monkeypatch.setattr(util, "helper_refusal", helper)
    real_git = gitops.git

    def git(repo, *args, **kwargs):
        used.append(("git", repo))
        return real_git(repo, *args, **kwargs)
    monkeypatch.setattr(gitops, "git", git)
    monkeypatch.setattr(mergecheck, "lane_gate", lambda project, lane, repo, head: used.append(("gate", repo)))

    def merge(repo, *args, **kwargs):
        used.append(("merge", repo))
        return "new"
    monkeypatch.setattr(gitops, "_merge_into", merge)
    result = gitops.merge_lane("p1", "tl1", str(link))
    assert result == "merged ygg/p1/lane/tl1 into ygg/p1/_integration at new\n"
    first = used.index(("helper", str(home)))
    assert used[first:] and all(path == str(home) for _, path in used[first:]), used
    assert ("gate", str(home)) in used and ("merge", str(home)) in used
    assert not any(path == str(link) for _, path in used[first:])


def test_the_approved_path_itself_is_used_unchanged(world, monkeypatch):
    home, _, _ = world
    seen = []
    monkeypatch.setattr(util, "helper_refusal", lambda path, label: seen.append(path))
    monkeypatch.setattr(mergecheck, "lane_gate", lambda project, lane, repo, head: seen.append(repo))
    monkeypatch.setattr(gitops, "_merge_into", lambda repo, *args, **kwargs: seen.append(repo) or "new")
    gitops.merge_lane("p1", "tl1", str(home))
    assert seen == [str(home)] * 3


def test_a_repo_no_approved_pid_names_is_still_refused(world, monkeypatch):
    _, other, _ = world
    with pytest.raises(util.YggError) as error:
        gitops.merge_lane("p1", "tl1", str(other))
    assert error.value.message.endswith("gate: no approved PID of p1 names " + str(other))


def _record_git(monkeypatch, used):
    real_git = gitops.git

    def git(repo, *args, **kwargs):
        used.append(repo)
        return real_git(repo, *args, **kwargs)
    monkeypatch.setattr(gitops, "git", git)


def test_a_relayed_call_runs_no_git_through_the_callers_path(world, monkeypatch):
    home, other, link = world
    monkeypatch.setenv("YGG_VIA_RELAY", "1")
    used = []
    _record_git(monkeypatch, used)
    monkeypatch.setattr(util, "helper_refusal", lambda path, label: None)
    monkeypatch.setattr(mergecheck, "lane_gate", lambda project, lane, repo, head: None)
    monkeypatch.setattr(gitops, "_merge_into", lambda repo, *args, **kwargs: "new")
    gitops.merge_lane("p1", "tl1", str(link))
    assert used and str(link) not in used, used
    assert all(path == str(home) for path in used), used


def test_a_relayed_path_that_does_not_resolve_to_the_approved_repo_is_refused_without_git(world, monkeypatch):
    home, other, link = world
    monkeypatch.setenv("YGG_VIA_RELAY", "1")
    os.unlink(link)
    link.symlink_to(other, target_is_directory=True)
    used = []
    _record_git(monkeypatch, used)
    with pytest.raises(Exception) as caught:
        gitops.merge_lane("p1", "tl1", str(link))
    assert "no approved PID of p1 names" in str(caught.value)
    assert used == [], used


# ---------------------------------------------------------------- merge check and merge main (resolve_repo)

@pytest.fixture
def check_world(world, monkeypatch):
    home, other, link = world
    monkeypatch.setattr(mergecheck, "approved_repo_paths", lambda project: [str(home)])
    monkeypatch.setattr("yggdrasil.tracker.project", lambda name: {"id": name, "programme": "alpha"})
    return home, other, link


def _record_repo_home(monkeypatch, used):
    real_git = gitops.git

    def git(repo, *args, **kwargs):
        used.append(repo)
        return real_git(repo, *args, **kwargs)
    monkeypatch.setattr(gitops, "git", git)
    real_home = gitops.repo_home

    def repo_home(path):
        used.append(path)
        return real_home(path)
    monkeypatch.setattr(gitops, "repo_home", repo_home)


def test_a_relayed_merge_check_repo_runs_no_git_through_the_callers_path(check_world, monkeypatch):
    home, other, link = check_world
    monkeypatch.setenv("YGG_VIA_RELAY", "1")
    used = []
    _record_repo_home(monkeypatch, used)
    assert mergecheck.resolve_repo("p1", str(link)) == str(home)
    assert used == [], used


def test_a_relayed_merge_check_path_that_does_not_resolve_to_the_approved_repo_is_refused_without_git(
        check_world, monkeypatch):
    home, other, link = check_world
    monkeypatch.setenv("YGG_VIA_RELAY", "1")
    os.unlink(link)
    link.symlink_to(other, target_is_directory=True)
    used = []
    _record_repo_home(monkeypatch, used)
    with pytest.raises(util.YggError) as error:
        mergecheck.resolve_repo("p1", str(link))
    assert error.value.message.endswith("gate: no approved PID of p1 names " + str(link))
    assert used == [], used


def test_a_relayed_worktree_of_the_approved_repo_is_refused_without_git(check_world, monkeypatch, tmp_path):
    home, _, _ = check_world
    worktree = tmp_path / "worktree"
    _git(home, "worktree", "add", "-q", str(worktree), "ygg/p1/lane/tl1")
    monkeypatch.setenv("YGG_VIA_RELAY", "1")
    used = []
    _record_repo_home(monkeypatch, used)
    with pytest.raises(util.YggError):
        mergecheck.resolve_repo("p1", str(worktree))
    assert used == [], used


def test_a_direct_caller_may_still_name_a_worktree_of_the_approved_repo(check_world, monkeypatch, tmp_path):
    home, _, _ = check_world
    worktree = tmp_path / "worktree"
    _git(home, "worktree", "add", "-q", str(worktree), "ygg/p1/lane/tl1")
    monkeypatch.delenv("YGG_VIA_RELAY", raising=False)
    assert mergecheck.resolve_repo("p1", str(worktree)) == str(home)


def test_merge_check_uses_only_the_approved_path_after_the_match(check_world, monkeypatch):
    home, other, link = check_world
    monkeypatch.setenv("YGG_VIA_RELAY", "1")
    monkeypatch.setattr(gitops, "_caller_is_pm", lambda project: None)
    seen = []

    def pair(path, project_id):
        seen.append(path)
        # The caller retargets its link as soon as the match has passed.
        os.unlink(link)
        link.symlink_to(other, target_is_directory=True)
        raise util.refuse("stop here")
    monkeypatch.setattr(mergecheck, "pair", pair)
    with pytest.raises(util.YggError):
        mergecheck.check_verb("p1", str(link))
    assert seen == [str(home)]


# ---------------------------------------------------------------- the relayed --repo selector (cli)

def _relayed_cli(monkeypatch, home):
    from yggdrasil import cli
    monkeypatch.setenv("YGG_VIA_RELAY", "1")
    monkeypatch.setenv("YGG_PROGRAMME", "alpha")
    monkeypatch.setattr("yggdrasil.state.all_nodes", lambda: {})
    monkeypatch.setattr("yggdrasil.tracker.programme", lambda name: {"id": name})
    monkeypatch.setattr("yggdrasil.tracker.programme_home", lambda programme: str(home))
    return cli


def test_the_relayed_repo_selector_returns_the_registered_home_not_the_callers_path(world, monkeypatch):
    home, other, link = world
    cli = _relayed_cli(monkeypatch, home)
    assert cli._relayed_repo(str(link)) == str(home)


@pytest.mark.parametrize("action", ["check", "main", "lane"])
def test_relayed_merge_verbs_get_the_registered_home_in_place_of_the_callers_path(world, monkeypatch, action):
    home, other, link = world
    cli = _relayed_cli(monkeypatch, home)
    got = []
    monkeypatch.setattr(mergecheck, "check_verb", lambda project, repo: got.append(repo) or "")
    monkeypatch.setattr(gitops, "merge_main", lambda project, repo, print_only: got.append(repo) or "")
    monkeypatch.setattr(gitops, "merge_lane", lambda project, lane, repo: got.append(repo) or "")
    argv = ["merge", action, "p1", "--repo", str(link)] + (["--lane", "tl1"] if action == "lane" else [])
    cli.dispatch(cli.build().parse_args(argv))
    assert got == [str(home)]
