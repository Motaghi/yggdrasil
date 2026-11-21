import pytest

from yggdrasil import hooks, launcher


def test_unresolved_refusal_says_how_to_get_around_it():
    code, message = hooks.static_checks("Bash", {"command": 'git commit -m "subject\n\nbody"'})
    assert code == 2 and message.startswith("Hard stop (unresolved) denied")
    for advice in ("one simple command per Bash call", "a # and quotes", "git commit -F <file>"):
        assert advice in message


def test_other_hard_stops_keep_their_message():
    assert hooks.static_checks("Bash", {"command": "git push --force origin ygg/a"}) == (
        2, "Hard stop (force) denied: ask the user through your PM.")


@pytest.mark.parametrize("role", sorted(launcher.ROLE_FILES))
def test_every_role_prompt_carries_the_shared_rules(role):
    prompt = launcher.prompt_for({"id": "n1", "role": role, "project": None}, "act-0001")
    for advice in ("one simple command per Bash call", "a `#` and quotes", "`git commit -F <file>`"):
        assert advice in prompt
