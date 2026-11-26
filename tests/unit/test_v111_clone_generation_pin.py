"""v1.1.1 lane clones: a launch pins the clone's identity and generation, and a delayed supervisor of a
generation that has since been replaced is refused before its command line is built, with no trusted git and
no fallback to it. Each test fails before its fix."""

import subprocess
from pathlib import Path

import pytest

from yggdrasil import clones, launcher, util
from yggdrasil.util import YggError


def _git(*args, cwd=None):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=E",
                           "-c", "user.email=e@example.invalid", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "files"))
    for name in ("YGG_TEST_CODEX_VERSION", "YGG_TEST_PLATFORM", "YGG_FAULT_AT", "YGG_TEST_ASSEMBLY_HOLD"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "home"
    _git("init", "-q", "--initial-branch=main", str(path))
    (path / "a.txt").write_text("a")
    _git("add", "a.txt", cwd=path)
    _git("commit", "-q", "-m", "a", cwd=path)
    _git("branch", "ygg/p/lane/tl1", cwd=path)
    monkeypatch.setattr(clones, "clone_config", lambda *a, **k: "[core]\n\trepositoryformatversion = 0\n")
    monkeypatch.setattr(clones, "lane_alive", lambda *a, **k: False)
    monkeypatch.setattr(clones, "report", lambda *a, **k: {"result": "unavailable"})
    return str(path)


def _superseded(home):
    """Generation 1 assembled and pinned by a launch; its index damaged; another launch rebuilds generation 2."""
    first = clones.assemble("p", "tl1", home)
    pin = clones.pin(first)
    Path(first["path"], ".git", "index").write_bytes(b"damaged")
    clones.quarantine("p", "tl1", reason="the clone was odd (check 4)", next_step="rebuild")
    second = clones.do_next("p", "tl1")
    assert second["generation"] == 2
    return first, pin, second


def _recording(monkeypatch):
    calls = []

    def fake(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "true\n", "")
    monkeypatch.setattr(launcher, "git", fake)
    monkeypatch.setattr(util, "trusted_git", fake)
    return calls


def test_pin_records_identity_and_generation(home):
    record = clones.assemble("p", "tl1", home)
    pin = clones.pin(record)
    assert pin == {"project": "p", "name": "tl1", "kind": "lane", "generation": 1, "nonce": record["nonce"],
                   "box": record["box"], "path": record["path"], "box_device": record["box_device"],
                   "box_inode": record["box_inode"]}
    assert clones.pin_refusal(pin) is None


def test_superseded_generation_is_refused_with_no_trusted_git(home, monkeypatch):
    first, pin, second = _superseded(home)
    calls = _recording(monkeypatch)
    data = {"cwd": first["path"], "role": "tl", "project": "p", "activation": "act-1", "clone": pin}
    refusal = clones.pin_refusal(pin)
    assert refusal and "generation 1" in refusal and "generation 2" in refusal
    with pytest.raises(YggError) as raised:
        launcher.codex_sandbox(data, {"role": "tl", "project": "p"})
    assert "generation 1" in raised.value.message
    assert not [c for c in calls if first["box"] in " ".join(map(str, c))]


def test_clone_path_without_a_pin_never_falls_back_to_trusted_git(home, monkeypatch):
    first, _, _ = _superseded(home)
    calls = _recording(monkeypatch)
    data = {"cwd": first["path"], "role": "tl", "project": "p", "activation": "act-1"}
    with pytest.raises(YggError):
        launcher.codex_sandbox(data, {"role": "tl", "project": "p"})
    assert not calls


def test_live_pinned_generation_builds_its_command_line_with_no_trusted_git(home, monkeypatch):
    _, _, second = _superseded(home)
    calls = _recording(monkeypatch)
    data = {"cwd": second["path"], "role": "tl", "project": "p", "activation": "act-2",
            "clone": clones.pin(second)}
    args = launcher.codex_sandbox(data, {"role": "tl", "project": "p"})
    assert args[:2] == ["--sandbox", "workspace-write"]
    assert not calls


def test_replaced_box_of_the_same_generation_is_refused(home):
    record = clones.assemble("p", "tl1", home)
    pin = dict(clones.pin(record), box_inode=record["box_inode"] + 1)
    assert "is not the box" in clones.pin_refusal(pin)


def test_supervisor_refuses_a_superseded_clone_before_the_command_line(home, monkeypatch):
    first, pin, _ = _superseded(home)
    built = []
    monkeypatch.setattr(launcher, "codex_sandbox", lambda *a: built.append(a) or [])
    refusal = launcher.clone_refusal({"clone": pin, "cli": "codex"})
    assert refusal and "generation 1" in refusal
    assert not built
