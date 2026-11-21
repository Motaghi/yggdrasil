"""Suite-wide collection rules that sit outside the frozen acceptance folders.

Ruling 134 (19 Nov 2026): the A6 North Star tests are frozen ahead of the work they describe, so they are
expected to fail until A6 is built. They run and are reported as expected failures, and a test that starts
passing shows up as XPASS rather than going unnoticed.
"""

import pytest

EXPECTED_RED = {
    "tests/acceptance/core/test_a6_northstar_and_page.py": "A6 North Star is frozen ahead of its build (ruling 134)",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        reason = EXPECTED_RED.get(item.nodeid.split("::", 1)[0])
        if reason:
            item.add_marker(pytest.mark.xfail(reason=reason, strict=False))
