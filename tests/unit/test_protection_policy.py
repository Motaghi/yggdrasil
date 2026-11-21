import json

from yggdrasil.commands import protection_problems

SAFE = {"enforce_admins": {"enabled": True}, "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "required_pull_request_reviews": {"required_approving_review_count": 1}}


def test_safe_policy_has_no_problems():
    assert protection_problems(json.dumps(SAFE)) == []


def test_each_unsafe_field_is_named():
    for field, value in (("enforce_admins", {"enabled": False}), ("allow_force_pushes", {"enabled": True}),
                         ("allow_deletions", {"enabled": True}),
                         ("required_pull_request_reviews", {"required_approving_review_count": 0})):
        problems = protection_problems(json.dumps(dict(SAFE, **{field: value})))
        assert len(problems) == 1 and field in problems[0]


def test_bare_exists_answer_proves_nothing():
    problems = protection_problems(json.dumps({"url": "https://example.invalid/protection"}))
    assert len(problems) == 4
    assert protection_problems("not json") == ["protection answer is not JSON"]
