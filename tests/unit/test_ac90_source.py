"""Ruling 75 / AC-90: a delivery setting the programme layer supplies reports source `programme`."""

from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


def set_programme_config(values):
    from yggdrasil import tracker
    programme = tracker.programme("alpha")
    programme["config"] = values
    tracker.save_programme(programme)
    clear_caches()


def test_programme_delivery_approach_reports_programme_source(world):
    from yggdrasil import status
    set_programme_config({"delivery_approach": "agile"})
    approach, source, _ = status.effective_delivery("p1")
    assert (approach, source) == ("agile", "programme")


def test_programme_cadence_reports_programme_source(world):
    from yggdrasil import status
    set_programme_config({"cadence": "timebox", "timebox_hours": 8})
    _, source, cadence = status.effective_delivery("p1")
    assert (source, cadence) == ("programme", "timebox")


def fake_profile(values):
    def profile():
        return dict(values)
    profile.cache_clear = lambda: None  # the shared fixture clears the real cached loader
    return profile


def set_pid(**values):
    from yggdrasil import commands, tracker
    text = tracker.read_document("p1", "pid")
    extra = "".join("\n" + key + ": " + str(value) for key, value in values.items())
    commands.tracker_write("pm1", "p1", "pid", text.replace("schema: ygg.pid.v1", "schema: ygg.pid.v1" + extra, 1))
    clear_caches()


# Round 20: a programme cadence that applies (approach agile, from the PID) reports `programme`.
def test_pid_agile_approach_with_programme_cadence_reports_programme(world):
    from yggdrasil import status
    set_programme_config({"cadence": "timebox", "timebox_hours": 8})
    set_pid(delivery_approach="agile")
    assert status.effective_delivery("p1") == ("agile", "programme", "timebox")


# Round 20: a programme cadence that does not apply (approach waterfall) does not change the source.
def test_unused_programme_cadence_does_not_change_the_source(world):
    from yggdrasil import status
    set_programme_config({"cadence": "flow"})
    set_pid(delivery_approach="waterfall")
    assert status.effective_delivery("p1") == ("waterfall", "pid", None)


def test_unused_programme_cadence_under_a_profile_waterfall_default(world, monkeypatch):
    from yggdrasil import config, status
    set_programme_config({"cadence": "flow"})
    monkeypatch.setattr(config, "profile", fake_profile({"delivery_approach_default": "waterfall"}))
    assert status.effective_delivery("p1") == ("waterfall", "profile", None)


# Round 20: a cadence-only PID or profile setting names its own layer, not core.
def test_cadence_only_pid_setting_reports_pid(world):
    from yggdrasil import status
    set_pid(cadence="timebox", timebox_hours=8)
    approach, source, cadence = status.effective_delivery("p1")
    assert (approach, source, cadence) == ("agile", "pid", "timebox")


def test_cadence_only_profile_default_reports_profile(world, monkeypatch):
    from yggdrasil import config, status
    monkeypatch.setattr(config, "profile", fake_profile({"agile_cadence_default": "timebox"}))
    assert status.effective_delivery("p1") == ("agile", "profile", "timebox")
