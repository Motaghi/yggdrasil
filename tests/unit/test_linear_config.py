"""Under the Linear tracker a settle delay is required, so a busy hour stays inside the request window."""

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


def test_a_zero_settle_delay_is_refused_with_the_linear_tracker(world, monkeypatch):
    from yggdrasil import config
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nsettle_delay_seconds: 0\n")
    clear_caches()
    config.validate()  # the files tracker accepts it (the frozen work-ahead fixture uses it)
    monkeypatch.setenv("YGG_TRACKER", "linear")
    clear_caches()
    with pytest.raises(util.YggError) as error:
        config.validate()
    assert error.value.code == util.EXIT_USAGE
    assert "settle_delay_seconds must be a positive number of seconds with tracker: linear" in error.value.message
    (world / "config.yaml").write_text("launch_confirm_seconds: 0.5\nsettle_delay_seconds: 5\n")
    clear_caches()
    config.validate()
