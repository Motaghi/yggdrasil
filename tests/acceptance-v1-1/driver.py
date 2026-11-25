"""CLI, HTTP, filesystem and process boundaries for the v1.1 suite."""
from contextlib import contextmanager
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

import yaml
import stub

SWITCHES = ('dashboard', 'dashboard_serve', 'summary', 'doctor', 'confirm_window',
            'retry_pm_reset', 't3_live_send', 'retry_cap', 'stage_close')
HERE = Path(__file__).resolve().parent


class Driver:
    def __init__(self, root, wheel=None):
        self.root = Path(root)
        self.wheel = wheel
        self.is_stub = os.environ.get('ACCEPTANCE_TARGET', 'real') == 'stub'
        if os.environ.get('ACCEPTANCE_TARGET', 'real') not in {'real', 'stub'}:
            raise ValueError('ACCEPTANCE_TARGET must be real or stub')
        self.cli = os.environ.get('YGG_BIN', 'ygg')
        self.children = []
        self.core = None
        self.env = {}
        self.elapsed = 0
        self.line_times = []
        if self.is_stub:
            return
        self.cli = shutil.which(self.cli) or self.cli
        for relative in ('h', 't', 'cwd', 'rec', 'bin'):
            self.mkdir(relative)
        self.env = {'HOME': str(self.path('h')), 'TMPDIR': str(self.path('t')),
                    'LANG': 'en_US.UTF-8',
                    'PATH': os.pathsep.join(dict.fromkeys([
                        str(Path(self.cli).absolute().parent),
                        str(Path(shutil.which('git') or '/usr/bin/git').parent),
                        str(Path(shutil.which('python3') or sys.executable).parent),
                        '/usr/bin', '/bin', '/usr/sbin', '/sbin']))}
        if os.environ.get('YGG_RELEASE_FILE'):
            self.env['YGG_RELEASE_FILE'] = os.environ['YGG_RELEASE_FILE']

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.is_stub:
            return
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        if self.core:
            self.core.__exit__(*args)
        # Recover children after a failed assertion in a held demo.
        for folder in self.folders():
            self.run('demo', '--cleanup', folder, timeout=60,
                     env={'YGG_TEST_DEMO_HOLD': None, 'YGG_TEST_DEMO_HOLD_POINTS': None,
                          'YGG_NODE': None, 'YGG_ACTIVATION': None})

    def path(self, relative):
        return self.root / relative

    def mkdir(self, relative):
        path = self.path(relative)
        if not self.is_stub:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def write(self, relative, text):
        if self.is_stub:
            return stub.nothing()
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def read(self, path):
        if self.is_stub:
            return stub.text()
        try:
            return Path(path).read_text(errors='replace')
        except FileNotFoundError:
            return ''

    def exists(self, path):
        if self.is_stub:
            return stub.nothing()
        return Path(path).exists()

    def entries(self, path):
        if self.is_stub:
            return stub.entries()
        return sorted(Path(path).iterdir()) if Path(path).exists() else []

    def json(self, path):
        text = self.read(path)
        return json.loads(text) if text else None

    def yaml(self, path):
        return yaml.safe_load(self.read(path))

    def lines(self, path):
        return [json.loads(line) for line in self.read(path).splitlines() if line.strip()]

    def environment(self, changes=None):
        env = dict(self.env)
        for key, value in (changes or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def external(self, argv, *, env=None, cwd=None, timeout=30, stdin=None):
        if self.is_stub:
            return stub.run()
        try:
            return subprocess.run(list(map(str, argv)), env=self.environment(env),
                                  cwd=cwd or self.path('cwd'), input=stdin, text=True,
                                  capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError as error:
            return subprocess.CompletedProcess(argv, 127, '', str(error))

    def run(self, *args, **kwargs):
        return self.external([self.cli, *args], **kwargs)

    def demo(self, *, keep=True, fast=True, env=None):
        args = ['demo', '--no-open']
        if fast:
            args.append('--fast')
        if keep:
            args.append('--keep')
        return self.stream(args, env=env, timeout=120 if fast else 310)

    def stream(self, args, *, env=None, timeout=120, holds=(), http=False, action=None):
        if self.is_stub:
            return stub.run(), None, []
        start = time.monotonic()
        process = subprocess.Popen([self.cli, *args], env=self.environment(env),
                                   cwd=self.path('cwd'), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.children.append(process)
        output, errors, timestamps = [], [], []
        def read_output():
            for line in process.stdout:
                output.append(line)
                timestamps.append((line.rstrip('\n'), time.monotonic() - start))
        def read_errors():
            errors.append(process.stderr.read())
        threads = [threading.Thread(target=read_output), threading.Thread(target=read_errors)]
        for thread in threads:
            thread.start()
        captures = []
        try:
            for point in holds:
                held = self.path('hold') / ('held-' + point)
                self.wait_for(lambda: held.exists(), timeout, process=process)
                text = ''.join(output)
                folder = self.live_folder()
                if action:
                    action(process, folder)
                elif http:
                    match = re.search(r'demo: dashboard (http://127\.0\.0\.1:\d+/)', text)
                    if not match:
                        raise AssertionError('Held demo did not print its dashboard address')
                    captures.append(self.http(match[1]))
                    self.write('hold/go-' + point, '')
                else:
                    captures.append(self.read(folder / 'state/dashboard/alpha.html'))
                    self.write('hold/go-' + point, '')
            process.wait(timeout=max(.1, timeout - (time.monotonic() - start)))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=5)
            process.stdout.close()
            process.stderr.close()
        self.elapsed = time.monotonic() - start
        self.line_times = timestamps
        result = subprocess.CompletedProcess(args, process.returncode, ''.join(output), ''.join(errors))
        match = re.search(r'^demo: kept (.+)$', result.stdout, re.M)
        folder = Path(match[1]) if match else None
        return result, folder, captures

    def wait_for(self, condition, timeout=120, process=None):
        if self.is_stub:
            return stub.nothing()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            if process and process.poll() is not None:
                raise AssertionError('Demo exited before the signed hold point')
            time.sleep(.02)
        raise AssertionError('Timed out waiting for the signed hold point')

    def folders(self):
        if self.is_stub:
            return stub.entries()
        return sorted(self.path('t').glob('ygg-demo-*'))

    def live_folder(self):
        folders = self.folders()
        if len(folders) != 1:
            raise AssertionError('Expected one demo folder at the held point')
        return folders[0]

    def status(self, folder):
        if self.is_stub:
            return stub.nothing()
        result = self.run('status', '--json', env={
            'YGG_CONFIG': folder / 'config.yaml', 'YGG_STATE_ROOT': folder / 'state',
            'YGG_FILES_ROOT': folder / 'tracker', 'YGG_TRACKER': 'files'})
        return json.loads(result.stdout)

    def activations(self, folder):
        if self.is_stub:
            return stub.entries()
        return [self.json(path) for path in sorted((folder / 'state/activations').glob('act-*.json'))]

    def records(self, folder, package):
        text = self.read(folder / f'tracker/projects/alpha-p1/issues/{package}.md')
        records = text.split('## Records', 1)[-1]
        return [yaml.safe_load(block) for block in re.findall(r'(?ms)^```yaml\s*\n(.*?)^```\s*$', records)]

    def git(self, folder, *args, real=False):
        command = shutil.which('git') if not self.is_stub else 'git'
        return self.external([command, '-C', folder / 'home', *args], env={
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': folder / 'userhome/.gitconfig'})

    def pid_pair(self, path):
        text = self.read(path).strip().split()
        return (int(text[0]), text[1]) if len(text) == 2 else None

    def pairs(self, folder):
        if self.is_stub:
            return stub.entries()
        result = []
        for name in ('conductor.pid', 'dashboard.pid'):
            if self.exists(folder / name):
                result.append(self.pid_pair(folder / name))
        for record in self.activations(folder):
            for pid, identity in [('launcher_pid', 'launcher_started_at'),
                                  ('supervisor_pid', 'supervisor_started_at'),
                                  ('pid', 'process_start_identity')]:
                if record.get(pid):
                    value = record.get(identity)
                    if pid == 'pid':
                        value = value or record.get('process_started_at')
                    result.append((record[pid], value))
        for path in (folder / 'state/merge-checks').glob('*.json'):
            record = self.json(path)
            if record.get('state') == 'running':
                result.append((record.get('pid'), record.get('process_started_at')))
        return result

    def python(self):
        if self.is_stub:
            return None
        first = Path(self.cli).read_text().splitlines()[0]
        if not first.startswith('#!'):
            raise AssertionError('YGG_BIN must identify the installed Python entry point')
        return shlex.split(first[2:])[0]

    def identity(self, pid):
        if self.is_stub:
            return stub.nothing()
        # Integration observation mandated by the signed process-identity convention;
        # no CLI exposes the kernel start identity. No behavioural internal assertions.
        result = self.external([self.python(), '-I', '-c',
            'import sys; from yggdrasil.util import process_start; print(process_start(int(sys.argv[1])) or "")',
            str(pid)])
        if result.returncode:
            raise AssertionError('Could not observe the installed process start identity: ' + result.stderr)
        return result.stdout.strip() or None

    def live_pairs(self, pairs):
        if self.is_stub:
            return stub.entries()
        return [pair for pair in pairs if pair and pair[1] and self.identity(pair[0]) == pair[1]]

    def settle(self):
        if not self.is_stub:
            time.sleep(2)

    def sleep(self, seconds=600, env=None):
        if self.is_stub:
            return stub.nothing()
        process = subprocess.Popen(['sleep', str(seconds)], cwd=self.path('cwd'), env=self.environment(env))
        self.children.append(process)
        return process

    def alive(self, process):
        if self.is_stub:
            return stub.nothing()
        return process.poll() is None

    def interrupt(self, sig, *, keep=True, twice=False, replace=False):
        sleeper = self.sleep() if replace else None
        self.signal_elapsed = 0
        def action(process, folder):
            if replace:
                records = sorted((folder / 'state/activations').glob('act-*.json'))
                path = next(path for path in records if self.json(path).get('node') == 'pm1')
                record = self.json(path)
                record.update(pid=sleeper.pid, process_start_identity='s:1')
                self.write(path, json.dumps(record))
            start = time.monotonic()
            process.send_signal(sig)
            if twice:
                time.sleep(1)
                if process.poll() is None:
                    process.send_signal(sig)
            process.wait(timeout=60)
            self.signal_elapsed = time.monotonic() - start
        env = self.hold_env('agent-pm1tl1-WP-1')
        args = ['demo', '--fast', '--no-open'] + (['--keep'] if keep else [])
        result, folder, _ = self.stream(args, env=env, holds=['agent-pm1tl1-WP-1'], action=action)
        return result, folder, sleeper

    def hold_env(self, points):
        self.mkdir('hold')
        return {'YGG_TEST_MODE': '1', 'YGG_TEST_DEMO_HOLD': str(self.path('hold')),
                'YGG_TEST_DEMO_HOLD_POINTS': points}

    def release_on(self):
        if self.is_stub:
            return stub.nothing()
        template = HERE.parents[1] / 'src/yggdrasil/data/release.yaml'
        switches = yaml.safe_load(self.read(template))
        return self.write('release-on.yaml', yaml.safe_dump(
            {name: 'on' for name in switches}, sort_keys=False))

    def held(self, *, http=False):
        points = ['before-plan-approval', 'before-merge-main']
        env = self.hold_env(','.join(points))
        if http:
            env['YGG_RELEASE_FILE'] = self.release_on()
        else:
            env['YGG_TEST_RELEASE_OFF'] = 'dashboard_serve'
        return self.stream(['demo', '--fast', '--no-open', '--keep'], env=env,
                           holds=points, http=http)

    def http(self, url):
        if self.is_stub:
            return stub.nothing()
        from urllib.parse import urlparse
        request = urllib.request.Request(url, headers={'Host': urlparse(url).netloc})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()

    def package_columns(self, text):
        class Columns(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.packages = {}
            def handle_starttag(self, tag, attributes):
                attrs = dict(attributes)
                if tag == 'section':
                    self.stack.append(attrs.get('data-column'))
                if tag == 'div' and attrs.get('data-package') and 'item' in attrs.get('class', '').split():
                    self.packages[attrs['data-package']] = self.stack[-1] if self.stack else None
            def handle_endtag(self, tag):
                if tag == 'section' and self.stack:
                    self.stack.pop()
        parser = Columns()
        parser.feed(text)
        return parser.packages

    def snapshot(self, relative):
        if self.is_stub:
            return stub.nothing()
        base = self.path(relative)
        result = {}
        for path in [base, *sorted(base.rglob('*'))]:
            info = path.lstat()
            result[str(path.relative_to(base))] = (info.st_mode & 0o7777,
                info.st_size if path.is_file() else None,
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        return result

    def recorder(self, relative):
        path = self.path(relative)
        self.write(relative, '#!/bin/sh\nprintf called >> ' + shlex.quote(str(self.path('rec/calls'))) + '\nexit 1\n')
        if not self.is_stub:
            path.chmod(0o755)
        return path

    def contaminated(self):
        self.recorder('rec/alert')
        self.recorder('rec/t3')
        self.write('h/.config/yggdrasil/config.yaml', 'alert_command: [' + json.dumps(str(self.path('rec/alert'))) + ']\n')
        self.write('cwd/yggdrasil.yaml', 'models: [')
        self.write('cwd/yaml.py', 'from pathlib import Path\nPath(' + repr(str(self.path('rec/yaml-imported'))) + ').touch()\n')
        self.write('user-state/nodes/pm9.json', '{}')
        self.write('user-tracker/_ygg/reviews.json', '{')
        self.write('profile.yaml', 't3_bin: ' + str(self.path('rec/t3')))
        self.write('claude-cfg/one', 'one')
        self.write('codex-home/one', 'one')
        if not self.is_stub:
            self.path('user-state').chmod(0o755)
        self.env.update(YGG_STATE_ROOT=str(self.path('user-state')),
            YGG_FILES_ROOT=str(self.path('user-tracker')), YGG_TRACKER='linear',
            YGG_PROFILE=str(self.path('profile.yaml')), CLAUDE_CONFIG_DIR=str(self.path('claude-cfg')),
            CODEX_HOME=str(self.path('codex-home')), ANTHROPIC_API_KEY='sk-' 'ant-demo-zzz123',
            OPENAI_API_KEY='sk-demo-zzz456')
        return self.sleep(env={'YGG_ACTIVATION': 'act-0009'})

    def all_text(self, folder):
        if self.is_stub:
            return stub.text()
        return '\n'.join(path.read_bytes().decode(errors='replace') for path in folder.rglob('*') if path.is_file())

    def standard(self):
        if self.is_stub:
            return stub.nothing()
        core_folder = HERE.parent / 'acceptance'
        def load(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        original = sys.modules.get('stub')
        sys.modules['stub'] = load('v11_core_stub', core_folder / 'stub.py')
        try:
            module = load('v11_core_driver', core_folder / 'driver.py')
        finally:
            if original is None:
                sys.modules.pop('stub', None)
            else:
                sys.modules['stub'] = original
        self.core = module.Driver(self.root / 'standard')
        self.core.write_yaml(self.core.root / 'home/.config/yggdrasil/config.yaml', {
            'load_threshold': 100, 'alert_forwarder': 'jsonl', 'alert_macos': 'off'})
        self.core.core_tree()
        self.core.run('agents', 'allow-minimal', '--yes')
        self.env = self.core.env
        return self.core

    def standard_run(self, *args, **kwargs):
        if self.is_stub:
            return stub.run()
        return self.core.run(*args, **kwargs)

    @contextmanager
    def a11_tree(self, name):
        if self.is_stub:
            yield stub.nothing()
            return
        aliases = ('stub', 'driver', 'stub_a9', 'driver_a9', 'stub_a10',
                   'driver_a10', 'stub_a11', 'driver_a11')
        originals = {alias: sys.modules.get(alias) for alias in aliases}
        try:
            for alias in aliases:
                folder = 'acceptance' if alias in {'stub', 'driver'} else 'acceptance-' + alias.rsplit('_', 1)[1]
                spec = importlib.util.spec_from_file_location(
                    alias, HERE.parent / folder / (alias + '.py'))
                module = importlib.util.module_from_spec(spec)
                sys.modules[alias] = module
                spec.loader.exec_module(module)
            driver_class = sys.modules['driver_a11'].DriverA11
        finally:
            for alias, original in originals.items():
                if original is None:
                    sys.modules.pop(alias, None)
                else:
                    sys.modules[alias] = original
        with driver_class(self.root / name) as driver:
            driver.core_tree()
            yield driver

    def wait_fake_exit(self, driver, node, launch):
        activation = launch['activation']
        self.wait_for(lambda: any(row.get('event') == 'exit'
            and row.get('activation') == activation and row.get('exit_code') == 0
            for row in driver.calls()), timeout=10)
        driver.wait_exit(node)
        self.wait_for(lambda: any(row.get('id', row.get('activation')) == activation
            and row.get('exited_at') for row in driver.a5_receipts(node)), timeout=10)

    def codex_verdict(self, text, *, attempts=1):
        if self.is_stub:
            return stub.entries(), stub.entries(), stub.entries(), stub.entries()
        with self.a11_tree('verdict') as driver:
            driver.ready(models={'review_model': 'gpt-6.1-sol'})
            driver.authorised('WP-1')
            driver.agent('pm1rv1', binary='codex', mode='script', auto_ack=False,
                         exit_code=0, steps=[{'id': 'verdict', 'write_file': {
                             'path': '{env:YGG_REVIEW_DIR}/verdict.json', 'text': text}}])
            driver.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
            statuses = []
            for number in range(attempts):
                if len(driver.launches('pm1rv1')) <= number:
                    driver.tick()
                self.wait_for(lambda: len(driver.launches('pm1rv1')) > number, timeout=10)
                launch = driver.launches('pm1rv1')[number]
                if launch['binary'] != 'codex':
                    raise AssertionError('AC-15 Given requires the Codex reviewer fake')
                self.wait_fake_exit(driver, 'pm1rv1', launch)
                driver.tick()
                statuses.append(driver.run('status').stdout)
            reviews = {row.get('id') or row.get('activation'): row.get('review')
                       for row in driver.a5_receipts('pm1rv1')}
            launches = [dict(row, review=reviews.get(row.get('activation')))
                        for row in driver.launches('pm1rv1')]
            return (statuses, launches,
                    driver.state_lines('alerts.jsonl'), driver.state_lines('messages.jsonl'))

    def testing_details(self, *, change):
        if self.is_stub:
            return stub.entries()
        with self.a11_tree('testing-changed' if change else 'testing-unchanged') as driver:
            driver.open_review(model='gpt-6.1-sol')
            control = driver.root / 'fake-control/pm1su1'
            ready = control / 'testing-waiting'
            go = control / 'go-1'
            program = ('import pathlib,time\n'
                       'pathlib.Path(' + repr(str(ready)) + ').write_text("")\n'
                       'while not pathlib.Path(' + repr(str(go)) + ').exists():\n'
                       ' time.sleep(0.05)\n')
            driver.agent('pm1su1', mode='script', auto_ack=False, exit_code=0,
                         steps=[{'id': 'w1', 'run': [sys.executable, '-c', program]}])
            driver.verdict()
            self.wait_for(lambda: ready.exists(), timeout=10)
            launch = driver.launches('pm1su1')[-1]
            receipts = driver.a5_receipts('pm1su1')
            if not any(row.get('testing_package') == 'WP-1' and not row.get('exited_at')
                       for row in receipts):
                raise AssertionError('AC-20 Given requires a running WP-1 testing activation')
            if change:
                criteria = driver.cwd / 'c2.md'
                driver.write(criteria, 'changed criteria')
                result = driver.run('tracker', 'write', '--as', 'pm1su1', '--project',
                    'alpha-p1', '--package', 'WP-1', '--kind', 'criteria', '--body-file', criteria,
                    env={'YGG_NODE': None, 'YGG_ACTIVATION': None})
                if result.returncode:
                    raise AssertionError('Could not write AC-20 criteria: ' + result.stderr)
                driver.tick()
                messages = driver.state_lines('messages.jsonl')
                driver.wait_control('go-1', 'pm1su1')
                self.wait_fake_exit(driver, 'pm1su1', launch)
            else:
                driver.wait_control('go-1', 'pm1su1')
                self.wait_fake_exit(driver, 'pm1su1', launch)
                driver.tick(2)
                messages = driver.state_lines('messages.jsonl')
            return messages

    def standard_status(self):
        if self.is_stub:
            return stub.nothing()
        return self.core.status()

    def standard_alerts(self):
        if self.is_stub:
            return stub.entries()
        return self.core.state_lines('alerts.jsonl')

    def standard_messages(self):
        if self.is_stub:
            return stub.entries()
        return self.core.state_lines('messages.jsonl')

    def standard_config(self, **values):
        if self.is_stub:
            return stub.nothing()
        path = self.core.root / 'home/.config/yggdrasil/config.yaml'
        config = yaml.safe_load(self.core.read(path)) or {}
        config.update(values)
        self.core.write_yaml(path, config)

    def standard_dashboard(self):
        if self.is_stub:
            return stub.text()
        return self.core.state_text('dashboard/alpha.html')

    def plan_state(self, state):
        self.standard()
        if self.is_stub:
            return stub.nothing()
        if state in {'executing', 'closed', 'exception', 'replan'}:
            self.core.ready()
        if state == 'closed':
            self.core.run('project', 'close', 'alpha-p1', '--premature', '--note', 'done')
        if state == 'pid-rejected':
            self.core.run('tracker', 'write', '--as', 'pm1', '--project', 'alpha-p1', '--kind', 'pid-ready', '--body', 'ready')
            self.core.run('pid', 'reject', 'alpha-p1', '--note', 'rejected')
        if state in {'exception', 'replan'}:
            self.core.run('exception', 'raise', '--from', 'pm1', '--reason', 'problem', '--plan', 'replan')
            pending = self.core.pending()
            exceptions = [row for row in pending['decisions'] if row['kind'] == 'exception' and row['project'] == 'alpha-p1']
            if len(exceptions) != 1:
                raise AssertionError('Expected one raised exception in pending JSON')
            self.core.run('exception', 'decide', exceptions[0]['id'], '--plan', 'reject')
            if state == 'replan':
                decisions = [row for row in self.core.pending()['decisions'] if row['kind'] == 'decision' and row['project'] == 'alpha-p1']
                if len(decisions) != 1:
                    raise AssertionError('Expected one follow-up decision in pending JSON')
                self.core.run('decide', decisions[0]['id'], '--option', 'replan')

    def route_map(self, change):
        self.standard()
        if self.is_stub:
            return stub.nothing()
        self.core.package('WP-2')
        self.core.at('2026-11-22T09:00:00Z')
        self.core.route_map(blockers=['waiting on WP-2'])
        if change:
            self.core.at('2026-11-22T10:00:00Z')
            if change == 'criteria':
                self.core.run('tracker', 'write', '--as', 'pm1su1', '--project', 'alpha-p1',
                              '--package', 'WP-2', '--kind', 'criteria', '--body', 'c')
            else:
                self.core.decision()
        self.core.at('2026-11-22T10:05:00Z' if change else '2026-11-22T09:05:00Z')

    def pair_prompt(self):
        self.standard()
        if self.is_stub:
            return stub.run(), stub.text()
        self.core.ready()
        self.core.run('tracker', 'write', '--as', 'pm1', '--project', 'alpha-p1', '--kind', 'brief', '--body', 'brief')
        result = self.core.run('launch', 'pm1tl1')
        self.core.wait_file(self.core.log)
        launches = self.core.launches('pm1tl1')
        text = ''
        if launches:
            call = launches[-1]
            text = call.get('stdin', '')
            for argument in call['argv']:
                if len(argument) <= 4096:
                    path = Path(argument)
                    if not path.is_absolute():
                        path = Path(call['cwd']) / path
                    if path.is_file() and path.stat().st_mode & 0o777 == 0o600:
                        text += path.read_text()
        return result, text

    def wheel_names(self):
        if self.is_stub:
            return stub.entries()
        if not self.wheel:
            raise AssertionError('Supply the built wheel with --acceptance-wheel; no implementation tree is inspected')
        with zipfile.ZipFile(self.wheel) as archive:
            return archive.namelist()

    def install_wheel(self):
        if self.is_stub:
            return stub.run()
        if not self.wheel:
            raise AssertionError('Supply the built wheel with --acceptance-wheel')
        result = self.external(['uv', 'tool', 'install', self.wheel], env={
            'UV_TOOL_DIR': self.path('tools'), 'UV_TOOL_BIN_DIR': self.path('toolbin')}, timeout=120)
        if result.returncode:
            raise AssertionError('Wheel installation failed: ' + result.stderr)
        self.cli = str(self.path('toolbin/ygg'))
        self.env['PATH'] = str(self.path('toolbin')) + os.pathsep + self.env['PATH']
        self.mkdir('empty')
        return result

    def linux_tools(self):
        # Host qualification is not an application boundary and runs even in stub mode.
        if sys.platform != 'linux':
            return 'AC-29 requires Linux with bwrap and strace; this host is not Linux'
        missing = [name for name in ('bwrap', 'strace') if not shutil.which(name)]
        return 'AC-29 missing Linux tool(s): ' + ', '.join(missing) if missing else None

    def offline_demo(self):
        if self.is_stub:
            return stub.run(), stub.run(), stub.text()
        self.install_wheel()
        result = self.external(['bwrap', '--dev-bind', '/', '/', '--unshare-net', '--',
            'strace', '-f', '-o', self.path('net.trace'), '-e', 'trace=connect,sendto,sendmsg',
            self.cli, 'demo', '--fast', '--no-open'], timeout=120)
        control = self.external(['bwrap', '--dev-bind', '/', '/', '--unshare-net', '--',
            'python3', '-c', "import socket; socket.create_connection(('192.0.2.1', 80), timeout=2)"])
        return result, control, self.read(self.path('net.trace'))

    def recording_path(self):
        for name in ('claude', 'codex', 'gh', 't3', 'osascript', 'security', 'open'):
            self.recorder('recorders/' + name)
        self.env['PATH'] = str(self.path('recorders')) + os.pathsep + self.env.get('PATH', '')

    def linear_demo(self):
        if self.is_stub:
            return stub.entries(), stub.run(), stub.entries()
        self.standard()
        with self.core.a5_linear():
            config = {'tracker': 'linear', 'linear_token_store': 'file',
                      'linear_token_file': str(self.core.linear_dir / 'token-store.json')}
            self.core.write_yaml(self.core.root / 'home/.config/yggdrasil/config.yaml', config)
            self.core.env['LINEAR_API_KEY'] = 'lin_' 'api_demo123456'
            self.core.run('rehydrate')
            before = self.core.a5_linear_requests()
            self.env = self.core.env
            result = self.run('demo', '--fast', '--no-open', timeout=120)
            after = self.core.a5_linear_requests()
        return before, result, after

    def git_isolation(self):
        if self.is_stub:
            return stub.nothing()
        real_git = shutil.which('git')
        for name in ('fsmon-sys', 'gpg-sys', 'hooks/pre-commit', 'fsmon-global', 'gpg-global'):
            self.recorder('rec/' + name)
        self.write('sys.gitconfig', '[core]\n fsmonitor = ' + str(self.path('rec/fsmon-sys')) +
            '\n[commit]\n gpgsign = true\n[gpg]\n program = ' + str(self.path('rec/gpg-sys')) +
            '\n[user]\n name = Planted System\n')
        self.write('h/.gitconfig', '[core]\n hooksPath = ' + str(self.path('rec/hooks')) +
            '\n fsmonitor = ' + str(self.path('rec/fsmon-global')) +
            '\n[commit]\n gpgsign = true\n[gpg]\n program = ' + str(self.path('rec/gpg-global')) +
            '\n[user]\n name = Planted Global\n')
        source = '#!' + sys.executable + '\nimport json, os, sys\n'
        source += 'os.environ["GIT_CONFIG_SYSTEM"] = ' + repr(str(self.path('sys.gitconfig'))) + '\n'
        source += 'with open(' + repr(str(self.path('rec/git.jsonl'))) + ', "a") as out:\n'
        source += ' out.write(json.dumps({"nosystem": os.environ.get("GIT_CONFIG_NOSYSTEM"), "node": os.environ.get("YGG_NODE"), "argv": sys.argv[1:]}) + "\\n")\n'
        source += 'os.execv(' + repr(real_git) + ', [' + repr(real_git) + ', *sys.argv[1:]])\n'
        self.write('gitw/git', source)
        self.path('gitw/git').chmod(0o755)
        self.env['PATH'] = str(self.path('gitw')) + os.pathsep + self.env['PATH']
        self.env['GIT_CONFIG_GLOBAL'] = str(self.path('h/.gitconfig'))

    def recover_killed(self):
        if self.is_stub:
            return stub.run(), stub.run(), None, stub.entries()
        saved = []
        folders = []
        def action(process, folder):
            saved.extend(self.pairs(folder))
            folders.append(folder)
            process.kill()
        self.stream(['demo', '--fast', '--no-open'], env=self.hold_env('agent-pm1tl1-WP-1'),
                    holds=['agent-pm1tl1-WP-1'], action=action)
        folder = folders[0]
        second, _, _ = self.demo(keep=False)
        cleanup = self.run('demo', '--cleanup', folder, timeout=60)
        return second, cleanup, folder, saved

    def unsafe_cleanup(self, kind):
        if self.is_stub:
            return stub.run(), None, None, None, None
        result, original, _ = self.demo()
        if result.returncode or not original:
            raise AssertionError('Cannot copy a kept demo for the signed cleanup example')
        sleeper = self.sleep(300)
        record = next(iter(sorted((original / 'state/activations').glob('act-*.json'))))
        data = self.json(record)
        data.update(pid=sleeper.pid, process_start_identity=self.identity(sleeper.pid))
        self.write(record, json.dumps(data))
        if kind == 'symlink':
            target = self.path('elsewhere')
            shutil.copytree(original, target)
            folder = self.path('t/ygg-demo-x1')
            folder.symlink_to(target, target_is_directory=True)
            watch = target
        else:
            folder = self.path('t/ygg-demo-x2' if kind == 'mode' else 't/ygg-demo-x3')
            shutil.copytree(original, folder)
            folder.chmod(0o755 if kind == 'mode' else 0o700)
            watch = folder
            if kind == 'state-symlink':
                shutil.rmtree(folder / 'state')
                user_state = self.mkdir('user-state')
                self.write('user-state/activations/act-0001.json', json.dumps(data))
                (folder / 'state').symlink_to(user_state, target_is_directory=True)
                watch = user_state
        before = self.snapshot(watch)
        cleanup = self.run('demo', '--cleanup', folder, timeout=60)
        return cleanup, folder, sleeper, before, self.snapshot(watch)

    def shadow_claude(self):
        self.recorder('shadow/claude')
        return str(self.path('shadow'))

    def no_git(self):
        self.env['PATH'] = str(self.mkdir('no-git'))

    def managed_cwd(self):
        self.write('wt/.ygg-managed', '')
        return self.mkdir('wt/sub')

    def profile(self, content):
        self.write('profile.yaml', content)
        self.env['YGG_PROFILE'] = str(self.path('profile.yaml'))

    def installed_demo(self):
        self.install_wheel()
        return self.run('demo', '--fast', '--no-open', cwd=self.path('empty'), timeout=120)

    def installed_versions(self):
        self.install_wheel()
        metadata = self.external([self.path('tools/yggdrasil/bin/python'), '-c',
            "import yggdrasil, importlib.metadata as m; print(yggdrasil.__version__, m.version('yggdrasil'))"])
        return metadata, self.run('--version')

    def inside(self, path, parent):
        if self.is_stub:
            return stub.nothing()
        resolved = Path(path).resolve()
        return resolved != Path(parent).resolve() and resolved.is_relative_to(Path(parent).resolve())
