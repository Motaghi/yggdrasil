"""Signalling a process needs an exact identity, or a timezone-ambiguous one verified by its environment."""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from yggdrasil import interlock, lifecycle, stop, util


def test_stop_must_not_signal_a_reused_pid_matching_only_in_another_zone(tmp_path, monkeypatch):
    monkeypatch.setenv('TZ', 'America/Los_Angeles')
    time.tzset()
    monkeypatch.setenv('YGG_STATE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setenv('YGG_FILES_ROOT', str(tmp_path / 'tracker'))
    monkeypatch.setenv('YGG_PLATFORM_STATE', str(tmp_path / 'platform'))
    monkeypatch.delenv('YGG_NODE', raising=False)
    monkeypatch.delenv('YGG_ACTIVATION', raising=False)
    child = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)
    try:
        actual = util.process_start(child.pid)
        old = datetime.fromtimestamp(int(util.start_epoch(actual)) - 8 * 3600, timezone.utc).strftime(util.LSTART)
        receipt = tmp_path / 'state/activations/act-0001.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({'node': 'pm1tl1', 'programme': 'alpha', 'activation': 'act-0001',
                                      'pid': child.pid, 'process_started_at': old, 'exited_at': None,
                                      'agent_nonce': 'nonce-of-the-old-process'}))
        assert not interlock._nonce_verified(child.pid, 'act-0001', 'nonce-of-the-old-process')
        try:
            stop.stop('alpha')
        except util.YggError:
            pass
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        assert child.poll() is None, f'unrelated child was signalled; old={old}, actual={actual}, rc={child.returncode}'
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        time.tzset()


def test_second_dst_fold_legacy_receipt_must_still_identify_its_live_process(monkeypatch):
    monkeypatch.setenv('TZ', 'UTC')
    time.tzset()
    zone = ZoneInfo('Europe/Copenhagen')
    local = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    epoch = int(local.timestamp())
    monkeypatch.setattr(util, '_system_zone', lambda: zone)
    child = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)
    try:
        monkeypatch.setattr(util, '_kernel_start', lambda pid: (epoch, 123456))
        recorded = local.strftime(util.LSTART)
        assert lifecycle.evidence(child.pid, recorded) == 'alive', (recorded, epoch, util.start_epochs(recorded))
    finally:
        child.kill()
        child.wait()
        time.tzset()


@pytest.mark.xfail(reason='known gap, tracked separately: on Linux a boot-time (btime) step shifts the identity')
def test_linux_live_identity_must_survive_a_wall_clock_step(tmp_path, monkeypatch):
    import sys
    monkeypatch.setenv('YGG_STATE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(os, 'sysconf', lambda name: 100)
    monkeypatch.setattr(util, 'pid_alive', lambda pid: True)
    boot = [1700000000]
    stat = '123 (agent (name)) S ' + ' '.join(['0'] * 18 + ['1234567'] + ['0'] * 30)
    original_read = Path.read_text
    def read(path, *args, **kwargs):
        if str(path) == '/proc/123/stat':
            return stat
        if str(path) == '/proc/stat':
            return f'cpu 1 2 3 4\nbtime {boot[0]}\n'
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    recorded = util.process_start(123)
    assert recorded == 's:1700012345.670000'
    boot[0] += 3600
    actual = util.process_start(123)
    assert lifecycle.evidence(123, recorded) == 'alive', (recorded, actual)


def test_a_verified_legacy_receipt_is_signalled_and_its_exact_identity_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv('TZ', 'America/Los_Angeles')
    time.tzset()
    state_root = tmp_path / 'state'
    monkeypatch.setenv('YGG_STATE_ROOT', str(state_root))
    monkeypatch.setenv('YGG_FILES_ROOT', str(tmp_path / 'tracker'))
    monkeypatch.setenv('YGG_PLATFORM_STATE', str(tmp_path / 'platform'))
    monkeypatch.delenv('YGG_NODE', raising=False)
    monkeypatch.delenv('YGG_ACTIVATION', raising=False)
    env = dict(os.environ, YGG_ACTIVATION='act-0001', YGG_ACTIVATION_NONCE='n-1', YGG_STATE_ROOT=str(state_root))
    env.pop('TZ')
    import sys  # a platform binary's environment can't be read on macOS; the agent here is ordinary Python
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True, env=env)
    time.sleep(0.3)
    try:
        actual = util.process_start(child.pid)
        # As the conductor (TZ=UTC) recorded it, read here in another zone: matches only ambiguously.
        recorded = datetime.fromtimestamp(int(util.start_epoch(actual)), timezone.utc).strftime(util.LSTART)
        assert util.start_match(recorded, actual) == 'ambiguous'
        receipt = state_root / 'activations/act-0001.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({'node': 'pm1tl1', 'programme': 'alpha', 'activation': 'act-0001',
                                      'pid': child.pid, 'process_started_at': recorded, 'exited_at': None,
                                      'agent_nonce': 'n-1'}))
        try:
            stop.stop('alpha')
        except util.YggError:
            pass
        child.wait(timeout=15)
        saved = json.loads(receipt.read_text())
        assert saved['process_start_identity'] == actual and saved['process_started_at'] == recorded
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        time.tzset()


