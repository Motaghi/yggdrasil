"""The conductor's wait between ticks ends at a wall-clock deadline, however long each sleep runs."""

from yggdrasil import conductor


class SlowClock:
    """A fake clock whose sleeps each take half as long again as asked, like a throttled background job."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds * 1.5


def test_slow_sleeps_do_not_stretch_the_wait(monkeypatch):
    clock = SlowClock()
    monkeypatch.setattr(conductor.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(conductor.time, "sleep", clock.sleep)
    conductor.wait_between_ticks(15, lambda: False)
    assert 15 <= clock.now - 1000.0 <= 15.3
    assert max(clock.sleeps) <= 0.2


def test_a_stop_is_seen_within_one_check(monkeypatch):
    clock = SlowClock()
    monkeypatch.setattr(conductor.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(conductor.time, "sleep", clock.sleep)
    conductor.wait_between_ticks(15, lambda: clock.now - 1000.0 >= 3)
    assert 3 <= clock.now - 1000.0 <= 3.3
