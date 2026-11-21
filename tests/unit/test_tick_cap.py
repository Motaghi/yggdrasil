"""Founder ruling D4: configurable per-tick launch cap (tick_cap, max_launches_per_tick, tick_budget_seconds)."""

import pytest

from yggdrasil import conductor, config, util


def clear():
    config.effective.cache_clear()
    config.file_layers.cache_clear()
    config.profile.cache_clear()


@pytest.fixture
def ticking(tmp_path, monkeypatch):
    """A tick whose queue, admission, launches and clock are faked; each settle delay costs 30 s."""
    for key in [k for k in __import__("os").environ if k.startswith("YGG_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("YGG_FILES_ROOT", str(tmp_path / "tracker"))
    monkeypatch.setenv("YGG_TRACKER", "files")
    cfg = tmp_path / "config.yaml"
    monkeypatch.setenv("YGG_CONFIG", str(cfg))
    clock = {"now": 0.0}
    launched = []
    monkeypatch.setattr(conductor.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(conductor.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    monkeypatch.setattr(conductor, "settle_delay", lambda: 30.0)
    monkeypatch.setattr(conductor, "candidates", lambda: ["n" + str(i) for i in range(10)])
    monkeypatch.setattr(conductor, "admit", lambda: (True, "ok"))
    monkeypatch.setattr(conductor, "reconcile", lambda: None)
    monkeypatch.setattr(conductor, "heimdall_tick", lambda: [])
    monkeypatch.setattr(conductor.launcher, "launch", lambda node, **kw: launched.append(node))
    from yggdrasil import events, rehydrate
    monkeypatch.setattr(events, "tick", lambda: None)
    monkeypatch.setattr(events, "after_launches", lambda: None)
    monkeypatch.setattr(rehydrate, "replay_journal", lambda: 0)

    def run(text):
        cfg.write_text(text)
        clear()
        clock["now"] = 0.0
        launched.clear()
        result = conductor.tick()
        clear()
        return result["launched"]
    return run


def test_default_both_stops_at_five_launches(ticking):
    assert len(ticking("schema: ygg.config.v1\n")) == 5


def test_count_ignores_the_budget(ticking):
    assert len(ticking("tick_cap: count\nmax_launches_per_tick: 7\ntick_budget_seconds: 1\n")) == 7


def test_time_ignores_the_count(ticking):
    # Launches start at 0, 30, 60, 90 s; the next would start at 120 s, beyond a 100 s budget.
    assert len(ticking("tick_cap: time\nmax_launches_per_tick: 1\ntick_budget_seconds: 100\n")) == 4


def test_both_stops_at_whichever_comes_first(ticking):
    assert len(ticking("tick_cap: both\nmax_launches_per_tick: 9\ntick_budget_seconds: 61\n")) == 3
    assert len(ticking("tick_cap: both\nmax_launches_per_tick: 2\ntick_budget_seconds: 1000\n")) == 2


def test_first_launch_is_never_deferred(ticking):
    assert len(ticking("tick_cap: time\ntick_budget_seconds: 0.001\n")) == 1


@pytest.mark.parametrize("cap,launched,elapsed,expected", [
    ("count", 5, 0, True), ("count", 4, 10_000, False),
    ("time", 99, 200, False), ("time", 1, 211, True),
    ("both", 5, 0, True), ("both", 1, 211, True), ("both", 4, 200, False),
    ("both", 0, 10_000, False),
])
def test_cap_reached(cap, launched, elapsed, expected):
    assert conductor.cap_reached(cap, launched, elapsed, 30, 5, 240) is expected


@pytest.mark.parametrize("text,message", [
    ("tick_cap: sometimes\n", "tick_cap must be both, count or time"),
    ("max_launches_per_tick: 0\n", "max_launches_per_tick must be a positive whole number"),
    ("max_launches_per_tick: 2.5\n", "max_launches_per_tick must be a positive whole number"),
    ("tick_budget_seconds: -1\n", "tick_budget_seconds must be a positive number"),
    ("tick_budget_seconds: soon\n", "tick_budget_seconds must be a positive number"),
])
def test_validation_rejects_bad_tick_settings(tmp_path, monkeypatch, text, message):
    monkeypatch.setenv("YGG_CONFIG", str(tmp_path / "c.yaml"))
    monkeypatch.delenv("YGG_PROFILE", raising=False)
    (tmp_path / "c.yaml").write_text(text)
    clear()
    try:
        with pytest.raises(util.YggError) as error:
            config.validate(check_plugins=False)
        assert error.value.code == 1
        assert message in error.value.message
    finally:
        clear()


def test_presets_inherit_the_defaults():
    for name in ("starter", "solo-builder", "team"):
        values = config._load(config.PRESETS / (name + ".yaml"))
        assert not {"tick_cap", "max_launches_per_tick", "tick_budget_seconds"} & set(values)
    core = config._load(config.CORE)
    assert (core["tick_cap"], core["max_launches_per_tick"], core["tick_budget_seconds"]) == ("both", 5, 240)
