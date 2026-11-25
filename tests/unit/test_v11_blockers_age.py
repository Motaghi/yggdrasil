"""The blockers line of `ygg status` says how old its route map is (O-4, AC-19); `status --json` is unchanged."""
from __future__ import annotations

import json

from test_round2 import clear_caches, package, world  # noqa: F401  (fixture and helper)

SECTIONS = ["Destination", "Frontier", "Blockers", "Decisions so far", "Fog", "Out of scope"]


def _write_route_map(world, at, monkeypatch):
    from yggdrasil import commands
    text = "".join("## " + name + "\n" + ("- waiting on WP-2\n" if name == "Blockers" else "") for name in SECTIONS)
    path = world / "rm.md"
    path.write_text(text)
    monkeypatch.setenv("YGG_NOW", at)
    commands.routemap_write("pm1", "p1", str(path))


def _line(world):
    from yggdrasil import status
    clear_caches()
    return [l for l in status.human(status.build_status()).splitlines() if "blockers" in l]


def test_a_fresh_route_map_names_its_time(world, monkeypatch):
    monkeypatch.setenv("YGG_NOW", "2026-11-22T08:00:00Z")
    package("WP-2", stage=1)
    _write_route_map(world, "2026-11-22T09:00:00Z", monkeypatch)
    monkeypatch.setenv("YGG_NOW", "2026-11-22T09:05:00Z")
    assert _line(world) == ["  blockers (route map of 2026-11-22 09:00 UTC): waiting on WP-2"]


def test_a_change_to_a_package_after_the_map_says_so(world, monkeypatch):
    from yggdrasil import commands
    monkeypatch.setenv("YGG_NOW", "2026-11-22T08:00:00Z")
    package("WP-2", stage=1)
    _write_route_map(world, "2026-11-22T09:00:00Z", monkeypatch)
    monkeypatch.setenv("YGG_NOW", "2026-11-22T10:00:00Z")
    commands.tracker_write("su1", "p1", "criteria", "c", package="WP-2")
    monkeypatch.setenv("YGG_NOW", "2026-11-22T10:05:00Z")
    assert _line(world) == ["  blockers (route map of 2026-11-22 09:00 UTC, older than the latest change to its "
                            "packages or decisions): waiting on WP-2"]


def test_the_json_status_does_not_carry_the_label(world, monkeypatch):
    from yggdrasil import status
    monkeypatch.setenv("YGG_NOW", "2026-11-22T08:00:00Z")
    package("WP-2", stage=1)
    _write_route_map(world, "2026-11-22T09:00:00Z", monkeypatch)
    clear_caches()
    text = json.dumps(status.build_status())
    assert "route map of" not in text and "written_at" not in text


def test_a_rewrite_of_a_package_after_the_map_says_so(world, monkeypatch):
    from yggdrasil import commands
    monkeypatch.setenv("YGG_NOW", "2026-11-22T08:00:00Z")
    package("WP-2", stage=1)
    _write_route_map(world, "2026-11-22T09:00:00Z", monkeypatch)
    monkeypatch.setenv("YGG_NOW", "2026-11-22T10:00:00Z")
    commands.tracker_write("pm1", "p1", "work-package", "Build, reworded", stage="1", package="WP-2")
    monkeypatch.setenv("YGG_NOW", "2026-11-22T10:05:00Z")
    assert "older than the latest change" in _line(world)[0]


def test_a_change_dated_after_now_is_clock_disagreement_and_does_not_make_the_map_stale(world, monkeypatch):
    from yggdrasil import commands
    monkeypatch.setenv("YGG_NOW", "2026-11-22T11:00:00Z")  # a package written by a session that read another clock
    package("WP-2", stage=1)
    _write_route_map(world, "2026-11-22T09:00:00Z", monkeypatch)
    monkeypatch.setenv("YGG_NOW", "2026-11-22T09:05:00Z")
    assert _line(world) == ["  blockers (route map of 2026-11-22 09:00 UTC): waiting on WP-2"]
    monkeypatch.setenv("YGG_NOW", "2026-11-22T10:00:00Z")
    commands.tracker_write("su1", "p1", "criteria", "c", package="WP-2")
    monkeypatch.setenv("YGG_NOW", "2026-11-22T10:05:00Z")
    assert "older than the latest change" in _line(world)[0]  # a change before now still counts
