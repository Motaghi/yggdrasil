import pytest


@pytest.mark.ac('AC-08')
@pytest.mark.release('v1.0-blocker')
def test_ac08_row3_l1_clash_and_finish_instructions(a11_1):
    text = a11_1.l1_skill()
    assert "When a merge check fails because main clashes with the work, tell the user in one plain sentence, and only when the user asks, merge main into the project's integration branch by hand, keeping what both sides meant, then let the merge check run again." in text
    assert 'When the user asks to finish an interrupted cancel, run ygg wp cancel <project> --finish <lane-node> without --yes, show the user every path and commit it lists, and add --yes only after the user confirms in this conversation.' in text
