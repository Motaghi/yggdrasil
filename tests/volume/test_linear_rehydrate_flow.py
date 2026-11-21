"""Rebuilding from Linear alone keeps the id counters, so the next allocation continues after a wipe."""

import os

import pytest

pytest.importorskip("graphql")

from driver import Driver


@pytest.mark.skipif(os.environ.get("ACCEPTANCE_TARGET", "real") != "real", reason="real target only")
def test_allocation_after_a_rehydrate_continues_the_counters(tmp_path):
    with Driver(tmp_path) as driver:
        with driver.a5_linear():
            driver.core_tree()
            driver.ready()
            first = driver.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "one")
            assert first.returncode == 0, first.stderr
            driver.empty_state()
            assert driver.run("rehydrate").returncode == 0
            second = driver.run("send", "--from", "pm1", "--to", "pm1tl1", "--kind", "work", "--body", "two")
            assert second.returncode == 0, second.stderr
            pending = {i["msg_id"] for i in (driver.status() or {}).get("inbox_pending", []) if i["node"] == "pm1tl1"}
            assert len(pending) >= 2  # the second message got a new id, the first survived the wipe
