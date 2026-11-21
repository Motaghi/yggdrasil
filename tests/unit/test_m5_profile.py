"""Core support a private profile needs (M5): L1 on the live pools, and the profile's platform pause dir."""

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (fixture and helper)


def write_profile(world, text):
    path = world / "profile.yaml"
    path.write_text(text)
    return path


def test_l1_resolves_to_live_pools_and_launched_nodes_to_background(world, monkeypatch):
    from yggdrasil import models
    monkeypatch.setenv("YGG_PROFILE", str(write_profile(world, (
        "pools:\n  l1_claude: live-a\n  l1_codex: live-b\n  launched_claude: bg-a\n  launched_codex: bg-b\n"))))
    clear_caches()
    l1 = models.resolve("l1")
    assert l1["pool"] == "live-a" and l1["pair_pool"] == "live-b"
    pm = models.resolve("pm1")
    assert pm["pool"] == "bg-a" and pm["pair_pool"] == "bg-b"


def test_profile_platform_pause_dir_stops_launches(world, monkeypatch):
    from yggdrasil import launcher, state
    platform = world / "platform-state"
    platform.mkdir()
    monkeypatch.delenv("YGG_PLATFORM_STATE", raising=False)
    monkeypatch.setenv("YGG_PROFILE", str(write_profile(world, "platform_pause_dir: " + str(platform) + "\n")))
    clear_caches()
    assert not state.platform_paused()
    (platform / "pause").write_text("")
    assert state.platform_paused()
    try:
        launcher.launch("pm1")
    except util.YggError as error:
        assert error.code == util.EXIT_PAUSED
    else:
        raise AssertionError("launch must be refused while the platform is paused")
