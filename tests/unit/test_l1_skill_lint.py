"""The generic main-session (L1) skill states the signed A11 rules (AC-25, A11.1 AC-08 and AC-14) and never names
the routine-noise phrases at all (AC-25 row 2)."""

from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[2] / "skills" / "yggdrasil-l1" / "SKILL.md"
BANNED = ("Routine:", "Nothing new", "Watch re-armed", "watch confirms", "No change in the last hour")

SENTENCES = (
    "When a watch wakes you and nothing has changed, end the turn without a message.",
    "Send the user one short summary each hour while work is active, and nothing else unless it needs them or "
    "changes the outcome.",
    "Check state with ygg status or git log before you report it.",
    "Confirm a diagnosis from receipts before you act on it.",
    "Re-arm your watch on every wake.",
    "Before you apply an override, name its scope, and never turn a project override into a machine-wide change.",
    "Never run ygg agents allow-standard, ygg agents allow-minimal, ygg ruling record, ygg stage end, ygg stage "
    "approve, ygg merge main or ygg wp cancel --finish unless the user asked for it in this conversation.",
    "When a merge-ready alert arrives, tell the user in one plain sentence that the project passed its checks, and "
    "when the user says \"merge this\", run ygg merge main <project> for them.",
    "When a merge check fails because main clashes with the work, tell the user in one plain sentence, and only "
    "when the user asks, merge main into the project's integration branch by hand, keeping what both sides meant, "
    "then let the merge check run again.",
    "When the user asks to finish an interrupted cancel, run ygg wp cancel <project> --finish <lane-node> without "
    "--yes, show the user every path and commit it lists, and add --yes only after the user confirms in this "
    "conversation.",
)


def _text():
    return SKILL.read_text()


@pytest.mark.parametrize("sentence", SENTENCES)
def test_the_skill_states_each_signed_sentence(sentence):
    assert sentence in _text()


def test_the_routine_phrases_appear_nowhere():
    lowered = _text().casefold()
    assert [phrase for phrase in BANNED if phrase.casefold() in lowered] == []


def test_the_skill_keeps_its_frontmatter():
    text = _text()
    assert text.startswith("---\nname: yggdrasil-l1\ndescription: ")
    assert "\n---\n" in text[4:]
