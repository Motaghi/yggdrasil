"""CLI and filesystem boundaries, using the supplied core driver and A11 conventions.

A11's import chain needs driver_a9, which is not in the isolated input. This
adapter reuses the supplied core Driver and reproduces only the thin A11 fixture
helpers needed here. It does not import or inspect Yggdrasil implementation code.
"""
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import time

import pytest
import yaml
import stub

_core_spec = importlib.util.spec_from_file_location(
    '_codex_acceptance_core_driver', Path(__file__).parent.parent / 'acceptance' / 'driver.py')
_core = importlib.util.module_from_spec(_core_spec)
_core_spec.loader.exec_module(_core)
LANE = 'refs/heads/ygg/alpha-p1/lane/pm1tl1'
INTEGRATION = 'refs/heads/ygg/alpha-p1/_integration'


class CodexDriver(_core.Driver):
    def __init__(self, root):
        super().__init__(root)
        if not self.is_stub:
            for name in ('git', 'sh'):
                if not shutil.which(name, path=self.env['PATH']):
                    pytest.skip('Required boundary tool missing: ' + name)
            if not Path(self.cli).is_file():
                pytest.skip('Required ygg CLI missing; set YGG_BIN or put ygg on PATH')
        self.options = {}
        self.fake_options = {}
        self.config_path = self.root / 'home/.config/yggdrasil/config.yaml'
        self.write(self.root / 'denylist.txt', 'zebra-orchid-7\n')
        self.example_profile(commit_identity={'name': 'Example Builder',
            'email': 'builder@example.invalid', 'trailers': 'none'},
            leak_denylist=str(self.root / 'denylist.txt'))
        self.config(load_threshold=1.6, alert_forwarder='jsonl')
        self.at('2026-11-19T12:00:00Z')
        self.env['YGG_TEST_CODEX_VERSION'] = 'codex-cli 0.170.0'
        self.env['YGG_TEST_PLATFORM'] = 'darwin'
        self.H, self.S, self.F, self.T = self.repo_a, self.state, self.tracker, self.root
        self.safety = Path(str(self.S) + '.safety')
        self.hooks = self.safety / 'hooks'
        self.lanes = self.H / '.ygg-worktrees/alpha-p1/lane'
        self.B = self.lanes / 'pm1tl1.g000001'
        self.B2 = self.lanes / 'pm1tl1.g000002'
        self.clone = self.B / 'checkout'
        self.QF = self.S / 'lane-clones/alpha-p1/_quarantine'
        self.QF1 = self.QF / 'pm1tl1.g000001.json'
        self.registry = self.S / 'lane-clones/alpha-p1/pm1tl1.json'
        self.serial = 0
        self.step_aliases = {}

    def __exit__(self, *args):
        super().__exit__(*args)
        if not self.is_stub:
            shutil.rmtree(self.root, ignore_errors=True)

    def run(self, *args, **kwargs):
        if self.is_stub:
            return stub.run()
        return super().run(*args, **kwargs)

    @contextmanager
    def v100_cli(self):
        if self.is_stub:
            yield stub.v100_cli()
            return
        executable = os.environ.get('YGG_V100_BIN')
        if not executable:
            pytest.skip('AC-57/AC-58 require YGG_V100_BIN pointing to the v1.0.0 release')
        if not Path(executable).is_file():
            pytest.skip('AC-57/AC-58 v1.0.0 executable missing: ' + executable)
        previous = self.cli
        wrapper = self.root / 'bin/ygg'
        try:
            self.cli = str(Path(executable).resolve())
            self.executable(wrapper, 'exec ' + shlex.quote(self.cli) + ' "$@"\n')
            yield
        finally:
            self.cli = previous
            self.executable(wrapper, 'exec ' + shlex.quote(self.cli) + ' "$@"\n')

    def config(self, **values):
        self.options.update(values)
        self.write_yaml(self.config_path, self.options)

    def standard(self, *, codex=True, approved=True, launcher=None):
        if launcher:
            self.config(launcher_cli=launcher)
        self.core_tree()
        self.run('agents', 'allow-minimal', '--yes')
        if codex:
            self.pid(models={'model': 'gpt-6.1-sol', 'effort': 'high'})
        if approved:
            self.run('pid', 'approve', 'alpha-p1')
        # core_tree has the standard approving project-reviewer fake.
        self.fakes()

    def fakes(self):
        self.fake_behavior(**self.fake_options)

    def agent(self, node='pm1tl1', *, binary='codex', steps=(), **options):
        self.serial += 1
        cooked = []
        aliases = self.step_aliases[node] = {}
        for i, value in enumerate(steps):
            value = dict(value)
            label = value.get('id', str(i))
            value['id'] = f'{node}-{self.serial}-{label}'
            aliases[label] = value['id']
            cooked.append(value)
        self.remove(self.root / 'fake-control' / node / 'exit')
        self.fake_options.setdefault(binary, {}).setdefault('agents', {})[node] = dict(
            mode='script', auto_ack=False, posttool=True, steps=cooked, **options)
        self.fakes()
        return cooked

    def launch(self, node='pm1tl1', *, steps=(), settle=True, binary='codex', env=None, timeout=90):
        self.agent(node, binary=binary, steps=steps)
        previous = len(self.launches(node))
        result = self.run('launch', node, env=env, timeout=timeout)
        if result.returncode == 0:
            self.until(lambda: len(self.launches(node)) > previous, timeout=timeout)
            if settle:
                self.wait_exit(node, timeout=timeout)
            else:
                self.wait_launch(node)
        return result

    def wait_launch(self, node, timeout=10):
        return self.until(lambda: self.launches(node), timeout=timeout)

    def until(self, action, *, timeout=20):
        if self.is_stub:
            return stub.nothing()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = action()
            if value:
                return value
            time.sleep(.02)
        raise AssertionError('Timed out waiting for the required boundary observation')

    def launch_record(self, node='pm1tl1'):
        records = self.launches(node)
        return records[-1] if records else {}

    def activation(self, node='pm1tl1'):
        record = self.launch_record(node)
        return record.get('env', {}).get('YGG_ACTIVATION', '')

    def receipt(self, node='pm1tl1'):
        activation = self.activation(node)
        return next((r for r in self.a5_receipts(node)
                     if (r.get('id') or r.get('activation')) == activation), {})

    def relay(self, node='pm1tl1', *, kept=True):
        activation = self.activation(node)
        return self.S / ('output/' + activation + '/relay' if kept else 'relay/' + activation)

    def response(self, number, node='pm1tl1'):
        return self.json_file(self.relay(node) / 'out' / (f'{number:032x}.json')) or {}

    def responses(self, node='pm1tl1'):
        return [self.json_file(p) for p in self.files(self.relay(node) / 'out')]

    def step(self, label, node='pm1tl1'):
        if self.is_stub:
            return stub.run()
        label = self.step_aliases.get(node, {}).get(label, label)
        records = [r for r in self.calls() if r.get('node') == node and r.get('id') == label]
        event = next((r for r in reversed(records) if r.get('event') in {'step', 'step-refused'}), {})
        output = next((r for r in reversed(records) if r.get('event') == 'step-output'), event)
        return subprocess.CompletedProcess([], event.get('exit_code', 127),
            output.get('stdout', ''), output.get('stderr', ''))

    def wait_step(self, label, node='pm1tl1', timeout=20):
        actual = self.step_aliases.get(node, {}).get(label, label)
        self.until(lambda: any(r.get('node') == node and r.get('id') == actual
            and r.get('event') == 'step' for r in self.calls()), timeout=timeout)
        return self.step(label, node)

    def control(self, name, node='pm1tl1'):
        self.write(self.root / 'fake-control' / node / name, '')

    def send(self, number=11, *, sender='pm1', recipient='pm1tl1', body='x'):
        self.run('test', 'set-next', 'message', str(number))
        return self.run('send', '--from', sender, '--to', recipient, '--kind', 'note', '--body', body)

    def tick(self, count=1):
        for _ in range(count):
            self.run('conductor', 'tick')

    def wp(self, package='WP-1'):
        return next((r for r in (self.status() or {}).get('work_packages', []) if r['id'] == package), {})

    def inbox(self, node='pm1'):
        return self.run('inbox', node).stdout

    def unread(self, node='pm1tl1', message='m-0011'):
        return any(r.get('node') == node and r.get('msg_id') == message
            for r in (self.status() or {}).get('inbox_pending', []))

    def alerts(self, node):
        value = self.command_json('alerts', '--json')
        return [dict(r, text=r.get('message', r.get('text', ''))) for r in (value or [])
            if r.get('node') == node and r.get('kind') == 'ygg-launch-refused']

    def git_result(self, directory, *args, env=None, timeout=30):
        if self.is_stub:
            return stub.run()
        call_env = self.env.copy()
        call_env.pop('GIT_ALTERNATE_OBJECT_DIRECTORIES', None)
        for key, value in (env or {}).items():
            if value is None:
                call_env.pop(key, None)
            else:
                call_env[key] = str(value)
        return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c',
            'core.fsmonitor=false', '-C', str(directory), *map(str, args)],
            env=call_env, capture_output=True, text=True, timeout=timeout, check=False)

    def git_text(self, directory, *args):
        return self.git_result(directory, *args).stdout.strip()

    def head(self, directory=None):
        return self.git_text(directory or self.clone, 'rev-parse', 'HEAD')

    def lane_head(self):
        return self.git_text(self.H, 'rev-parse', LANE)

    def commit_file(self, directory, name='a.txt', text='clean', message='a'):
        self.write(directory / name, text)
        self.git_result(directory, 'add', '--', name)
        result = self.git_result(directory, '-c', 'user.name=Example Builder', '-c',
            'user.email=builder@example.invalid', 'commit', '-m', message)
        if not self.is_stub and result.returncode:
            raise AssertionError('Given commit could not be created: ' + result.stderr)
        return self.head(directory)

    def authorised(self, package='WP-1'):
        self.package(package, criteria=True, authorise=True)
        # Establish the row's initial inbox state before explicitly planted mail.
        for row in (self.status() or {}).get('inbox_pending', []):
            if row.get('node') == 'pm1tl1':
                self.run('inbox', 'pm1tl1', '--ack', row['msg_id'])

    def made_lane(self):
        self.launch()
        return self.lane_head()

    def received(self, *, accepted=False, ready_for_test=False):
        self.authorised()
        self.launch(steps=self.commit_steps('a.txt', 'clean', 'a') + [self.ready_step()])
        head = self.lane_head()
        if accepted or ready_for_test:
            self.complete_package_review('WP-1')
        if accepted:
            self.run('acceptance', 'record', '--from', 'pm1su1', '--package', 'WP-1',
                '--sha', head, '--verdict', 'accept')
        return head

    def commit_steps(self, name='a.txt', text='clean', message='a', *, bypass=False):
        prefix = ['git', '-c', 'core.hooksPath=/dev/null'] if bypass else ['git']
        return [{'write_file': {'path': name, 'text': text}},
            {'run': prefix + ['add', name]}, {'id': 'commit', 'run': prefix + ['commit', '-m', message]},
            {'id': 'made-head', 'run': prefix + ['rev-parse', 'HEAD']}]

    def ready_step(self, package='WP-1'):
        return {'id': 'ready', 'run': ['ygg', 'wp', 'ready', '--from', 'pm1tl1', '--package', package]}

    def sync(self):
        return self.run('lane', 'sync', 'alpha-p1', '--lane', 'pm1tl1')

    def plant(self, number, value, *, poll=20):
        key = f'{number:032x}'
        body = shlex.quote(json.dumps(value))
        script = f'printf %s {body} > "{{env:YGG_RELAY}}/in/.p-{key}" && mv "{{env:YGG_RELAY}}/in/.p-{key}" "{{env:YGG_RELAY}}/in/{key}.json"'
        steps = [{'run': ['sh', '-c', script]}]
        if poll:
            steps.append(self.poll_responses([number], seconds=poll))
        return steps

    def poll_responses(self, numbers, seconds=20):
        checks = ' && '.join(f'test -f "{{env:YGG_RELAY}}/out/{n:032x}.json"' for n in numbers)
        return {'run': ['sh', '-c', f'i=0; until {checks}; do i=$((i+1)); test "$i" -lt {seconds * 10} || exit 1; sleep 0.1; done']}

    def names(self, directory):
        if self.is_stub:
            return stub.entries()
        return sorted(p.name for p in Path(directory).iterdir()) if Path(directory).is_dir() else []

    def files(self, directory):
        if self.is_stub:
            return stub.entries()
        return sorted(p for p in Path(directory).rglob('*') if p.is_file())

    def snapshot_files(self, directory):
        if self.is_stub:
            return stub.snapshot()
        return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.files(directory)}

    def digest(self, path):
        if self.is_stub:
            return stub.text()
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def inode_pairs(self, directory):
        if self.is_stub:
            return stub.entries()
        return [(p.stat().st_dev, p.stat().st_ino) for p in self.files(directory)]

    def is_dir(self, path):
        return stub.nothing() if self.is_stub else Path(path).is_dir()

    def is_link(self, path):
        return stub.nothing() if self.is_stub else Path(path).is_symlink()

    def readlink(self, path):
        return stub.text() if self.is_stub else str(Path(path).readlink())

    def symlink(self, path, target):
        if self.is_stub:
            return stub.nothing()
        path = Path(path)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)

    def rename(self, source, destination):
        if self.is_stub:
            return stub.nothing()
        Path(source).rename(destination)

    def fifo(self, path):
        if self.is_stub:
            return stub.nothing()
        if not hasattr(os, 'mkfifo'):
            pytest.skip('Required FIFO tool unavailable: os.mkfifo')
        Path(path).unlink(missing_ok=True)
        os.mkfifo(path)

    def executable(self, path, text):
        if self.is_stub:
            return stub.nothing()
        self.write(path, '#!/bin/sh\n' + text)
        Path(path).chmod(0o755)

    def recording_programs(self):
        for name in ['fsmon', 'clean', 'pack', 'alt', 'hooks/pre-commit', 'hooks/reference-transaction']:
            self.executable(self.T / 'rec' / name,
                'touch ' + shlex.quote(str(self.T / 'rec' / ('ran-' + name.split('/')[-1]))) + '\nexit 0\n')

    def tampered_config_steps(self):
        steps = self.commit_steps()
        for key, name in [('core.fsmonitor', 'fsmon'), ('filter.x.clean', 'clean')]:
            steps.append({'run': ['git', 'config', key, str(self.T / 'rec' / name)]})
        steps += [{'write_file': {'path': '.gitattributes', 'text': '* filter=x'}}]
        for key, name in [('core.hooksPath', 'hooks'), ('uploadpack.packObjectsHook', 'pack'),
                          ('core.alternateRefsCommand', 'alt')]:
            steps.append({'run': ['git', 'config', key, str(self.T / 'rec' / name)]})
        return steps

    def prompt(self, node='pm1tl1'):
        call = self.launch_record(node)
        text = call.get('stdin', '')
        for arg in call.get('argv', []):
            if len(arg) > 4096:
                continue
            path = Path(arg)
            if not path.is_absolute():
                path = Path(call.get('cwd', str(self.root))) / path
            if self.exists(path) and path.is_file():
                text += self.read(path)
        return text

    def toml(self, path):
        if self.is_stub:
            return stub.snapshot()
        try:
            import tomllib
        except ImportError:
            pytest.skip('Required TOML reader unavailable: Python 3.11 tomllib')
        return tomllib.loads(self.read(path))

    def elapsed(self, action):
        if self.is_stub:
            return action(), stub.seconds()
        before = time.monotonic()
        result = action()
        return result, time.monotonic() - before

    @contextmanager
    def background_ready(self):
        if self.is_stub:
            yield None
            return
        process = subprocess.Popen([self.cli, 'wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1'],
            cwd=self.cwd, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def pause(self, seconds):
        if self.is_stub:
            return stub.nothing()
        time.sleep(seconds)

    def finish_process(self, process, timeout):
        if self.is_stub:
            return stub.run()
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess([], process.returncode, stdout, stderr)

    def review(self, review='rv-0001'):
        if self.is_stub:
            return stub.snapshot()
        records = (self.json_file(self.F / '_ygg/reviews.json') or {}).get('reviews', {})
        return records.get(review, {})

    def reviews(self, package='WP-1'):
        if self.is_stub:
            return stub.entries()
        records = (self.json_file(self.F / '_ygg/reviews.json') or {}).get('reviews', {})
        return [value for value in records.values() if value.get('package') == package]

    def messages(self, node='pm1'):
        return [r for r in self.state_lines('messages.jsonl')
            if r.get('to') == node and r.get('outcome') == 'delivered']

    def write_json(self, path, value):
        self.write(path, json.dumps(value))

    def malformed_requests(self, source):
        program = (
            'import os,pathlib; root=pathlib.Path(os.environ["YGG_RELAY"])/"in"; '
            f'(root/"{1:032x}.json").symlink_to({str(source)!r}); '
            f'p=root/".p-{2:032x}"; p.write_bytes(b" "*(1024*1024+1)); p.rename(root/"{2:032x}.json"); '
            f'p=root/".p-{3:032x}"; p.write_text(\'["inbox"]\'); p.rename(root/"{3:032x}.json")')
        import sys
        return [{'run': [sys.executable, '-c', program]}, self.poll_responses([1, 2, 3])]

    def pause_step(self, seconds):
        import sys
        return [{'run': [sys.executable, '-c', f'import time; time.sleep({seconds})']}]

    def mkdir(self, path):
        if self.is_stub:
            return stub.nothing()
        Path(path).mkdir(parents=True, exist_ok=True)

    def tree_names(self, path):
        if self.is_stub:
            return stub.entries()
        return sorted(str(p.relative_to(path)) for p in Path(path).rglob('*'))

    def clock(self):
        return stub.seconds() if self.is_stub else time.monotonic()

    def read_bytes(self, path):
        if self.is_stub:
            return stub.binary()
        return Path(path).read_bytes()

    def untrusted_toml(self):
        return ('approval_policy = "on-request"\n[mcp_servers.rec]\ncommand = '
            + json.dumps(str(self.T / 'rec/mcp')) + '\n[[hooks.PreToolUse]]\ncommand = '
            + json.dumps(str(self.T / 'rec/hook')) + '\n[profiles.x]\n[projects.'
            + json.dumps(str(self.H)) + ']\ntrust_level = "trusted"\n')

    def provider_toml(self):
        # notify is a top-level key in the signed row, outside the provider table.
        return ('model_provider = "hub"\nnotify = [' + json.dumps(str(self.T / 'rec/notify'))
            + ']\n[model_providers.hub]\nname = "hub"\n'
            'base_url = "http://127.0.0.1:18080/v1"\nenv_key = "OPENAI_API_KEY"\nwire_api = "responses"\n')

    def wait_exit(self, node, timeout=90):
        if self.is_stub:
            return stub.nothing()
        self.until(lambda: self.receipt(node).get('exited_at'), timeout=timeout)

    def recorder(self, name):
        self.executable(self.T / 'rec' / name,
            'touch ' + shlex.quote(str(self.T / 'rec' / ('ran-' + name))) + '\nexit 0\n')

    def quarantine_record_path(self):
        return self.QF1

    def ticks_until(self, observation):
        for _ in range(10):
            self.tick()
            if observation():
                return
        if not self.is_stub:
            raise AssertionError('Required observation absent after 10 ticks')

    def accept_package(self, package, name):
        self.authorised(package)
        self.launch(steps=self.commit_steps(name, 'clean', package) + [self.ready_step(package)])
        head = self.lane_head()
        self.complete_package_review(package)
        self.run('acceptance', 'record', '--from', 'pm1su1', '--package', package,
            '--sha', head, '--verdict', 'accept')
        return head

    def merge_fixture(self, *, last=True, setup=None, close=True, packages=2, check=True):
        stages = [{'number': 1, 'name': 'Foundations', 'planned_end': '2026-11-19T17:00:00Z'}]
        if not last:
            stages.append({'number': 2, 'name': 'Build', 'planned_end': '2026-11-20T17:00:00Z'})
        options = dict(models={'model': 'gpt-6.1-sol', 'effort': 'high'},
            stages=stages, merge_mode='prepare')
        if setup:
            options['merge_check_setup'] = [str(setup)]
        self.pid(**options)
        self.run('pid', 'approve', 'alpha-p1')
        head = ''
        for number in range(1, packages + 1):
            head = self.accept_package(f'WP-{number}', f'module{number}.txt')
            self.run('merge', 'lane', 'alpha-p1', '--lane', 'pm1tl1', '--repo', self.H,
                env={'YGG_NODE': 'pm1'})
        if close:
            report = self.F / '_reports/end-stage-1.md'
            self.write(report, '# End of stage 1\n')
            self.run('stage', 'end', 'alpha-p1', '--within-tolerance', 'yes', '--report', report,
                env={'YGG_NODE': 'pm1'})
            self.run('stage', 'approve', 'alpha-p1')
            if check:
                for _ in range(10):
                    text = self.run('status').stdout
                    if 'merge check: passed' in text:
                        break
                    self.tick()
                if not self.is_stub and 'merge check: passed' not in self.run('status').stdout:
                    raise AssertionError('Given merge check did not pass')
        return head

    def cancel_fixture(self, *, borrowed_side=False):
        head_a = self.received(accepted=True)
        self.authorised('WP-2')
        worktree = self.T / 'received-wp2'
        self.git_result(self.H, 'worktree', 'add', str(worktree), LANE.removeprefix('refs/heads/'))
        try:
            head_r = self.commit_file(worktree, 'r.txt', 'r', 'R')
        finally:
            self.git_result(self.H, 'worktree', 'remove', '--force', str(worktree))
        self.launch()
        if borrowed_side:
            worktree = self.T / 'borrowed-side'
            self.git_result(self.H, 'worktree', 'add', '-b', 'side', str(worktree), head_r)
            try:
                head_x = self.commit_file(worktree, 'x.txt', 'x', 'X')
            finally:
                self.git_result(self.H, 'worktree', 'remove', '--force', str(worktree))
            self.git_result(self.clone, 'merge', '--ff-only', head_x)
        head_e = self.commit_file(self.clone, 'e.txt', 'clean', 'E')
        if borrowed_side:
            return head_a, head_r, head_e, head_x
        return head_a, head_r, head_e

    def finish_cancel(self):
        import re
        preview = self.run('wp', 'cancel', 'alpha-p1', '--finish', 'pm1tl1')
        if self.is_stub:
            code = stub.text()
        else:
            match = re.search(r'--yes\s+[\"\x27]?([A-Za-z0-9_-]+)', preview.stdout + '\n' + preview.stderr)
            if not match:
                raise AssertionError('Cancel preview did not print a --yes confirmation code')
            code = match.group(1)
        return self.run('wp', 'cancel', 'alpha-p1', '--finish', 'pm1tl1', '--yes', code)

    def odd_clone_with_waiting_worker(self):
        self.made_lane()
        self.launch('pm1tl1w1', steps=[{'wait': 'go-2'}], settle=False)
        victim = self.T / 'victim/packed-refs'
        self.write(victim, 'victim')
        self.symlink(self.clone / '.git/packed-refs', victim)
        return self.digest(victim)

    def merge_setup_recorder(self):
        setup = self.T / 'rec/setup'
        self.executable(setup,
            'printf "%s\\n" "$TMPDIR" "$TMP" "$TEMP" "$XDG_CACHE_HOME" > '
            + shlex.quote(str(self.T / 'rec/setup-env')) + '\nls -A "$TMPDIR" > '
            + shlex.quote(str(self.T / 'rec/setup-tmp')) + '\nexit 0\n')
        return setup

    def cache_listing_step(self, name):
        return {'run': ['sh', '-c', 'ls -AR "$TMPDIR" "$XDG_CACHE_HOME" > '
            + shlex.quote(str(self.T / 'rec' / name))]}

    def poll_taken(self, count, seconds=20):
        check = ('set -- "{env:YGG_RELAY}"/taken/*; '
            f'test "$#" -eq {count} && test -e "$1"')
        return {'run': ['sh', '-c',
            f'i=0; until {check}; do i=$((i+1)); test "$i" -lt {seconds * 10} || exit 1; sleep 0.1; done']}

    def duplicate_taken_request_step(self, value):
        body = shlex.quote(json.dumps(value))
        barrier = self.poll_taken(1)['run'][2]
        planter = (barrier + '; test "$i" -lt 200 || exit 1; '
            'set -- "{env:YGG_RELAY}"/taken/*; '
            f'printf %s {body} > "{{env:YGG_RELAY}}/in/.p-dup" && '
            'mv "{env:YGG_RELAY}/in/.p-dup" "{env:YGG_RELAY}/in/${1##*/}"')
        return {'id': 'once', 'run': ['sh', '-c',
            '(' + planter + ') & ygg send --from pm1tl1 --to pm1 --kind note --body once']}

    def relay_hold(self, sequence):
        folder = self.T / 'hold'
        self.mkdir(folder)
        self.write(folder / f'hold-{sequence}', '')
        self.env['YGG_TEST_RELAY_HOLD'] = str(folder)

    def poll_relay_entered(self, sequence):
        path = shlex.quote(str(self.T / 'hold' / f'entered-{sequence}'))
        return {'run': ['sh', '-c',
            f'i=0; until test -f {path}; do i=$((i+1)); test "$i" -lt 200 || exit 1; sleep 0.1; done']}

    def wait_relay_entered(self, sequence):
        self.until(lambda: self.exists(self.T / 'hold' / f'entered-{sequence}'))

    def release_relay(self, sequence):
        self.write(self.T / 'hold' / f'release-{sequence}', '')

    def group_alive(self, node='pm1tl1'):
        if self.is_stub:
            return stub.nothing()
        if not shutil.which('ps'):
            pytest.skip('Required process-group observation tool missing: ps')
        pgid = self.receipt(node).get('pid')
        if not pgid:
            return None
        rows = subprocess.run(['ps', '-axo', 'pid=,pgid=,stat='], capture_output=True,
            text=True, check=True).stdout.splitlines()
        return any(int(parts[1]) == pgid and not parts[2].startswith('Z')
            for line in rows if len(parts := line.split()) >= 3)

    def wait_pending_and_group_dead(self, number):
        self.until(lambda: self.receipt().get('pid'))
        self.until(lambda: self.exists(self.relay(kept=False) / 'in' / f'{number:032x}.json')
            and self.group_alive() is False)

    def quarantine_status(self, box):
        return '\n'.join(line for line in self.run('status').stdout.splitlines()
            if line.startswith(str(box)))

    def remove_tree(self, path):
        if self.is_stub:
            return stub.nothing()
        shutil.rmtree(path)

    def three_odd_replacements(self):
        self.made_lane()
        victim = self.T / 'victim/packed-refs'
        self.write(victim, 'victim')
        for generation in range(1, 4):
            box = self.lanes / f'pm1tl1.g{generation:06d}'
            self.symlink(box / 'checkout/.git/packed-refs', victim)
            self.launch()
        box = self.lanes / 'pm1tl1.g000004'
        self.symlink(box / 'checkout/.git/packed-refs', victim)
        return box

    def alternate_dependency(self, *, relative=False):
        self.made_lane()
        other = self.T / 'other'
        self.mkdir(other)
        self.git_result(other, 'init', '--initial-branch=main')
        head_x = self.commit_file(other, 'x.txt', 'x', 'X')
        alternates = self.clone / '.git/objects/info/alternates'
        target = other / '.git/objects'
        path = os.path.relpath(target, self.clone / '.git/objects') if relative else str(target)
        self.write(alternates, path + '\n')
        recorded_hash = self.digest(alternates)
        self.git_result(self.clone, 'branch', 'dep', head_x)
        return head_x, recorded_hash

    def feed_fixture(self):
        # Use WP-2 for L0 so rows that authorise WP-1 have no earlier WP-1 review.
        self.authorised('WP-2')
        self.launch(steps=self.commit_steps() + [self.ready_step('WP-2')])
        head_l0 = self.lane_head()
        self.complete_package_review('WP-2')
        self.run('acceptance', 'record', '--from', 'pm1su1', '--package', 'WP-2',
            '--sha', head_l0, '--verdict', 'accept')
        main = self.commit_file(self.H, 'm.txt', 'clean', 'main')
        return head_l0, main

    def feed_sync(self, fault=None):
        env = {'YGG_FAULT_AT': 'boundary:' + fault} if fault else None
        return self.run('lane', 'sync', 'alpha-p1', '--lane', 'pm1tl1', env=env)

    @contextmanager
    def background_launch(self, *, env=None):
        self.agent()
        if self.is_stub:
            yield stub.nothing()
            return
        call_env = self.env.copy()
        call_env.update({key: str(value) for key, value in (env or {}).items()})
        process = subprocess.Popen([self.cli, 'launch', 'pm1tl1'], cwd=self.cwd,
            env=call_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def git_with_input(self, directory, *args, stdin, env=None):
        if self.is_stub:
            return stub.run()
        call_env = self.env.copy()
        call_env.pop('GIT_ALTERNATE_OBJECT_DIRECTORIES', None)
        call_env.update({key: str(value) for key, value in (env or {}).items()})
        return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c',
            'core.fsmonitor=false', '-C', str(directory), *map(str, args)],
            input=stdin, env=call_env, capture_output=True, text=True,
            timeout=30, check=False)

    def advance_home_lane_during_assembly(self):
        parent = self.lane_head()
        index_env = {'GIT_INDEX_FILE': str(self.T / 'assembly-index')}
        self.git_result(self.H, 'read-tree', parent, env=index_env)
        blob = self.git_with_input(self.H, 'hash-object', '-w', '--stdin', stdin='u').stdout.strip()
        self.git_result(self.H, 'update-index', '--add', '--cacheinfo',
            f'100644,{blob},u.txt', env=index_env)
        tree = self.git_result(self.H, 'write-tree', env=index_env).stdout.strip()
        commit = self.git_with_input(self.H, '-c', 'user.name=Example Builder',
            '-c', 'user.email=builder@example.invalid', 'commit-tree', tree,
            '-p', parent, stdin='U\n').stdout.strip()
        result = self.git_result(self.H, 'update-ref', LANE, commit, parent)
        if not self.is_stub and result.returncode:
            raise AssertionError('Given home-lane commit U could not be installed: ' + result.stderr)
        return commit

    def remove_clone_objects(self):
        if self.is_stub:
            return stub.nothing()
        objects = self.clone / '.git/objects'
        for path in (objects / 'pack').iterdir():
            if path.is_file():
                path.unlink()
        for path in objects.iterdir():
            if path.is_dir() and len(path.name) == 2 and all(c in '0123456789abcdef' for c in path.name):
                shutil.rmtree(path)

    def accepted_other_lane_file(self):
        self.package('WP-3', lane='pm1tl2', criteria=True, authorise=True)
        for row in (self.status() or {}).get('inbox_pending', []):
            if row.get('node') == 'pm1tl2':
                self.run('inbox', 'pm1tl2', '--ack', row['msg_id'])
        steps = self.commit_steps('shared.txt', 'two', 's')
        steps.append({'id': 'ready', 'run': ['ygg', 'wp', 'ready',
            '--from', 'pm1tl2', '--package', 'WP-3']})
        self.launch('pm1tl2', steps=steps)
        head_x = self.git_text(self.H, 'rev-parse', 'refs/heads/ygg/alpha-p1/lane/pm1tl2')
        self.complete_package_review('WP-3')
        self.run('acceptance', 'record', '--from', 'pm1su2', '--package', 'WP-3',
            '--sha', head_x, '--verdict', 'accept')
        return head_x


    def move_pack_outside_box(self):
        if self.is_stub:
            return stub.text()
        pack = self.clone / '.git/objects/pack'
        outside = self.T / 'packs'
        self.rename(pack, outside)
        target = os.path.relpath(outside, self.clone / '.git/objects')
        self.symlink(pack, target)
        return target

    def is_fifo(self, path):
        if self.is_stub:
            return stub.nothing()
        try:
            return stat.S_ISFIFO(Path(path).lstat().st_mode)
        except FileNotFoundError:
            return False

    def relative_path(self, target, start):
        return os.path.relpath(target, start)

    def other_repository(self):
        other = self.T / 'other'
        self.mkdir(other)
        self.git_result(other, 'init', '--initial-branch=main')
        return self.commit_file(other, 'x.txt', 'x', 'X')

    def git_binary(self, directory, *args, stdin=None):
        if self.is_stub:
            return stub.binary()
        call_env = self.env.copy()
        call_env.pop('GIT_ALTERNATE_OBJECT_DIRECTORIES', None)
        result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c',
            'core.fsmonitor=false', '-C', str(directory), *map(str, args)],
            input=stdin, env=call_env, capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise AssertionError('Given object transfer failed: ' + result.stderr.decode(errors='replace'))
        return result.stdout

    def feed_without_parent(self):
        head_l0, parent_n = self.feed_fixture()
        self.feed_sync('ingest-before-index')
        head_m = self.lane_head()
        for line in self.git_text(self.H, 'rev-list', '--objects', head_m,
                                  '--not', head_l0).splitlines():
            oid = line.split()[0]
            if oid == parent_n:
                continue
            kind = self.git_text(self.H, 'cat-file', '-t', oid)
            contents = self.git_binary(self.H, 'cat-file', kind, oid)
            self.git_binary(self.clone, 'hash-object', '-w', '--literally',
                            '-t', kind, '--stdin', stdin=contents)
        return head_l0, head_m, parent_n

    def is_regular(self, path):
        if self.is_stub:
            return stub.nothing()
        return stat.S_ISREG(Path(path).lstat().st_mode)

    def chmod(self, path, mode):
        if self.is_stub:
            return stub.nothing()
        Path(path).chmod(mode)

    def mode(self, path):
        if self.is_stub:
            return stub.nothing()
        return stat.S_IMODE(Path(path).lstat().st_mode)

    def require_unlistable(self, path):
        if self.is_stub:
            return stub.nothing()
        try:
            os.listdir(path)
        except PermissionError:
            return
        pytest.skip('Required permission capability unavailable: mode 0111 folder can still be listed')

    def migrate_reftable(self):
        result = self.git_result(self.clone, 'refs', 'migrate', '--ref-format=reftable')
        if result.returncode:
            text = result.stderr.lower()
            if any(token in text for token in ['not a git command', 'unknown subcommand',
                'unknown option', 'unrecognized option', 'unknown ref storage format']):
                pytest.skip('Required Git reftable migration tool unavailable: ' + result.stderr.strip())
            raise AssertionError('Given reftable migration failed: ' + result.stderr)
        return result

    @property
    def L(self):
        return Path(self.env['HOME']) / '.config/yggdrasil/agent-codex'

    def signin_boundary(self):
        self.env.pop('YGG_TEST_CODEX_VERSION', None)
        self.env.pop('YGG_NODE', None)
        self.env.pop('YGG_ACTIVATION', None)
        if self.is_stub:
            return stub.signin_boundary()
        binary = self.root / 'bin/codex'
        source = self.read(binary)
        extension = '''
    if binary == "codex" and sys.argv[1:2] in (["login"], ["logout"]):
        answer = behavior.get("signin_commands", {}).get(" ".join(sys.argv[1:]), {})
        home = Path(os.environ["CODEX_HOME"])
        if "auth" in answer:
            target = home / "auth.json"
            target.write_text(answer["auth"])
            target.chmod(0o600)
        if answer.get("save_stdin"):
            (home / "stdin.txt").write_text(record["stdin"])
        sys.stdout.write(answer.get("stdout", ""))
        sys.stderr.write(answer.get("stderr", ""))
        log("exit", exit_code=answer.get("exit", 0))
        return answer.get("exit", 0)
    if binary == "codex" and agent.get("signin_observe"):
        observations = {}
        for name in ("auth.json", "auth.lock"):
            path = Path(os.environ["CODEX_HOME"]) / name
            item = {"exists": os.path.lexists(path)}
            if item["exists"]:
                info = path.lstat()
                item["lstat"] = {"st_mode": info.st_mode, "st_uid": info.st_uid,
                    "st_nlink": info.st_nlink, "st_ino": info.st_ino, "st_dev": info.st_dev}
                item["target"] = os.readlink(path) if path.is_symlink() else None
                item["content"] = path.read_text() if path.is_file() else None
            observations[name] = item
        log("signin-snapshot", files=observations)
        if agent.get("signin_refresh") is not None:
            target = (Path(os.environ["CODEX_HOME"]) / "auth.json").resolve(strict=True)
            import tempfile
            descriptor, temporary = tempfile.mkstemp(dir=target.parent)
            with os.fdopen(descriptor, "w") as output:
                output.write(agent["signin_refresh"])
            os.replace(temporary, target)
'''
        anchor = '    log("launch")\n'
        if source.count(anchor) != 1:
            raise AssertionError('Supplied fake lacks its unique launch recording boundary')
        self.write(binary, source.replace(anchor, anchor + extension, 1))

    def plant_login(self, *, directory=None, auth=None, config=False):
        directory = directory or self.L
        self.mkdir(directory)
        self.chmod(directory, 0o700)
        if auth is not None:
            self.write(directory / 'auth.json', auth)
            self.chmod(directory / 'auth.json', 0o600)
        if config:
            self.write(directory / 'config.toml', 'cli_auth_credentials_store = "file"\n')
        return directory

    def signin_answer(self, arguments, **answer):
        if self.is_stub:
            return stub.signin_answer()
        self.fake_options.setdefault('codex', {}).setdefault('signin_commands', {})[
            ' '.join(arguments)] = answer
        self.fakes()

    def signin_calls(self):
        if self.is_stub:
            return stub.signin_calls()
        return [row for row in self.calls('codex') if row.get('event') == 'launch']

    def signin_snapshot(self, node='pm1tl1'):
        if self.is_stub:
            return stub.signin_snapshot()
        return next((row['files'] for row in reversed(self.calls('codex'))
            if row.get('event') == 'signin-snapshot' and row.get('node') == node), {})

    def signin_agent(self, node='pm1tl1', *, steps=(), refresh=None, running=False):
        if running:
            steps = [*steps, {'id': 'stay-running', 'wait': 'exit'}]
        return self.agent(node, steps=steps, signin_observe=True, signin_refresh=refresh)

    def signin_launch(self, node='pm1tl1', *, steps=(), refresh=None, running=False):
        self.signin_agent(node, steps=steps, refresh=refresh, running=running)
        previous = len(self.launches(node))
        result = self.run('launch', node)
        if result.returncode == 0:
            self.until(lambda: len(self.launches(node)) > previous)
            if not running:
                self.wait_exit(node)
        return result

    def signin_home(self, node='pm1tl1'):
        return self.S / 'launch' / self.activation(node) / 'codex-home'

    def signin_review_given(self):
        self.authorised()
        self.signin_launch(steps=self.commit_steps() + [self.ready_step()])
        self.signin_agent('pm1rv1')

    def signin_review_ticks(self):
        self.ticks_until(lambda: bool(self.launches('pm1rv1')))
        self.wait_exit('pm1rv1')

    def signin_alerts(self, *, user=False):
        if self.is_stub:
            return stub.signin_alerts()
        rows = self.command_json('alerts', *(['--for-user'] if user else []), '--json')
        return [dict(row, text=row.get('message', row.get('text', '')))
            for row in (rows or []) if row.get('kind') == 'ygg-codex-sign-in']

    def signin_profile(self, **values):
        if self.is_stub:
            return stub.signin_profile()
        path = Path(self.env['YGG_PROFILE'])
        profile = yaml.safe_load(self.read(path))
        profile.update(values)
        self.write_yaml(path, profile)

    def signin_provider(self):
        source = self.T / 'codex-home'
        self.env['CODEX_HOME'] = str(source)
        self.write(source / 'config.toml', 'model_provider = "hub"\n'
            '[model_providers.hub]\nname = "hub"\n'
            'base_url = "http://127.0.0.1:18080/v1"\n'
            'env_key = "HUB_KEY"\nwire_api = "responses"\n')

    def result_json(self, result):
        if self.is_stub:
            return stub.result_json()
        try:
            return json.loads(result.stdout)
        except ValueError:
            return {}

    def signin_relay_wrapper(self):
        if self.is_stub:
            return stub.signin_relay_wrapper()
        directory = self.T / 'relay-wrapper'
        script = ('import json, os, sys\n'
            'if os.environ.get("YGG_VIA_RELAY"):\n'
            '    with open(' + repr(str(directory / 'environments.jsonl')) + ', "a") as output:\n'
            '        output.write(json.dumps(dict(os.environ)) + "\\n")\n'
            'os.execv(' + repr(self.cli) + ', [' + repr(self.cli) + ', *sys.argv[1:]])\n')
        self.write(directory / 'ygg', '#!' + sys.executable + '\n' + script)
        self.chmod(directory / 'ygg', 0o755)
        self.env['PATH'] = str(directory) + os.pathsep + self.env['PATH']

    def signin_relay_environments(self):
        if self.is_stub:
            return stub.signin_relay_environments()
        return [json.loads(line) for line in self.read(
            self.T / 'relay-wrapper/environments.jsonl').splitlines() if line.strip()]

    def signin_running(self):
        if self.is_stub:
            return stub.nothing()
        if not shutil.which('ps'):
            pytest.skip('Required process observation tool missing: ps')
        rows = subprocess.run(['ps', '-axo', 'stat=,args='], capture_output=True,
            text=True, check=True).stdout.splitlines()
        binary = str(self.root / 'bin/codex')
        return any(not parts[0].startswith('Z') and binary in parts[1].split()
            for line in rows if len(parts := line.split(None, 1)) == 2)

    @contextmanager
    def second_state_root(self):
        with type(self)(self.T / 'install2') as second:
            second.env['HOME'] = self.env['HOME']
            second.env['YGG_CONFIG'] = str(second.config_path)
            second.standard()
            second.signin_boundary()
            try:
                yield second
            finally:
                second.control('exit')
                if second.launches('pm1tl1'):
                    second.wait_exit('pm1tl1')
