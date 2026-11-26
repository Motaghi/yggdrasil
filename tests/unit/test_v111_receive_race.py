"""v1.1.1: a receive checked against one generation of a lane clone never lands after that generation was
replaced. `ygg wp ready` checks the clone's new commits outside the journal lock; a cancel or a replacement that
runs in between builds a new generation at the unchanged home lane head, so the lane ref's compare-and-swap
alone still passes. The final hold must re-check the same `ready` record (generation and nonce) and the package
before any ref or registry write.

Real git, the real receive, cancel and ready, and the frozen Codex suite's fixture (read, never changed) for the
standard project, with a cancel or a replacement run between the checks and the final hold."""

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
        d.standard(codex=True)
        d.authorised()
        result = d.launch(binary="codex")
        assert result.returncode == 0, result.stderr
        yield d
    finally:
        d.cleanup_processes()
        d.restore_permissions()


def _ready_with(d, interleave):
    """Run `ready` for WP-1 with `interleave()` called right after the receive's checks (steps 1 to 7) return."""
    from yggdrasil import clones, review, util
    original = clones.receive
    seen = {}

    def receive_then(*args, **kwargs):
        found = original(*args, **kwargs)
        seen["found"] = found
        interleave()
        return found

    with patch.dict(os.environ, d.env, clear=True), patch.object(clones, "receive", receive_then):
        try:
            seen["result"] = review.ready("pm1tl1", "WP-1")
        except util.YggError as error:
            seen["refusal"] = error.message
        seen["record"] = clones.read("alpha-p1", "pm1tl1")
    return seen


def test_a_cancel_between_the_checks_and_the_final_hold_is_not_undone(driver):
    d = driver
    lane0 = d.lane_head()
    unreceived = d.commit_file(d.clone, "unreceived.txt", "work", "WP-1 work")

    def cancel():
        from yggdrasil import cancel
        cancel.cancel("alpha-p1", ["WP-1"])

    seen = _ready_with(d, cancel)
    assert seen["found"]["head"] == unreceived  # the receive did check the old generation's commit
    assert "refusal" in seen
    record = seen["record"]
    assert record["generation"] == 2
    # The cancel's reset stands: the home lane still names L0, and generation 2 was given only L0.
    assert d.lane_head() == lane0
    assert record["given_head"] == lane0 == d.head(Path(record["path"]))


def test_a_replaced_clone_between_the_checks_and_the_final_hold_refuses_the_receive(driver):
    d = driver
    lane0 = d.lane_head()
    d.commit_file(d.clone, "unreceived.txt", "work", "WP-1 work")

    def replace():
        # What cleanup's recovery and a launch's replacement do: quarantine the live box, then rebuild at L0.
        from yggdrasil import clones
        with clones.lane_lock("alpha-p1", "pm1tl1"):
            clones.quarantine("alpha-p1", "pm1tl1", reason="replaced for the test", next_step="rebuild")
            clones.do_next("alpha-p1", "pm1tl1")

    seen = _ready_with(d, replace)
    assert "refusal" in seen and "replaced" in seen["refusal"]
    record = seen["record"]
    assert record["generation"] == 2 and record["state"] == "ready"
    assert d.lane_head() == lane0
    assert record["given_head"] == lane0 == d.head(Path(record["path"]))


def test_an_unchanged_generation_still_receives(driver):
    d = driver
    head = d.commit_file(d.clone, "received.txt", "work", "WP-1 work")
    seen = _ready_with(d, lambda: None)
    assert "refusal" not in seen, seen.get("refusal")
    assert d.lane_head() == head
    assert seen["record"]["generation"] == 1 and seen["record"]["given_head"] == head
