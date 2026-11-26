"""v1.1.1: after the user switches `launcher_cli` from Codex to Claude Code on a project whose lane is a clone,
`ygg status` must not resolve the clone's checkout (trusted code never reads, writes or runs git in a box). The
lane keeps the runtime recorded when it was made, so the permission-path calculation for Claude Code never
applies to it. Real state and the frozen Codex suite's fixture (read, never changed)."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SUITE = Path(__file__).resolve().parents[1] / "acceptance-v1-1-codex"
YGG = Path(sys.executable).parent / "ygg"


@pytest.fixture
def driver(tmp_path, monkeypatch):
    if not YGG.is_file():
        pytest.skip("the ygg CLI of this environment is missing")
    monkeypatch.setenv("YGG_BIN", str(YGG))
    monkeypatch.syspath_prepend(str(SUITE))
    from driver import CodexDriver
    d = CodexDriver(tmp_path / "world")
    try:
        d.standard(codex=False, launcher="codex")
        d.authorised()
        result = d.launch(binary="codex")
        assert result.returncode == 0, result.stderr
        yield d
    finally:
        d.cleanup_processes()
        d.restore_permissions()


def _status_touching(d):
    from yggdrasil import status
    touched = []
    original = os.path.realpath

    def track(path, *args, **kwargs):
        if str(path).startswith(str(d.B)):
            touched.append(str(path))
        return original(path, *args, **kwargs)

    with patch.dict(os.environ, d.env, clear=True), patch.object(os.path, "realpath", track):
        view = status.build_status()
    return view, touched


def test_status_never_resolves_a_clone_checkout_after_a_runtime_switch(driver):
    d = driver
    d.config(launcher_cli="claude-code")
    d.send()
    view, touched = _status_touching(d)
    assert touched == []
    node = next(n for n in view["nodes"] if n["id"] == "pm1tl1")
    assert "agent-permissions" not in node["blocked_by"]


def test_status_on_an_unchanged_codex_lane_resolves_nothing_in_the_box(driver):
    d = driver
    d.send()
    view, touched = _status_touching(d)
    assert touched == []
