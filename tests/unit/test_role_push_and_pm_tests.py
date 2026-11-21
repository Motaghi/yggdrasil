"""Role texts from pilot 2 (20 Nov 2026): a team lead pushes only when there is a remote and the brief allows it, and
a project manager never promises to run test commands it has no rule for."""
from __future__ import annotations

from pathlib import Path

import yggdrasil

ROLES = Path(yggdrasil.__file__).parent / "data" / "roles"


def _text(name: str) -> str:
    return " ".join((ROLES / name).read_text().split())


def test_the_team_lead_pushes_only_with_a_remote_and_a_brief_that_allows_it():
    text = _text("tl.md")
    assert "Push only when the repository has an `origin` remote and the brief and PID allow pushing" in text
    assert "Otherwise don't push: your commits stay on your lane branch and reach main through `ygg merge`." in text
    assert "Push only your lane, with exactly" not in text


def test_the_project_manager_relies_on_the_senior_users_acceptance_for_tests():
    text = _text("pm.md")
    assert "Before you make a provisional decision on a claim, record the command you ran and its output in the " \
           "decision." in text  # frozen AC-38 sentence
    assert "are not yours to run" in text
    assert "rely on the senior user's acceptance, which is recorded on the tested commit" in text
    assert "is a claim until you have run the command yourself" not in text
    # Pilot 2 run 4: the PM held a package back waiting for acceptance, which only comes after the review.
    assert "Acceptance comes after the review" in text
    assert "never hold a package back from `ygg wp complete` waiting for acceptance" in text


def test_the_pm_merges_a_lane_after_each_acceptance_before_the_next_package():
    text = _text("pm.md")
    assert "In a git project, give each lane one package at a time." in text
    assert "Authorise the next package on a lane only after the previous one is accepted and you have merged the " \
           "lane into integration with `ygg merge lane <project> --lane <team lead> --repo <repository path>`" in text
    assert "without this merge the next package's review also sees the earlier package's files" in text
    assert "A document-folder project has no lane merge" in text
    lead = _text("tl.md")
    assert "your PM authorises the next one only after merging the accepted one into integration" in lead


def test_every_role_raises_a_blocker_through_ygg_and_probes_through_the_test_command():
    # Pilot 2 run 5: a team lead ran an unapproved probe script, was denied, and wrote "I'm blocked" only in its
    # final output, so nobody was owed anything and the project stalled.
    text = _text("shared.md")
    assert "A blocker or question written only in your final output reaches nobody, and the work stalls." in text
    assert "A team lead raises it with `ygg issue raise --from <you> --reason <text>`." in text
    assert "`ygg send --from <you> --to <node> --kind issue --body <text>`" in text
    assert "(a worker to the node it reports to)" in text
    assert "A package reviewer puts it in its verdict file; a programme reviewer follows its own reporting steps." \
        in text
    assert "the git writes your role is given" in text
    assert "A team lead or worker who needs to see how something behaves writes the probe as a test" in text
