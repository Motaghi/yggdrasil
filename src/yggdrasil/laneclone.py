"""The launcher's Codex work-area slots, filled by the lane-clone module (v1.1.1): `launcher.LANE_BOX` is
`laneclone:launch_box` and `launcher.TEST_BOX` is `laneclone:test_box`. Both return the work-area dict the Codex
launch uses: `box`, `checkout` (`<box>/checkout`), `box_device`, `box_inode`, `prompt` (lines for the agent's
prompt) and `kind` (`lane` or `test`). The work itself lives in `clones`."""

from __future__ import annotations

from . import clones, tracker


def launch_box(node: dict, nodes: dict, lane_owner: str, activation: str) -> dict:
    """A Codex team lead or worker: the lane's live box, assembled, recovered, replaced or tended first. The
    prompt lines are the tending's own (a replaced clone, the lane moved); other notes (`fast-forward`) stay for
    `clones.flush_notes(activation)`, which the launch calls once the launch folder exists."""
    programme = tracker.programme(node["programme"])
    home = str(tracker.programme_home(programme))
    clones.lane_checkout(node, home, node["project"], lane_owner, activation)
    record = clones.read(node["project"], lane_owner) or {}
    notes = clones._NOTES.get(activation) or {}
    prompt = notes.pop("clone-prompt.txt", [])
    return clones.box_fields(record, prompt, node["project"], lane_owner)


def test_box(node: dict, nodes: dict, activation: str, target: dict) -> dict:
    """A testing Codex senior user: a fresh test clone of `target["branch"]`, detached at its head."""
    return clones.test_box(node, nodes, activation, target)