def test_an_exact_identity_never_matches_ambiguously(monkeypatch):
    child = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)
    try:
        actual = util.process_start(child.pid)
        assert util.start_match(actual, actual) == 'exact'
        other = 's:%d.000001' % int(util.start_epoch(actual) - 3600)
        assert util.start_match(other, actual) is None
    finally:
        child.kill()
        child.wait()


def test_stop_signals_a_verified_second_dst_fold_legacy_agent(tmp_path, monkeypatch):
    import sys  # an ordinary Python agent, so its environment can be read on macOS
    monkeypatch.setenv('TZ', 'UTC')
    time.tzset()
    state_root = tmp_path / 'state'
    monkeypatch.setenv('YGG_STATE_ROOT', str(state_root))
    monkeypatch.setenv('YGG_FILES_ROOT', str(tmp_path / 'tracker'))
    monkeypatch.setenv('YGG_PLATFORM_STATE', str(tmp_path / 'platform'))
    monkeypatch.delenv('YGG_NODE', raising=False)
    monkeypatch.delenv('YGG_ACTIVATION', raising=False)
    zone = ZoneInfo('Europe/Copenhagen')
    local = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    epoch = int(local.timestamp())
    legacy = local.strftime(util.LSTART)
    env = {'PATH': os.defpath, 'YGG_STATE_ROOT': str(state_root), 'YGG_ACTIVATION': 'act-0001',
           'YGG_ACTIVATION_NONCE': 'expected'}
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True, env=env)
    time.sleep(0.3)
    try:
        receipt = state_root / 'activations/act-0001.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({'node': 'pm1tl1', 'programme': 'alpha', 'activation': 'act-0001',
                                      'pid': child.pid, 'process_started_at': legacy, 'exited_at': None,
                                      'agent_nonce': 'expected'}))
        monkeypatch.setattr(util, '_system_zone', lambda: zone)
        monkeypatch.setattr(util, '_kernel_start', lambda pid: (epoch, 123456))
        assert interlock._nonce_verified(child.pid, 'act-0001', 'expected')
        try:
            stop.stop('alpha')
        except util.YggError:
            pass
        child.wait(timeout=15)
        assert json.loads(receipt.read_text())['process_start_identity'] == 's:%d.123456' % epoch
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        time.tzset()


@pytest.mark.parametrize('zone', ['Europe/Copenhagen', 'America/Los_Angeles', 'Pacific/Auckland'])
def test_a_local_reading_has_one_epoch_outside_a_repeated_hour(zone, monkeypatch):
    monkeypatch.setenv('TZ', zone)
    time.tzset()
    try:
        for when in (datetime(2026, 1, 14, 10, 0, 7), datetime(2026, 7, 14, 10, 0, 7)):
            text = when.strftime(util.LSTART)
            assert util.start_epochs(text, zones=('local',)) == {int(when.timestamp())}, text
        zoned = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo('Europe/Copenhagen'))
        if zone == 'Europe/Copenhagen':  # the repeated hour reads both ways
            assert util.start_epochs(zoned.strftime(util.LSTART), zones=('local',)) == \
                {int(zoned.timestamp()), int(zoned.replace(fold=1).timestamp())}
    finally:
        monkeypatch.undo()
        time.tzset()
