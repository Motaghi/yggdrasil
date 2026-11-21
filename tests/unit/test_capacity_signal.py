"""Capacity failures are classified from real provider signals only (table of every reviewed case)."""

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)
from test_round3 import receipt_file

CAPACITY = [
    ("HTTP 429 Too Many Requests", "http-status"),  # the M1 fake launcher's capacity output
    ("HTTP 429", "http-status"),
    ("HTTP 429.", "http-status"),  # dev #2 round 20: sentence punctuation
    ("Request failed: HTTP 429, retry later", "http-status"),
    ("HTTP/1.1 429 Too Many Requests", "http-status"),
    ('API Error: {"status":429,"message":"Resource exhausted"}', "json-status"),  # dev #2 round 20
    ('{"status":429,"message":"Request rejected"}', "json-status"),  # QA round 20
    ('{"message":"Request rejected","status_code":429}', "json-status"),  # QA round 20, other key order
    ('{"error":{"type":"rate_limit_error","message":"see https://docs.example/limits"}}', "rate-limit"),
    ('API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}', "api-error-status"),
    ("status code 429", "api-error-status"),
    ("request failed: status code: 429", "api-error-status"),  # QA round 20
    ("Error: 429", "api-error-status"),  # QA round 20
    ("Rate limit exceeded, retry later", "rate-limit"),  # QA round 20
    ('{"type":"rate_limit_error"}', "rate-limit"),  # QA round 20
    ("Too Many Requests", "too-many-requests"),
    ("429 rate limit", "rate-limit"),
    ("rate limit", "rate-limit"),
    ("Rate limited, retry later", "rate-limit"),
    ("rate_limit_error", "rate-limit"),
    ("quota exceeded", "quota"),
    ("Your quota is exhausted", "quota"),  # QA round 20
    ("You exceeded your current quota", "quota"),
    ("insufficient_quota", "quota"),
    ("usage limit reached", "usage-limit"),
    ("Resource exhausted", "resource-exhausted"),
]

NOT_CAPACITY = [
    "[Errno 2] No such file or directory: '/private/var/folders/T/pytest-of-u/pytest-4296/test0/missing-claude'",
    "No such file or directory: /tmp/429/claude",
    "/tmp/pytest-429/run: not found",
    "No such file or directory: /tmp/rate-limit/missing-claude",  # dev #2 round 20
    "Error: Cannot find module express-rate-limit",  # dev #2 round 20
    "FileNotFoundError: '/tmp/code:429'",  # QA round 20
    "Cannot read session with id=code:429",  # QA round 20
    "error code:429 in fixture",
    "act-0429 failed",
    "line 429: syntax error",
    "build 4290 failed",
    "quota.json missing",
    "rate-limit",
    "Error opening session act-429",  # QA round 20
    "FileNotFoundError: '/tmp/429'",  # QA round 20
    "FileNotFoundError: '/tmp/rate-limit/429'",  # QA round 20
    "Cannot open quota limit configuration file",  # QA round 20
    "Permission denied: /tmp/session",  # QA round 20
]


@pytest.mark.parametrize("tail,signal", CAPACITY)
def test_real_capacity_signals_are_capacity(tail, signal):
    from yggdrasil import launcher
    assert launcher.capacity_signal(tail) == signal


@pytest.mark.parametrize("tail", NOT_CAPACITY)
def test_paths_ids_and_other_text_are_not_capacity(tail):
    from yggdrasil import launcher
    assert launcher.capacity_signal(tail) is None


def test_failed_spawn_under_a_429_temp_path_is_failed(world):
    from yggdrasil import launcher
    receipt_file("act-0091", node="tl1", outcome="pending", exit_code=127, exited_at=util.iso(), pid=None,
                 spawn_failed=True,
                 stderr_tail="[Errno 2] No such file or directory: '/tmp/pytest-of-u/pytest-4296/missing-claude'")
    assert launcher.classify(launcher.receipt("act-0091")) == "failed"
    receipt_file("act-0092", node="tl1", outcome="pending", exit_code=1, exited_at=util.iso(), pid=999999,
                 stderr_tail="HTTP 429 Too Many Requests")
    assert launcher.classify(launcher.receipt("act-0092")) == "capacity"


def test_exit_fact_class_uses_the_capacity_classifier():
    # The `exited` fact's capacity flag (and so the derived capacity backoff) comes from the same classifier.
    from yggdrasil import launcher
    base = {"exit_code": 1, "exited_at": util.iso(), "pid": 999999}
    assert launcher.exit_fields(dict(base, stderr_tail="Error: Cannot find module express-rate-limit")) == \
        {"capacity": False}
    assert launcher.exit_fields(dict(base, stderr_tail="No such file or directory: /tmp/429/claude")) == \
        {"capacity": False}
    found = launcher.exit_fields(dict(base, stderr_tail="HTTP 429 Too Many Requests"))
    assert found["capacity"] is True and found["backoff_until"]
    assert launcher.exit_fields(dict(base, exit_code=0, stderr_tail="rate limit")) == {"capacity": False}
