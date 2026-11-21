"""A Linear issue write whose outcome is unknown keeps its pending hash; the next tick and a rehydrate settle it
from what Linear holds, so a committed authorisation never loses its owed launch (I4; landing review round 2)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from driver import Driver
from yggdrasil import util
from yggdrasil.linear import adapter

pytestmark = pytest.mark.skipif(os.environ.get("ACCEPTANCE_TARGET", "real") != "real", reason="real target only")


def test_a_committed_authorisation_survives_an_ambiguous_response_and_a_wipe(tmp_path, monkeypatch):
    fake_folder = Path(__file__).resolve().parents[1] / "acceptance/fakes"
    wrapper = tmp_path / "linear-wrapper.py"
    wrapper.write_text(
        "import sys\nfrom pathlib import Path\n"
        + "sys.path.insert(0, " + repr(str(fake_folder)) + ")\n"
        + "import linear_a5\noriginal = linear_a5.Workspace.graphql\n"
        + "def graphql(self, body, *, execute=True):\n"
        + "    result = original(self, body, execute=execute)\n"
        + "    control = self.directory / 'commit-then-fail'\n"
        + "    if execute and control.exists() and body.get('operationName') == 'YggIssueUpdate':\n"
        + "        control.unlink()\n"
        + "        return 503, {'errors': [{'message': 'response failed after commit'}]}, True\n"
        + "    return result\n"
        + "linear_a5.Workspace.graphql = graphql\n"
        + "linear_a5.serve(Path(sys.argv[1]))\n"
    )
    original_popen = subprocess.Popen

    def popen(args, *extra, **kwargs):
        if len(args) > 2 and str(args[1]) == str(fake_folder / "linear_a5.py"):
            args = [sys.executable, str(wrapper), str(args[-1])]
        return original_popen(args, *extra, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    with Driver(tmp_path) as driver:
        config = tmp_path / "fast.yaml"
        config.write_text("settle_delay_seconds: 0.01\nlaunch_confirm_seconds: 0.1\n")
        driver.env["YGG_CONFIG"] = str(config)
        with driver.a5_linear():
            driver.core_tree()
            driver.ready()
            driver.package(criteria=True)
            (driver.linear_dir / "commit-then-fail").touch()
            failed = driver.run("wp", "authorise", "alpha-p1", "--package", "WP-41", "--lane", "pm1tl1")
            assert failed.returncode == 8 and "ambiguous" in failed.stderr, failed.stderr
            state = driver.a5_linear_state()
            issue = next(row for row in state["issues"] if row["title"].startswith("WP-41 "))
            remote, _ = util.split_frontmatter(adapter.decode(issue["description"]))
            assert remote["state"] == "authorised" and remote["owed"]
            registry = json.loads(adapter.decode(next(d for d in state["documents"]
                                                      if d["title"] == adapter.REGISTRY)["content"]))
            assert registry["issues"]["alpha-p1/WP-41"]["pending"]  # kept: the write may have committed
            retry = driver.run("wp", "authorise", "alpha-p1", "--package", "WP-41", "--lane", "pm1tl1")
            assert "someone other than the app" not in retry.stderr, retry.stderr  # its own write, adopted
            driver.empty_state()
            rebuilt = driver.run("rehydrate")
            assert rebuilt.returncode == 0, rebuilt.stderr
            view = driver.status()
            node = next(row for row in view["nodes"] if row["id"] == "pm1tl1")
            packages = [row for row in view["work_packages"] if row["id"] == "WP-41"]
            assert driver.a5_linear_live_schema_errors() == []
            observed = {"remote_authorised": True, "packages_after_wipe": packages,
                        "node_state": node["state"], "held_reason": node["held_reason"],
                        "held_launches": view["held_launches"]}
            assert packages, json.dumps(observed)


def test_a_committed_pending_authorisation_survives_a_client_crash_and_a_wipe(tmp_path, monkeypatch):
    fake_folder = Path(__file__).resolve().parents[1] / 'acceptance/fakes'
    wrapper = tmp_path / 'linear-wrapper.py'
    wrapper.write_text(
        'import sys, time\nfrom pathlib import Path\n'
        + 'sys.path.insert(0, ' + repr(str(fake_folder)) + ')\n'
        + 'import linear_a5\noriginal = linear_a5.Workspace.graphql\n'
        + 'def graphql(self, body, *, execute=True):\n'
        + '    result = original(self, body, execute=execute)\n'
        + "    control = self.directory / 'commit-then-wait'\n"
        + "    if execute and control.exists() and body.get('operationName') == 'YggIssueUpdate':\n"
        + '        control.unlink()\n'
        + "        (self.directory / 'committed').touch()\n"
        + '        deadline = time.monotonic() + 20\n'
        + "        while not (self.directory / 'release-response').exists() and time.monotonic() < deadline:\n"
        + '            time.sleep(0.02)\n'
        + '    return result\n'
        + 'linear_a5.Workspace.graphql = graphql\n'
        + 'linear_a5.serve(Path(sys.argv[1]))\n'
    )
    original_popen = subprocess.Popen

    def popen(args, *extra, **kwargs):
        if len(args) > 2 and str(args[1]) == str(fake_folder / 'linear_a5.py'):
            args = [sys.executable, str(wrapper), str(args[-1])]
        return original_popen(args, *extra, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', popen)
    with Driver(tmp_path) as driver:
        config = tmp_path / 'fast.yaml'
        config.write_text('settle_delay_seconds: 0.01\nlaunch_confirm_seconds: 0.1\n')
        driver.env['YGG_CONFIG'] = str(config)
        with driver.a5_linear():
            driver.core_tree()
            driver.ready()
            driver.package(criteria=True)
            (driver.linear_dir / 'commit-then-wait').touch()
            process = original_popen([driver.cli, 'wp', 'authorise', 'alpha-p1', '--package', 'WP-41', '--lane', 'pm1tl1'], cwd=driver.cwd, env=driver.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 15
                while not (driver.linear_dir / 'committed').exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert (driver.linear_dir / 'committed').exists(), 'update never committed'
                process.kill()
                process.wait(timeout=5)
            finally:
                (driver.linear_dir / 'release-response').touch()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            state = driver.a5_linear_state()
            issue = next(row for row in state['issues'] if row['title'].startswith('WP-41 '))
            remote, _ = util.split_frontmatter(adapter.decode(issue['description']))
            assert remote['state'] == 'authorised' and remote['owed']
            registry_doc = next(d for d in state['documents'] if d['title'] == adapter.REGISTRY)
            registry = adapter.decode(registry_doc['content'])
            assert json.loads(registry)['issues']['alpha-p1/WP-41']['pending'], registry
            driver.empty_state()
            rebuilt = driver.run('rehydrate')
            assert rebuilt.returncode == 0, rebuilt.stderr
            view = driver.status()
            node = next(row for row in view['nodes'] if row['id'] == 'pm1tl1')
            packages = [row for row in view['work_packages'] if row['id'] == 'WP-41']
            assert driver.a5_linear_live_schema_errors() == []
            observed = {'pending_survived': True, 'remote_authorised': True, 'packages_after_wipe': packages, 'node_state': node['state'], 'held_reason': node['held_reason'], 'held_launches': view['held_launches']}
            assert packages, json.dumps(observed)
            assert node["held_reason"] and "alpha-p1/WP-41" in node["held_reason"]  # visible until settled
            assert driver.run("conductor", "tick").returncode == 0  # settles the write from Linear
            registry_doc = next(d for d in driver.a5_linear_state()["documents"] if d["title"] == adapter.REGISTRY)
            assert "pending" not in json.loads(adapter.decode(registry_doc["content"]))["issues"]["alpha-p1/WP-41"]
            node = next(row for row in driver.status()["nodes"] if row["id"] == "pm1tl1")
            assert not (node["held_reason"] or "").startswith("the Linear write")


# Landing review round 3: an update answered incompletely, and a crash around its confirmation.
def _configure(driver, tmp_path):
    path = tmp_path / "fast.yaml"
    path.write_text("settle_delay_seconds: 0.01\nlaunch_confirm_seconds: 0.1\n")
    driver.env["YGG_CONFIG"] = str(path)


def _registry(driver):
    doc = next(d for d in driver.a5_linear_state()["documents"] if d["title"] == adapter.REGISTRY)
    return json.loads(adapter.decode(doc["content"]))


@pytest.mark.parametrize("response", ["503", "graphql-error", "missing-payload"])
def test_a_committed_update_with_an_incomplete_response_keeps_its_obligation(tmp_path, monkeypatch, response):
    fake_folder = Path(__file__).resolve().parents[1] / "acceptance/fakes"
    wrapper = tmp_path / "linear-wrapper.py"
    replacement = ("{'data': {'issueUpdate': {'success': True, 'issue': None}}, "
                   "'errors': [{'message': 'response resolver failed after commit', "
                   "'path': ['issueUpdate', 'issue'], 'extensions': {'code': 'INTERNAL_SERVER_ERROR'}}]}"
                   if response != "missing-payload" else "{'data': {'issueUpdate': {'success': True, 'issue': None}}}")
    status = 503 if response == "503" else 200
    wrapper.write_text(
        "import sys\nfrom pathlib import Path\n"
        + "sys.path.insert(0, " + repr(str(fake_folder)) + ")\n"
        + "import linear_a5\noriginal = linear_a5.Workspace.graphql\n"
        + "def graphql(self, body, *, execute=True):\n"
        + "    result = original(self, body, execute=execute)\n"
        + "    control = self.directory / 'commit-then-fail'\n"
        + "    if execute and control.exists() and body.get('operationName') == 'YggIssueUpdate':\n"
        + "        control.unlink()\n"
        + "        return " + str(status) + ", " + replacement + ", True\n"
        + "    return result\n"
        + "linear_a5.Workspace.graphql = graphql\n"
        + "linear_a5.serve(Path(sys.argv[1]))\n"
    )
    original_popen = subprocess.Popen

    def popen(args, *extra, **kwargs):
        if len(args) > 2 and str(args[1]) == str(fake_folder / "linear_a5.py"):
            args = [sys.executable, str(wrapper), str(args[-1])]
        return original_popen(args, *extra, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    with Driver(tmp_path) as driver:
        _configure(driver, tmp_path)
        with driver.a5_linear():
            driver.core_tree()
            driver.ready()
            driver.package(criteria=True)
            (driver.linear_dir / "commit-then-fail").touch()
            result = driver.run("wp", "authorise", "alpha-p1", "--package", "WP-41", "--lane", "pm1tl1")
            assert result.returncode == 8, result.stderr
            remote_issue = next(i for i in driver.a5_linear_state()["issues"] if i["title"].startswith("WP-41 "))
            header, _ = util.split_frontmatter(adapter.decode(remote_issue["description"]))
            assert header["state"] == "authorised" and header["owed"]
            pending = _registry(driver)["issues"]["alpha-p1/WP-41"].get("pending")
            driver.empty_state()
            result = driver.run("rehydrate")
            assert result.returncode == 0, result.stderr
            for _ in range(2):
                assert driver.run("conductor", "tick").returncode == 0
            status = driver.status()
            node = next(n for n in status["nodes"] if n["id"] == "pm1tl1")
            observed = {"response": response, "pending": pending,
                        "remote_owed": header["owed"], "node": node,
                        "packages": status["work_packages"], "starts": driver.launches("pm1tl1"),
                        "held_launches": status["held_launches"]}
            assert pending and (driver.launches("pm1tl1") or node["held_reason"]), observed


@pytest.mark.parametrize("fault", [5, 6, 7])
def test_a_crash_around_the_confirmation_keeps_the_obligation(tmp_path, fault):
    with Driver(tmp_path) as driver:
        _configure(driver, tmp_path)
        with driver.a5_linear():
            driver.core_tree()
            driver.ready()
            driver.package(criteria=True)
            result = driver.run("wp", "authorise", "alpha-p1", "--package", "WP-41", "--lane", "pm1tl1",
                                env={"YGG_FAULT_AT": "write:" + str(fault)})
            assert result.returncode == 97, result.stderr
            remote_issue = next(i for i in driver.a5_linear_state()["issues"] if i["title"].startswith("WP-41 "))
            header, _ = util.split_frontmatter(adapter.decode(remote_issue["description"]))
            assert header["state"] == "authorised" and header["owed"]
            for _ in range(3):
                result = driver.run("conductor", "tick")
                assert result.returncode == 0, result.stderr
            status = driver.status()
            node = next(n for n in status["nodes"] if n["id"] == "pm1tl1")
            observed = {"fault": fault, "entry": _registry(driver)["issues"]["alpha-p1/WP-41"],
                        "remote_owed": header["owed"], "node": node,
                        "packages": status["work_packages"], "starts": driver.launches("pm1tl1"),
                        "held_launches": status["held_launches"]}
            assert driver.launches("pm1tl1") or node["held_reason"] or "pm1tl1" in status["held_launches"], observed


# Landing review round 4: reconciliation settles only from a complete read.
def _install_fake(tmp_path, monkeypatch):
    fake_folder = Path(__file__).resolve().parents[1] / 'acceptance/fakes'
    wrapper = tmp_path / 'linear-wrapper.py'
    wrapper.write_text(
        'import sys, json\nfrom pathlib import Path\n'
        + 'sys.path.insert(0, ' + repr(str(fake_folder)) + ')\n'
        + 'import linear_a5\noriginal = linear_a5.Workspace.graphql\n'
        + 'def graphql(self, body, *, execute=True):\n'
        + "    control = self.directory / 'refuse-next'\n"
        + '    if execute and control.exists():\n'
        + '        refusal = json.loads(control.read_text())\n'
        + "        if body.get('operationName') == refusal['operation']:\n"
        + '            control.unlink()\n'
        + "            if refusal.get('mode') == 'incomplete-version':\n"
        + '                status, answer, ok = original(self, body, execute=execute)\n'
        + "                answer['data']['issue'].pop('updatedAt', None)\n"
        + '                return status, answer, ok\n'
        + "            if 'body' in refusal:\n"
        + "                return refusal['status'], refusal['body'], True\n"
        + "            return refusal['status'], {'errors': [{'message': 'review injected refusal', 'extensions': {'code': refusal['code']}}]}, True\n"
        + '    return original(self, body, execute=execute)\n'
        + 'linear_a5.Workspace.graphql = graphql\n'
        + 'linear_a5.serve(Path(sys.argv[1]))\n'
    )
    original_popen = subprocess.Popen

    def popen(args, *extra, **kwargs):
        if len(args) > 2 and str(args[1]) == str(fake_folder / 'linear_a5.py'):
            args = [sys.executable, str(wrapper), str(args[-1])]
        return original_popen(args, *extra, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', popen)


@pytest.mark.parametrize('refusal', ['forbidden', 'unreachable'])
def test_a_failed_reconciliation_read_keeps_the_unsynced_mark(tmp_path, monkeypatch, refusal):
    _install_fake(tmp_path, monkeypatch)
    with Driver(tmp_path) as driver:
        _configure(driver, tmp_path)
        with driver.a5_linear():
            driver.core_tree()
            driver.ready()
            driver.package(criteria=True)
            result = driver.run('wp', 'authorise', 'alpha-p1', '--package', 'WP-41', '--lane', 'pm1tl1',
                                env={'YGG_FAULT_AT': 'write:7'})
            assert result.returncode == 97, result.stderr
            assert 'pending' not in _registry(driver)['issues']['alpha-p1/WP-41']
            ids = driver.json_file(driver.state / 'linear-ids.json')
            assert ids['unsynced:alpha-p1/WP-41']
            remote = next(i for i in driver.a5_linear_state()['issues'] if i['title'].startswith('WP-41 '))
            header, _ = util.split_frontmatter(adapter.decode(remote['description']))
            assert header['state'] == 'authorised' and header['owed']
            (driver.linear_dir / 'refuse-next').write_text(json.dumps({
                'operation': 'YggIssueRead', 'status': 400 if refusal == 'forbidden' else 503,
                'code': 'FORBIDDEN' if refusal == 'forbidden' else 'INTERNAL_SERVER_ERROR'}))
            for _ in range(3):
                result = driver.run('conductor', 'tick')
                assert result.returncode == 0, result.stderr
            status = driver.status()
            node = next(n for n in status['nodes'] if n['id'] == 'pm1tl1')
            remote_after = next(i for i in driver.a5_linear_state()['issues'] if i['title'].startswith('WP-41 '))
            after_header, _ = util.split_frontmatter(adapter.decode(remote_after['description']))
            assert after_header['state'] == 'authorised' and after_header['owed']
            observed = {'refusal': refusal, 'node': node, 'packages': status['work_packages'],
                        'starts': driver.launches('pm1tl1'), 'held_launches': status['held_launches'],
                        'remote_owed': after_header['owed'],
                        'marker': driver.json_file(driver.state / 'linear-ids.json').get('unsynced:alpha-p1/WP-41')}
            print(json.dumps(observed))
            assert driver.launches('pm1tl1') or node['held_reason'] or 'pm1tl1' in status['held_launches'], observed


@pytest.mark.parametrize('fault', [5, 6, 7, 8, 9])
def test_recovery_from_a_crash_at_each_write_launches_once(tmp_path, fault):
    with Driver(tmp_path) as driver:
        _configure(driver, tmp_path)
        with driver.a5_linear():
            driver.core_tree()
            driver.ready()
            driver.package(criteria=True)
            result = driver.run('wp', 'authorise', 'alpha-p1', '--package', 'WP-41', '--lane', 'pm1tl1',
                                env={'YGG_FAULT_AT': 'write:' + str(fault)})
            assert result.returncode == 97, result.stderr
            for _ in range(3):
                assert driver.run('conductor', 'tick').returncode == 0
            before = len(driver.launches('pm1tl1'))
            assert before == 1
            for _ in range(2):
                result = driver.run('rehydrate')
                assert result.returncode == 0, result.stderr
                assert driver.run('conductor', 'tick').returncode == 0
            assert len(driver.launches('pm1tl1')) == before


def test_an_idle_tick_reads_no_issue_when_nothing_is_due(tmp_path):
    with Driver(tmp_path) as driver:
        _configure(driver, tmp_path)
        with driver.a5_linear():
            driver.core_tree()
            driver.ready()
            driver.quiesce()
            before = len(driver.a5_linear_requests())
            for _ in range(3):
                assert driver.run('conductor', 'tick').returncode == 0
            requests = [r['body']['operationName'] for r in driver.a5_linear_requests()[before:]
                        if r['path'] == '/graphql']
            print(json.dumps({'idle_operations': requests}))
            assert requests == ['YggRevision'] * 3


@pytest.mark.parametrize('response', ['null-issue', 'incomplete-version'])
def test_an_incomplete_reconciliation_read_keeps_pending(tmp_path, monkeypatch, response):
    _install_fake(tmp_path, monkeypatch)
    with Driver(tmp_path) as driver:
        _configure(driver, tmp_path)
        with driver.a5_linear():
            driver.core_tree()
            driver.ready()
            driver.package(criteria=True)
            result = driver.run('wp', 'authorise', 'alpha-p1', '--package', 'WP-41', '--lane', 'pm1tl1',
                                env={'YGG_FAULT_AT': 'write:5'})
            assert result.returncode == 97, result.stderr
            entry = _registry(driver)['issues']['alpha-p1/WP-41']
            assert entry.get('pending')
            remote = next(i for i in driver.a5_linear_state()['issues'] if i['title'].startswith('WP-41 '))
            header, _ = util.split_frontmatter(adapter.decode(remote['description']))
            assert header['state'] == 'authorised' and header['owed']
            assert _registry(driver)['issues']['alpha-p1/WP-41'].get('pending')
            refusal = {'operation': 'YggIssueRead', 'status': 200}
            if response == 'null-issue':
                refusal['body'] = {'data': {'issue': None}}
            else:
                refusal['mode'] = 'incomplete-version'
            (driver.linear_dir / 'refuse-next').write_text(json.dumps(refusal))
            for _ in range(3):
                result = driver.run('conductor', 'tick')
                assert result.returncode == 0, result.stderr
            status = driver.status()
            node = next(n for n in status['nodes'] if n['id'] == 'pm1tl1')
            remote_after = next(i for i in driver.a5_linear_state()['issues'] if i['title'].startswith('WP-41 '))
            after_header, _ = util.split_frontmatter(adapter.decode(remote_after['description']))
            assert after_header['state'] == 'authorised' and after_header['owed']
            observed = {'response': response, 'registry': _registry(driver)['issues']['alpha-p1/WP-41'],
                        'node': node, 'packages': status['work_packages'],
                        'remote_owed': after_header['owed'],
                        'starts': driver.launches('pm1tl1'), 'held_launches': status['held_launches']}
            print(json.dumps(observed))
            assert driver.launches('pm1tl1') or node['held_reason'] or 'pm1tl1' in status['held_launches'], observed
