import pytest


UNDELIVERED = "1 alert couldn't be delivered; ask your main session to check it"


def alert_given(d, *, macos=None, attempts=3, hang=False, headless=False,
                quiesce_pms=False):
    d.auto_alerts(headless=headless)
    if quiesce_pms:
        d.quiesce_core_pms()
    d.config(alert_max_attempts=attempts)
    if macos is not None:
        d.config(alert_macos=macos)
    if hang:
        d.config(alert_send_timeout_seconds=2, alert_tick_budget_seconds=5)
        d.fakes(t3={'sleep_seconds': 600})
    d.raise_alert('ygg-launch-refused', node='pm1tl1')
    return d.alert_id()


def steps(d, identifier, channel):
    return [r['state'] for r in d.alert_delivery(identifier) if r.get('channel') == channel]


def abandoned(d, identifier):
    assert 'abandoned' in [r['state'] for r in d.alert_delivery(identifier)]
    alerts = d.alerts('ygg-alert-undelivered')
    assert len(alerts) == 1 and alerts[0]['class'] == 'log'
    assert UNDELIVERED in d.run('status').stdout


def at_tick(d, seconds):
    d.later(seconds)
    d.tick()


@pytest.mark.ac('AC-09')
@pytest.mark.release('v1.0-blocker')
def test_ac09_row1_timeout_t3_once_macos_retries_to_cap(a11_1):
    d = a11_1
    d.fakes(osascript={'exit': 1})
    identifier = alert_given(d, hang=True)
    for seconds in [0, 15, 45]:
        at_tick(d, seconds)
    abandoned(d, identifier)
    assert steps(d, identifier, 't3') == ['pending', 'sent', 'timed-out']
    assert len(d.alert_calls('t3', identifier)) == 1
    assert len(d.calls('osascript')) == 3
    before = d.notifications()
    at_tick(d, 105)
    assert d.notifications() == before


@pytest.mark.ac('AC-09')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('mac_fails', [False, True])
def test_ac09_row2_timeout_never_resends_t3(a11_1, mac_fails):
    d = a11_1
    d.fakes(osascript={'exit': 1 if mac_fails else 0})
    identifier = alert_given(d, hang=True, headless=True, quiesce_pms=True)
    _, elapsed = d.elapsed(lambda: at_tick(d, 0))
    assert elapsed <= 10
    assert steps(d, identifier, 't3') == ['pending', 'sent', 'timed-out']
    assert steps(d, identifier, 'osascript') == ['pending', 'sent', 'failed' if mac_fails else 'delivered']
    if mac_fails:
        assert d.alert_delivery(identifier)[-1]['state'] == 'pending'
    before = d.notifications()
    d.fakes(t3={'sleep_seconds': 0, 'exit': 0}, osascript={'sleep_seconds': 0, 'exit': 0})
    at_tick(d, 15)
    assert len(d.alert_calls('t3', identifier)) == 1
    if mac_fails:
        assert len(d.calls('osascript')) == 2
        assert steps(d, identifier, 'osascript')[-1] == 'delivered'
    else:
        assert d.notifications() == before
        assert len(d.calls('osascript')) == 1


@pytest.mark.ac('AC-09')
@pytest.mark.release('v1.0-blocker')
def test_ac09_row3_no_macos_timeout_abandoned_at_once(a11_1):
    d = a11_1
    identifier = alert_given(d, macos='off', hang=True)
    at_tick(d, 0)
    abandoned(d, identifier)
    assert steps(d, identifier, 't3') == ['pending', 'sent', 'timed-out']
    assert len(d.alert_calls('t3', identifier)) == 1
    assert d.calls('osascript') == []
    before = d.notifications()
    at_tick(d, 15)
    at_tick(d, 45)
    assert d.notifications() == before


@pytest.mark.ac('AC-10')
@pytest.mark.release('v1.0-blocker')
def test_ac10_row1_macos_off_only_t3_counts(a11_1):
    d = a11_1
    d.fakes(t3={'exit': 1})
    identifier = alert_given(d, macos='off', attempts=2, headless=True)
    at_tick(d, 0)
    at_tick(d, 15)
    abandoned(d, identifier)
    at_tick(d, 45)
    assert len(d.calls('t3')) == 2
    assert [step for step in steps(d, identifier, 't3') if step == 'failed'] == ['failed', 'failed']
    assert d.calls('osascript') == []
    assert steps(d, identifier, 'osascript') == []


@pytest.mark.ac('AC-10')
@pytest.mark.release('v1.0-blocker')
def test_ac10_row2_profile_off_without_thread_has_no_channel(a11_1):
    d = a11_1
    d.profile(alert_macos='off')
    d.config(alert_forwarder='auto', alert_max_attempts=2)
    d.raise_alert('ygg-launch-refused', node='pm1tl1')
    identifier = d.alert_id()
    at_tick(d, 0)
    abandoned(d, identifier)
    at_tick(d, 15)
    assert d.calls('t3') == [] and d.calls('osascript') == []


@pytest.mark.ac('AC-10')
@pytest.mark.release('v1.0-blocker')
def test_ac10_row2_project_cannot_set_macos_channel(a11_1):
    d = a11_1
    d.write(d.cwd / 'yggdrasil.yaml', 'alert_macos: off\n')
    result = d.run('status')
    assert result.returncode == 1
    assert 'alert_macos may be set only in a config named by YGG_CONFIG, the home config, a profile or the environment' in result.stderr


@pytest.mark.ac('AC-10')
@pytest.mark.release('v1.0-blocker')
def test_ac10_row3_phone_success_does_not_clear_undelivered(a11_1):
    d = a11_1
    d.profile(alert_command=[str(d.root / 'bin/phone-push')])
    d.fakes(t3={'exit': 1}, **{'phone-push': {'exit': 0}})
    identifier = alert_given(d, macos='off', attempts=2, headless=True)
    for seconds in [0, 15, 45]:
        at_tick(d, seconds)
    assert len(d.calls('phone-push')) == 1
    assert steps(d, identifier, 'phone-push')[-1:] == ['delivered']
    assert len(d.calls('t3')) == 2
    assert d.calls('osascript') == []
    assert 'abandoned' in [r['state'] for r in d.alert_delivery(identifier)]
    assert UNDELIVERED in d.run('status').stdout
