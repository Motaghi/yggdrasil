import copy
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import yaml

from driver import Driver
from driver_a10 import DriverA10
import stub_a11


LANE = 'ygg/alpha-p1/lane/pm1tl1'
INTEGRATION = 'ygg/alpha-p1/_integration'
T = '2026-11-19T12:00:00Z'


class DriverA11(DriverA10):
    def __init__(self, root):
        super().__init__(root)
        self.options = {}
        self.fake_options = {}
        self.commit_observations = []
        self._capture_sequence = 0
        self.profile_options = {'commit_identity': {'name': 'Example Builder',
            'email': 'builder@example.invalid', 'trailers': 'none'},
            'leak_denylist': str(self.root / 'denylist.txt')}
        self.config_path = self.root / 'home/.config/yggdrasil/config.yaml'
        self.write(self.root / 'denylist.txt', 'zebra-orchid-7\n')
        self.config(load_threshold=1.6, alert_forwarder='jsonl')
        self.profile()
        self.at(T)

    def run(self, *args, **kwargs):
        if self.is_stub:
            return stub_a11.run()
        return super().run(*args, **kwargs)

    def l1_skill(self):
        if self.is_stub:
            return stub_a11.text()
        return self.read(self.core_path('skills/yggdrasil-l1/SKILL.md'))

    def core_tree(self, *, agreement=True):
        Driver.core_tree(self)
        if agreement:
            self.run('agents', 'allow-minimal', '--yes')
        else:
            self.remove(self.tracker / '_ygg/agents-agreement.json')

    def config(self, **values):
        for key, value in values.items():
            if value is None:
                self.options.pop(key, None)
            else:
                self.options[key] = value
        self.write_yaml(self.config_path, self.options)

    def profile(self, **values):
        self.profile_options.update(values)
        self.example_profile(**self.profile_options)

    def fakes(self, **values):
        for binary, fields in values.items():
            self.fake_options.setdefault(binary, {}).update(fields)
        self.fake_behavior(**self.fake_options)

    def agent(self, node='pm1tl1', **values):
        self.remove(self.root / 'fake-control' / node / 'exit')
        binary = values.pop('binary', 'claude')
        agents = self.fake_options.setdefault(binary, {}).setdefault('agents', {})
        agents[node] = values
        self.fake_behavior(**self.fake_options)

    def start(self, node='pm1tl1', *, codex=False, **values):
        self.ready()
        if codex:
            self.pid(models={'model': 'gpt-6.1-sol', 'effort': 'high'})
            self.run('pid', 'approve', 'alpha-p1')
        self.agent(node, binary='codex' if codex else 'claude', mode='persist',
                   auto_ack=False, **values)
        result = self.run('launch', node)
        self.wait_launch(node)
        return result

    def wait_launch(self, node, timeout=10):
        if self.is_stub:
            return stub_a11.nothing()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.launches(node):
                return self.launches(node)[-1]
            time.sleep(.02)
        return None

    def lane(self, node='pm1tl1'):
        launches = self.launches(node)
        return Path(launches[-1]['cwd']) if launches else self.repo_a / '.ygg-worktrees/alpha-p1/lane' / node

    def current(self, node='pm1tl1'):
        receipts = self.a5_receipts(node)
        return receipts[-1] if receipts else None

    def activation(self, node='pm1tl1'):
        launches = self.launches(node)
        if launches:
            return launches[-1]['env'].get('YGG_ACTIVATION')
        receipt = self.current(node) or {}
        return receipt.get('id') or receipt.get('activation')

    def call(self, command, *, node='pm1tl1', tool_id=None, cwd=None):
        payload = {'tool_name': 'Bash', 'tool_input': {'command': command}}
        if tool_id:
            payload['tool_use_id'] = tool_id
        return self.hook(payload, node=node, cwd=cwd)

    def edit(self, file, *, node='pm1rv1'):
        return self.hook({'tool_name': 'Edit', 'tool_input': {
            'file_path': str(file), 'old_string': 'a', 'new_string': 'b'}}, node=node)

    def post(self, command='make gate', *, node='pm1tl1', tool_id='tu-1', exit=None, cwd=None):
        payload = {'tool_name': 'Bash', 'tool_input': {'command': command}, 'tool_use_id': tool_id}
        if exit is not None:
            payload.update(error='Exit code ' + str(exit), is_interrupt=False)
        return self.posttool(payload, node=node, cwd=cwd)

    def notice(self, result):
        if self.is_stub:
            return stub_a11.nothing()
        try:
            return json.loads(result.stdout).get('hookSpecificOutput', {}).get('additionalContext')
        except ValueError:
            return None

    def send(self, number=11, *, sender='pm1', recipient='pm1tl1', urgent=False,
             body='use the d6c1 script', kind='work'):
        self.run('test', 'set-next', 'message', str(number))
        args = ['send', '--from', sender, '--to', recipient, '--kind', kind, '--body', body]
        if urgent:
            args += ['--urgent']
        return self.run(*args)

    def unread(self, count=2, *, body='use the d6c1 script'):
        self.start()
        if count:
            self.send(11, body=body)
        if count > 1:
            self.send(12, urgent=True)

    def ack(self, number, *, node='pm1tl1', caller=None):
        return self.run('inbox', node, '--ack', f'm-{number:04}', env={
            'YGG_NODE': node if caller is None else caller, 'YGG_ACTIVATION': self.activation(node)})

    def node(self, node='pm1tl1'):
        status = self.status() or {}
        return next((row for row in status.get('nodes', []) if row['id'] == node), None)

    def project(self, project='alpha-p1'):
        status = self.status() or {}
        return next((row for row in status.get('projects', []) if row['id'] == project), None)

    def wp(self, package='WP-1'):
        status = self.status() or {}
        return next((row for row in status.get('work_packages', []) if row['id'] == package), None)

    def line(self, name='pm1tl1'):
        lines = self.run('status').stdout.splitlines()
        return next((line for line in lines if name in line), '')

    def alerts(self, kind=None, *, user=False, node=None):
        data = self.command_json('alerts', *(['--for-user'] if user else []), '--json')
        if not isinstance(data, list):
            return []
        return [dict(row, text=row.get('message', row.get('text')))
                for row in data if (kind is None or row.get('kind') == kind)
                and (node is None or row.get('node') == node)]

    def tick(self, count=1):
        result = None
        for _ in range(count):
            result = self.run('conductor', 'tick')
        return result

    @contextmanager
    def tick_until_reserved(self, node='pm1tl1', timeout=10):
        if self.is_stub:
            yield stub_a11.nothing()
            return
        directory = self.state / 'activations'
        previous = set(directory.glob('*.json'))
        process = subprocess.Popen([self.cli, 'conductor', 'tick'], cwd=self.cwd,
            env=self.env.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + timeout
            reservation = None
            while time.monotonic() < deadline:
                for path in directory.glob('*.json'):
                    if path in previous:
                        continue
                    record = self.json_file(path) or {}
                    phase = record.get('phase', record.get('state'))
                    if (record.get('node') == node and phase in {'reserved', 'starting'}
                            and not record.get('terminal') and not record.get('exited_at')):
                        reservation = record
                        break
                if process.poll() is not None:
                    raise AssertionError('The reserving tick ended before the held-start step for ' + node)
                if reservation is not None:
                    break
                time.sleep(.01)
            if reservation is None:
                raise AssertionError('No new reserved or starting activation for ' + node)
            yield reservation
            process.communicate(timeout=180)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def fail_command(self, command='make gate', count=3, *, cwd=None):
        for number in range(count):
            self.call(command, tool_id='tu-f' + str(number), cwd=cwd)
            self.post(command, tool_id='tu-f' + str(number), exit=1, cwd=cwd)

    def inbox(self, node='pm1', *, caller=None):
        return self.run('inbox', node, env={'YGG_NODE': caller or node})

    def agent_steps(self, steps, *, node='pm1tl1', codex=False):
        steps = copy.deepcopy(steps)
        captures = []
        for step in steps:
            arguments = step.get('run', [])
            if arguments[:2] != ['git', 'commit']:
                continue
            self._capture_sequence += 1
            path = self.root / 'fake-control' / node / ('git-observation-' + str(self._capture_sequence) + '.json')
            captures.append(path)
            program = ("import json,pathlib,subprocess,sys; "
                "observe=lambda: [subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True).stdout, "
                "subprocess.run(['git','diff','--cached'],capture_output=True,text=True).stdout]; "
                "before=observe(); result=subprocess.run(sys.argv[2:]); after=observe(); "
                "pathlib.Path(sys.argv[1]).write_text(json.dumps({'before':before,'after':after,'exit':result.returncode})); "
                "sys.exit(result.returncode)")
            step['run'] = [sys.executable, '-c', program, str(path), *arguments]
        self.agent(node, binary='codex' if codex else 'claude', mode='script',
                   auto_ack=False, posttool=True, steps=steps)
        self.run('launch', node)
        self.wait_exit(node)
        if self.is_stub:
            return stub_a11.run()
        for path in captures:
            observation = self.json_file(path)
            if observation is not None:
                self.commit_observations.append(observation)
        records = self.calls('codex' if codex else 'claude')
        step = next((row for row in reversed(records) if row.get('event') == 'step'
                     and row.get('id') == steps[-1]['id']), {})
        output = next((row for row in reversed(records) if row.get('event') == 'step-output'
                       and row.get('id') == steps[-1]['id']), {})
        return subprocess.CompletedProcess([], step.get('exit_code', 127),
                                          output.get('stdout', ''), output.get('stderr', ''))

    def commit(self, *, file='clean.txt', text='hello\n', message='x', arguments=None,
               node='pm1tl1', codex=False, stage=True):
        self.ready()
        steps = [{'id': 'file-' + str(self._tool_sequence), 'write_file': {'path': file, 'text': text}}]
        if stage:
            steps += [{'id': 'add-' + str(self._tool_sequence), 'run': ['git', 'add', '--', file]}]
        steps += [{'id': 'commit-' + str(self._tool_sequence),
                   'run': ['git', 'commit', *(arguments if arguments is not None else ['-m', message])]}]
        self._tool_sequence += 1
        return self.agent_steps(steps, node=node, codex=codex)

    def authorised(self, package='WP-1', *, lane='pm1tl1', build=True):
        self.ready()
        known = getattr(self, '_authorised_packages', set())
        if package not in known:
            self.package(package, lane, criteria=True, authorise=True)
            known.add(package)
            self._authorised_packages = known
        self.agent(lane, mode='exit0')
        self.run('launch', lane)
        self.wait_exit(lane)
        built = getattr(self, '_built_packages', set())
        if build and package not in built:
            directory = self.lane(lane)
            self.write(directory / (package + '.txt'), package + '\n')
            self.trusted_git(directory, 'add', '--', package + '.txt')
            self.clean_trusted_commit(directory)
            built.add(package)
            self._built_packages = built
        return self.ref('HEAD', self.lane(lane))

    def open_review(self, *, model=None, package='WP-1', mode='persist'):
        if model:
            self.ready(models={'review_model': model})
        self.authorised(package)
        self.agent('pm1rv1', binary='codex', mode=mode, auto_ack=False)
        self.agent('pm1rv1', mode=mode, auto_ack=False)
        result = self.run('wp', 'ready', '--from', 'pm1tl1', '--package', package)
        self.tick()
        self.wait_launch('pm1rv1')
        return result

    def verdict(self, review='rv-0001', verdict='approve', *, findings=None, exit_code=0):
        launch = self.launches('pm1rv1')
        folder = Path(launch[-1]['env']['YGG_REVIEW_DIR']) if launch else self.state / 'scratch/empty'
        body = {'review': review, 'verdict': verdict}
        if findings is not None:
            body['findings'] = findings
        self.write(folder / 'verdict.json', json.dumps(body))
        self.exit_agent('pm1rv1')
        self.wait_exit('pm1rv1')
        self.tick()

    def verdict_between_reviews(self, review):
        launches = self.launches('pm1rv1')
        folder = Path(launches[-1]['env']['YGG_REVIEW_DIR']) if launches else self.state / 'scratch/empty'
        self.write(folder / 'verdict.json', json.dumps({'review': review, 'verdict': 'approve'}))
        self.exit_agent('pm1rv1')
        self.wait_exit('pm1rv1')
        self.agent('pm1rv1', binary='codex', mode='persist')
        self.tick()

    def accepted(self, package='WP-2', *, file='scripts/a.py'):
        self.authorised(package, build=False)
        directory = self.lane()
        self.write(directory / file, 'a\n')
        self.trusted_git(directory, 'add', '--', file)
        self.trusted_git(directory, '-c', 'user.name=Example Builder', '-c',
                         'user.email=builder@example.invalid', 'commit', '-m', package)
        sha = self.ref('HEAD', directory)
        self.agent('pm1rv1', binary='codex', mode='persist')
        self.run('wp', 'ready', '--from', 'pm1tl1', '--package', package)
        self.tick()
        count = getattr(self, '_accepted_review_number', 0) + 1
        self._accepted_review_number = count
        self.verdict(f'rv-{count:04}')
        self.run('acceptance', 'record', '--from', 'pm1su1', '--package', package,
                 '--sha', sha, '--verdict', 'accept')
        return sha

    def prompt(self, node='pm1tl1'):
        launches = self.launches(node)
        if not launches:
            return ''
        call = launches[-1]
        text = call.get('stdin', '')
        for arg in call['argv']:
            if len(arg) > 4096:
                continue
            path = Path(arg)
            if not path.is_absolute():
                path = Path(call['cwd']) / path
            if self.exists(path) and self.mode(path) == 0o600:
                text += self.read(path)
        return text

    def launch_env(self, node='pm1tl1'):
        calls = self.launches(node)
        return calls[-1]['env'] if calls else None

    def hook_settings(self, node='pm1tl1'):
        return self.launch_settings(node)

    def wait_control(self, name, node='pm1tl1'):
        self.write(self.root / 'fake-control' / node / name, '')

    def signal_agent(self, node='pm1tl1', sig=signal.SIGKILL, *, supervisor=False):
        if self.is_stub:
            return stub_a11.nothing()
        receipt = self.current(node) or {}
        pid = receipt.get('supervisor_pid' if supervisor else 'pid')
        if pid:
            os.kill(pid, sig)

    def later(self, seconds):
        instant = datetime.fromisoformat(T.replace('Z', '+00:00')) + timedelta(seconds=seconds)
        self.at(instant.isoformat())

    def elapsed(self, action):
        before = time.monotonic()
        result = action()
        return result, time.monotonic() - before

    def wall_clock(self):
        self.env.pop('YGG_NOW', None)
        self.set_load('0.10')

    def disk_yaml(self, path):
        if self.is_stub:
            return stub_a11.nothing()
        return yaml.safe_load(self.read(path))

    def list_files(self, directory):
        if self.is_stub:
            return stub_a11.entries()
        return sorted(str(p.relative_to(directory)) for p in Path(directory).rglob('*') if p.is_file())

    def fixture(self, name):
        if self.is_stub:
            return stub_a11.nothing()
        return json.loads(self.read(Path(__file__).parent / 'fixtures' / name))

    def home_hook(self, location='default', *, code=1, body=None):
        folder = self.repo_a / ('.git/hooks' if location == 'default' else 'configured-hooks')
        if location != 'default':
            self.trusted_git(self.repo_a, 'config', 'core.hooksPath', folder)
        script = body or ('printf x >> ' + shlex.quote(str(self.root / 'home-hook-ran')) + '\nexit ' + str(code) + '\n')
        self.executable(folder / 'pre-commit', script)
        return folder

    def agent_git(self, *arguments, node='pm1tl1', codex=False):
        self._tool_sequence += 1
        return self.agent_steps([{'id': 'git-' + str(self._tool_sequence),
                                 'run': ['git', *map(str, arguments)]}], node=node, codex=codex)

    def wait_step(self, *, node='pm1tl1', steps=None, codex=False):
        self.agent(node, binary='codex' if codex else 'claude', mode='script',
                   auto_ack=False, posttool=True, steps=[{'id': 'w1', 'wait': 'go-1'}, *(steps or [])])
        self.run('launch', node)
        self.wait_launch(node)

    def wait_steps_done(self, node='pm1tl1'):
        self.wait_control('go-1', node)
        self.wait_exit(node)
        if self.is_stub:
            return stub_a11.run()
        records = [r for r in self.calls() if r.get('event') == 'step' and r.get('node') == node]
        step = records[-1] if records else {}
        outputs = [r for r in self.calls() if r.get('event') == 'step-output'
                   and r.get('id') == step.get('id') and r.get('node') == node]
        output = outputs[-1] if outputs else {}
        return subprocess.CompletedProcess([], step.get('exit_code', 127),
                                          output.get('stdout', ''), output.get('stderr', ''))

    def fault_commit(self, fault, *, accepted=False):
        if fault == 'frozen':
            self.accepted('WP-2')
        self.authorised('WP-1')
        directory = self.lane()
        name = {'frozen': 'scripts/a.py', 'workflow': '.github/workflows/x.yml',
                'credential': 'config/.env', 'hook': '.githooks/pre-commit'}.get(fault, 'fault.txt')
        text = {'denylist': 'zebra-orchid-7', 'secret': 'gho_' + 'A' * 36}.get(fault, 'b\n')
        self.write(directory / name, text)
        self.trusted_git(directory, 'add', '--', name)
        message = {'trailer': 'x\n\nCo-Authored-By: Claude <noreply@anthropic.com>',
                   'lower-trailer': 'x\n\nco-authored-by : Claude <x@example.invalid>'}.get(fault, 'x')
        env = self.env.copy()
        env.update(GIT_AUTHOR_NAME='Example Builder', GIT_AUTHOR_EMAIL='builder@example.invalid',
                   GIT_COMMITTER_NAME='Example Builder', GIT_COMMITTER_EMAIL='builder@example.invalid')
        prefix = 'GIT_AUTHOR' if fault == 'author' else 'GIT_COMMITTER'
        if fault in {'author', 'committer'}:
            env[prefix + '_NAME'], env[prefix + '_EMAIL'] = 'Other Person', 'other@example.invalid'
        if not self.is_stub:
            subprocess.run(['git', '-C', str(directory), '-c', 'core.hooksPath=/dev/null',
                            'commit', '-m', message], env=env, text=True, capture_output=True, check=True)
        sha = self.ref('HEAD', directory)
        if accepted:
            self.seed_acceptance('alpha-p1', 'WP-1', sha)
        return sha

    def hooks_snapshot(self):
        if self.is_stub:
            return stub_a11.nothing()
        return {str(p.relative_to(self.repo_a / '.git/hooks')): p.read_bytes()
                for p in (self.repo_a / '.git/hooks').rglob('*') if p.is_file()}

    def ready(self, project='alpha-p1', **overrides):
        if getattr(self, '_approved', False) and not overrides:
            return stub_a11.run() if self.is_stub else subprocess.CompletedProcess([], 0, '', '')
        result = super().ready(project, **overrides)
        self._approved = result.returncode == 0
        return result

    def readonly_inbox(self):
        if self.is_stub:
            return stub_a11.nothing()
        directory = self.state / 'inboxes'
        for path in [*directory.rglob('*'), directory]:
            mode = path.stat().st_mode & 0o777
            self.permission_paths.append((path, mode))
            path.chmod(0)

    def sync_given(self, *, conflict=False):
        self.authorised()
        directory = self.lane()
        self.write(directory / 'scripts/b.py', 'base\n')
        self.trusted_git(directory, 'add', '.')
        self.trusted_git(directory, 'commit', '-m', 'base')
        base = self.ref('HEAD', directory)
        self.trusted_git(self.repo_a, 'merge', '--ff-only', base)
        self.accepted('WP-1')
        if conflict:
            self.write(directory / 'scripts/b.py', 'lane\n')
            self.trusted_git(directory, 'add', '.')
            self.trusted_git(directory, 'commit', '-m', 'lane')
        self.write(self.repo_a / ('scripts/b.py' if conflict else 'scripts/m.py'), 'main\n')
        self.write(self.repo_a / '.github/workflows/ci.yml', 'name: main\n')
        self.trusted_git(self.repo_a, 'add', '.')
        self.trusted_git(self.repo_a, '-c', 'user.name=Other Person', '-c',
                         'user.email=other@example.invalid', 'commit', '-m', 'main')

    def record_ruling(self, **kwargs):
        return self.run('ruling', 'record', '--project', 'alpha-p1', '--text',
                        'commit with plain git on the lane', **kwargs)

    def receipt_text(self, node='pm1tl1'):
        receipt = self.current(node)
        if receipt is None:
            return ''
        return json.dumps(receipt)

    def legacy_receipt(self, *, unrelated=False):
        if self.is_stub:
            return stub_a11.nothing()
        receipt = self.current()
        if unrelated:
            process = subprocess.Popen(['sleep', '600'], env=self.env)
            self.processes.append(process)
            pid = process.pid
        else:
            pid = receipt['pid']
        env = dict(self.env, TZ='UTC')
        started = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], env=env,
                                 capture_output=True, text=True, check=True).stdout.strip()
        self.change_json(self.state / 'activations' / (self.activation() + '.json'),
                         pid=pid, process_started_at=started)
        return pid

    def alive(self, pid):
        if self.is_stub:
            return stub_a11.nothing()
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def busy(self, *, child=None):
        self.config(max_silence_minutes=1)
        self.later(-120)
        self.start(**({'child': child} if child else {}))
        self.call('make gate', tool_id='tu-7')
        self.at(T)

    def close_stage(self):
        return self.close_stage_number(1)

    def cap(self, rounds=5):
        self.ready(review_rounds=rounds)
        self.authorised()
        for number in range(1, rounds + 1):
            self.agent('pm1rv1', binary='codex', mode='persist')
            self.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
            self.tick()
            self.wait_launch('pm1rv1')
            self.verdict(f'rv-{number:04}', 'revise', findings=[{
                'file': 'scripts/a.py', 'line': 12, 'text': 'unsafe report path'}])
        return self.pending()

    def answer_cap(self, answer):
        pending = self.pending() or {}
        decisions = pending.get('decisions', []) if isinstance(pending, dict) else pending
        identifier = next((d.get('id') for d in decisions if 'WP-1' in json.dumps(d)), '')
        return self.run('decide', identifier, '--option', answer)

    def held_review(self):
        self.authorised()
        self.agent('pm1rv1', binary='codex', mode='exit0')
        self.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
        for _ in range(4):
            self.tick()
            self.wait_exit('pm1rv1')

    def approve_review(self, package='WP-1'):
        self.open_review(package=package)
        self.verdict()

    def docs(self):
        home = self.make_cwd('docs')
        result = self.run('programme', 'create', 'docs', '--domain', 'docs', '--home', home, '--kind', 'vault-docs')
        self.run('project', 'create', 'docs-p1', '--programme', 'docs', '--pm', 'pm5')
        self.run('node', 'add', 'pm5tl1', '--role', 'tl', '--parent', 'pm5')
        self.run('node', 'add', 'pm5su1', '--role', 'su', '--parent', 'pm5', '--pairs-with', 'pm5tl1')
        self.run('agents', 'allow-minimal', '--yes', '--programme', 'docs')
        self.pid('docs-p1', 'pm5', 'docs', False)
        self.run('pid', 'approve', 'docs-p1')
        for node, kind in [('pm5', 'stage-plan'), ('pm5', 'work-package'), ('pm5su1', 'criteria')]:
            self.run('tracker', 'write', '--as', node, '--project', 'docs-p1', '--stage', '1',
                     '--package', 'WP-1', '--kind', kind, '--body', 'WP-1')
        self.run('wp', 'authorise', 'docs-p1', '--package', 'WP-1', '--lane', 'pm5tl1')
        return result

    def replace_criteria(self):
        return self.run('tracker', 'write', '--as', 'pm1su1', '--project', 'alpha-p1',
                        '--package', 'WP-1', '--kind', 'criteria', '--body', 'changed criteria')

    def clean_trusted_commit(self, directory=None):
        directory = directory or self.lane()
        return self.trusted_git(directory, '-c', 'user.name=Example Builder', '-c',
                                'user.email=builder@example.invalid', 'commit', '--allow-empty', '-m', 'x')

    def delayed_given(self, *, delay=20, load='0.10', threshold=3.0, background=False,
                      quiesce_pms=False):
        self.config(launch_confirm_seconds=1, load_threshold=threshold)
        self.ready()
        if background or quiesce_pms:
            self.quiesce_core_pms()
        self.set_load(load, now=T)
        self.start_delay(delay)
        self.send(10)
        if background:
            return self.tick_until_reserved()
        return self.run('conductor', 'tick')

    def quiesce_core_pms(self):
        for node in ('pm1', 'pm2', 'pm3'):
            self.agent(node, mode='exit0')
            self.run('launch', node)
            self.wait_exit(node)

    def close_task_launches(self, node='pm1'):
        if self.is_stub:
            return stub_a11.entries()
        receipts = {receipt.get('id') or receipt.get('activation'): receipt
                    for receipt in self.a5_receipts(node)}
        matches = []
        for launch in self.launches(node):
            receipt = receipts[launch['env']['YGG_ACTIVATION']]
            work = next((receipt[key] for key in
                         ('consumed', 'work', 'work_set', 'work_keys', 'work_items')
                         if key in receipt), None)
            if not work:
                continue
            if re.search(r'\bclose\b', json.dumps(work).replace('_', '-')):
                matches.append(launch)
        return matches

    def cap_decisions(self):
        if self.is_stub:
            return stub_a11.entries()
        pending = self.pending() or {}
        rows = pending.get('decisions', []) if isinstance(pending, dict) else pending
        return [row for row in rows if 'WP-1' in json.dumps(row)]

    def work_owed(self, node, package):
        if self.is_stub:
            return stub_a11.nothing()
        return package in self.inbox(node).stdout

    def review_revises(self):
        if self.is_stub:
            return stub_a11.nothing()
        text = self.line('WP-1')
        match = re.search(r'(\d+) revise', text)
        return int(match[1]) if match else None

    def late_verdict(self):
        self.agent('pm1rv1', binary='codex', mode='script', steps=[{
            'id': 'late', 'write_file': {'path': '{env:YGG_REVIEW_DIR}/verdict.json',
                                        'text': '{"review":"rv-0001","verdict":"revise"}'}}])
        self.run('launch', 'pm1rv1')
        self.wait_exit('pm1rv1')

    def close_stage_number(self, number):
        report = self.tracker / '_reports' / f'end-stage-{number}.md'
        self.write(report, f'# End of stage {number}\n')
        return self.run('stage', 'end', 'alpha-p1', '--within-tolerance', 'yes',
                        '--report', report, env={'YGG_NODE': 'pm1'})

    def agreement(self, **values):
        return self.change_json(self.tracker / '_ygg/agents-agreement.json', **values)

    def hook_files(self):
        if self.is_stub:
            return stub_a11.nothing()
        folder = self.state.with_name(self.state.name + '.safety') / 'hooks'
        return {path.name: self.read(path) for path in folder.iterdir() if path.is_file()}

    def child_events(self):
        return [r for r in self.calls() if r.get('event', '').startswith('child')]

    def group_alive(self, node='pm1tl1'):
        if self.is_stub:
            return stub_a11.nothing()
        receipt = self.current(node) or {}
        pgid = receipt.get('pid')
        if not pgid:
            return None
        rows = subprocess.run(['ps', '-axo', 'pid=,pgid=,stat='], capture_output=True,
                              text=True, check=True).stdout.splitlines()
        return any(int(parts[1]) == pgid and not parts[2].startswith('Z')
                   for line in rows if len(parts := line.split()) >= 3)

    def wait_group_dead(self, seconds=8):
        if self.is_stub:
            return stub_a11.nothing()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.group_alive():
                return True
            time.sleep(.02)
        return False

    def index_lock(self, node='pm1tl1'):
        if self.is_stub:
            return self.root / 'empty-index.lock'
        path = self.trusted_git(self.lane(node), 'rev-parse', '--git-path', 'index.lock').stdout.strip()
        return Path(path) if Path(path).is_absolute() else self.lane(node) / path

    def hold_lock_outside(self):
        path = self.index_lock()
        if self.is_stub:
            return stub_a11.nothing()
        import sys
        process = subprocess.Popen([sys.executable, '-c',
            'import sys,time; f=open(sys.argv[1],"w"); f.flush(); time.sleep(600)', str(path)],
            env=self.env, start_new_session=True)
        self.processes.append(process)
        self.wait_file(path)
        return process.pid

    def fresh_programme(self):
        home = self.make_cwd('gamma')
        self.repository(home)
        self.run('programme', 'create', 'gamma', '--domain', 'gamma', '--home', home, '--kind', 'git-repo')
        self.run('project', 'create', 'gamma-p1', '--programme', 'gamma', '--pm', 'pm4')
        self.run('node', 'add', 'pm4tl1', '--role', 'tl', '--parent', 'pm4')
        self.run('send', '--from', 'pm4', '--to', 'pm4tl1', '--kind', 'work', '--body', 'x')

    def prompts(self, node):
        if self.is_stub:
            return stub_a11.entries()
        return [launch.get('stdin', '') for launch in self.launches(node)]

    def seconds_between(self, start, end):
        return (datetime.fromisoformat(end.replace('Z', '+00:00')) -
                datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds()

    def seconds_between_whole_seconds(self, start, end):
        return (datetime.fromisoformat(end.replace('Z', '+00:00')).replace(microsecond=0) -
                datetime.fromisoformat(start.replace('Z', '+00:00')).replace(microsecond=0)).total_seconds()

    def package_state(self, package, state, *, title=None):
        self.package(package, criteria=True)
        values = {'state': state, 'lane': 'pm1tl1'}
        if title is not None:
            values['title'] = title
        self.set_header(self.tracker / 'projects/alpha-p1/issues' / (package + '.md'), **values)

    def checkpoint(self, text):
        return self.run('tracker', 'write', '--as', 'pm1', '--project', 'alpha-p1',
                        '--kind', 'checkpoint', '--body', text)

    def dashboard(self):
        return self.state_text('dashboard/alpha.html')

    def dashboard_structure(self):
        if self.is_stub:
            return stub_a11.nothing()
        from html.parser import HTMLParser
        class Columns(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.columns = []
                self.packages = {}
            def handle_starttag(self, tag, attrs):
                data = dict(attrs)
                parent = next((column for _, column in reversed(self.stack) if column), None)
                column = data.get('data-column', parent)
                if 'data-column' in data:
                    self.columns.append(data['data-column'])
                if 'data-package' in data:
                    self.packages[data['data-package']] = column
                if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
                    self.stack.append((tag, column))
            def handle_endtag(self, tag):
                for i in range(len(self.stack)-1, -1, -1):
                    if self.stack[i][0] == tag:
                        self.stack = self.stack[:i]
                        break
        parser = Columns()
        parser.feed(self.dashboard())
        return {'columns': parser.columns, 'packages': parser.packages}

    def server(self):
        if self.is_stub:
            return stub_a11.text()
        process = subprocess.Popen([self.cli, 'dashboard', 'serve', 'alpha', '--port', '0'],
            env=self.env, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(process.stdout.readline)
        try:
            line = future.result(timeout=10).strip()
        except TimeoutError:
            process.kill()
            line = ''
        pool.shutdown(wait=False)
        match = re.search(r'http://127\.0\.0\.1:(\d+)/', line)
        self.server_port = int(match[1]) if match else None
        return line

    def http(self, path='/', *, method='GET', host=None):
        if self.is_stub:
            return stub_a11.nothing()
        if self.server_port is None:
            return None
        import http.client
        connection = http.client.HTTPConnection('127.0.0.1', self.server_port, timeout=5)
        connection.request(method, path, headers={'Host': host} if host else {})
        response = connection.getresponse()
        result = (response.status, response.read().decode())
        connection.close()
        return result

    def external_connect(self):
        if self.is_stub:
            return stub_a11.nothing()
        addresses = [entry[4][0] for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
        address = next((a for a in addresses if not a.startswith('127.')), None)
        if not address:
            raise AssertionError('AC-50 requires a host with a non-loopback IPv4 address')
        try:
            with socket.create_connection((address, self.server_port), timeout=5):
                return True
        except ConnectionRefusedError:
            return False

    def notifications(self):
        return [r for r in self.calls() if r['binary'] in {'t3', 'osascript', 'phone-push'}]

    def delivery(self):
        return [dict(row, state=row.get('step', row.get('state')))
                for row in self.state_lines('alerts-delivered.jsonl')]

    def t3_sends(self, thread):
        return [r for r in self.calls('t3') if r['argv'][:2] == ['thread', 'send'] and thread in r['argv']]

    def auto_alerts(self, *, headless=False):
        if headless:
            self.config(launcher='headless')
        self.config(alert_forwarder='auto', alert_t3_thread='thr-l1')
        self.env['YGG_T3_BIN'] = str(self.root / 'bin/t3')

    def t3_running(self):
        self.ready()
        self.config(launcher='t3')
        self.a5_t3(cli_version='0.1.9', server_version='0.1.9')
        # This fixture instance returns the thread identifier in AC-36's Given.
        binary = self.root / 'bin/t3'
        source = self.read(binary)
        self.write(binary, source.replace('identifier = "th-" + str(state["next_thread"])',
                                          'identifier = "thr-tl1"'))
        result = self.run('launch', 'pm1tl1')
        if not self.is_stub:
            path = self.root / 't3-state.json'
            state = self.json_file(path)
            state['threads']['thr-tl1'].update(status='running', active=True)
            self.write(path, json.dumps(state))
        return result

    def programme_config(self, **values):
        path = self.tracker / 'initiatives/alpha/initiative.json'
        if self.is_stub:
            return stub_a11.nothing()
        data = self.json_file(path)
        if data is None:
            raise AssertionError('programme fixture missing at ' + str(path))
        data.update(values)
        self.write(path, json.dumps(data))

    def set_package_config(self, **values):
        path = self.tracker / 'projects/alpha-p1/issues/WP-1.md'
        return self.set_header(path, **values)

    def state_and_tracker_snapshot(self):
        if self.is_stub:
            return stub_a11.nothing()
        return {str(p.relative_to(self.root)): p.read_bytes()
                for root in [self.state, self.tracker] for p in root.rglob('*') if p.is_file()}

    def summary_given(self, *, decisions=0, title='Export'):
        self.ready()
        for package, state, name in [('WP-1', 'merged', 'Save module'),
                                     ('WP-2', 'merged', 'Load module'), ('WP-3', 'in-review', title)]:
            self.package_state(package, state, title=name)
        for number in range(decisions):
            self.decision(number=number + 3)

    def dashboard_given(self, *, summary=None, all_states=False):
        self.ready()
        mapping = {}
        if all_states:
            states = [('planned', 'to-do'), ('authorised', 'working'), ('rejected', 'working'),
                ('in-review', 'checking'), ('ready-for-test', 'checking'), ('accepted', 'done'),
                ('merged', 'done'), ('stopped', 'needs-you'), ('review-exhausted', 'needs-you'),
                ('suspended', 'needs-you'), ('cancelled', None)]
            for number, (state, column) in enumerate(states, 1):
                package = 'WP-' + str(number)
                self.package_state(package, state)
                if column:
                    mapping[package] = column
            self.start()
            self.call('pytest', tool_id='tu-pytest')
        if summary is not None:
            self.checkpoint(summary)
        return mapping

    def parents(self, branch, directory=None):
        return self.trusted_git(directory or self.repo_a, 'rev-list', '--parents', '-1', branch).stdout.split()[1:]

    def merge_given(self, *, last=True, unmerged=False, check=True, hook='absent', merge_mode='prepare', setup=None):
        stages = [{'number': 1, 'name': 'Foundations', 'planned_end': '2026-11-19T17:00:00Z'}]
        if not last:
            stages += [{'number': 2, 'name': 'Build', 'planned_end': '2026-11-20T17:00:00Z'}]
        options = {'stages': stages, 'merge_mode': merge_mode}
        if setup:
            options['merge_check_setup'] = setup
        self.ready(**options)
        for number in [1, 2]:
            package = 'WP-' + str(number)
            self.accepted(package, file=f'module{number}.txt')
            if not (unmerged and number == 2):
                self.run('merge', 'lane', 'alpha-p1', '--lane', 'pm1tl1', '--repo', self.repo_a,
                         env={'YGG_NODE': 'pm1'})
        if hook != 'absent':
            marker = shlex.quote(str(self.root / 'home-hook-ran'))
            scripts = {
                'pass': 'git diff --cached --name-only >> ' + marker + '\nexit 0\n',
                'fail-once': 'if test -e ' + marker + '; then exit 0; fi\ntouch ' + marker + '\necho "6 tests failed" >&2\nexit 1\n',
                'slow': 'sleep 30\nprintf x >> ' + marker + '\n',
                'timeout': 'sleep 120\n',
                'changed-files': 'printf changed >> module1.txt\nexit 0\n',
                'environment': 'printf "TZ=%s\\nPATH=%s\\n" "$TZ" "$PATH" > ' + shlex.quote(str(self.root / 'hook-env')) + '\n',
                'main-only': 'exit 1\n',
            }
            self.home_hook('configured' if hook == 'main-only' else 'default', body=scripts[hook])
            if hook == 'main-only':
                self.trusted_git(self.repo_a, 'config', 'core.hooksPath', '.githooks')
                self.executable(self.repo_a / '.githooks/pre-commit', 'exit 1\n')
                self.trusted_git(self.repo_a, 'add', '.githooks/pre-commit')
                self.clean_trusted_commit(self.repo_a)
                self.write(self.lane() / '.githooks/pre-commit', 'exit 0\n')
                self.trusted_git(self.lane(), 'add', '.githooks/pre-commit')
                self.clean_trusted_commit()
                integration = self.make_cwd('integration-fixture')
                self.trusted_git(self.repo_a, 'worktree', 'add', integration, INTEGRATION)
                self.trusted_git(integration, 'merge', '--no-ff', LANE, '-m', 'integration')
        self.close_stage_number(1)
        self.run('stage', 'approve', 'alpha-p1')
        if check and last and not unmerged:
            self.check_finished()

    def merge_check_started(self):
        if self.is_stub:
            return stub_a11.nothing()
        return 'merge check:' in self.line('alpha-p1')

    def check_finished(self, timeout=15):
        if self.is_stub:
            return stub_a11.nothing()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.tick()
            text = self.line('alpha-p1')
            if 'merge check: passed' in text or 'merge check: failed' in text:
                return text
            time.sleep(.05)
        return self.line('alpha-p1')

    def redo_package(self):
        self.commit(file='module1.txt', text='fixed\n', message='WP-1 fix')
        self.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
        self.tick()
        text = self.tracker_text('projects/alpha-p1/issues/WP-1.md')
        records = re.findall(r'(?ms)^```yaml\s*\n(.*?)^```\s*$', text)
        review = next((match[0] for record in reversed(records)
                       if (match := re.search(r'\brv-\d{4}\b', record))), None)
        if review is None and not self.is_stub:
            raise AssertionError('Redone WP-1 has no current review record')
        self.verdict(review)
        self.run('acceptance', 'record', '--from', 'pm1su1', '--package', 'WP-1',
                 '--sha', self.ref('HEAD', self.lane()), '--verdict', 'accept')
        self.run('merge', 'lane', 'alpha-p1', '--lane', 'pm1tl1', '--repo', self.repo_a, env={'YGG_NODE': 'pm1'})

    def move_integration(self):
        directory = None
        worktrees = self.trusted_git(self.repo_a, 'worktree', 'list', '--porcelain').stdout
        for worktree in worktrees.split('\n\n'):
            lines = worktree.splitlines()
            if f'branch refs/heads/{INTEGRATION}' in lines:
                directory = Path(next(line[9:] for line in lines if line.startswith('worktree ')))
                break
        if directory is None:
            directory = self.make_cwd('integration-move')
            self.trusted_git(self.repo_a, 'worktree', 'add', directory, INTEGRATION)
        self.clean_trusted_commit(directory)

    def kill_conductor_during_send(self):
        if self.is_stub:
            return stub_a11.nothing()
        process = subprocess.Popen([self.cli, 'conductor', 'run'], cwd=self.cwd, env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(row.get('state') == 'sent' for row in self.delivery()):
                process.kill()
                process.wait(timeout=5)
                return
            time.sleep(.02)
        process.kill()
        process.wait(timeout=5)
        raise AssertionError('Given requires an alert channel at sent')

    def restart_conductor(self):
        if self.is_stub:
            return stub_a11.nothing()
        process = subprocess.Popen([self.cli, 'conductor', 'run'], cwd=self.cwd, env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.state_text('conductor.heartbeat'):
            time.sleep(.02)
        process.terminate()
        process.wait(timeout=5)

    def independent_review_and_test_given(self):
        self.trusted_git(self.repo_a, 'branch', LANE, 'main')
        for package in ['WP-3', 'WP-4']:
            self.package(package, criteria=True, authorise=True)
            self.run('wp', 'complete', '--from', 'pm1tl1', '--package', package)
        self.ready(wip_limit=4)
        for package, lane in [('WP-1', 'pm1tl1'), ('WP-2', 'pm1tl2')]:
            self.authorised(package, lane=lane)
            self.run('wp', 'ready', '--from', lane, '--package', package)
        self.agent('pm1su1', mode='exit0')
        self.agent('pm1rv1', binary='codex', mode='persist')

    def tick_until_quiet(self):
        for number in range(1, 3):
            self.tick()
            launches = self.launches('pm1rv1')
            folder = Path(launches[-1]['env']['YGG_REVIEW_DIR']) if launches else self.state / 'scratch/empty'
            self.write(folder / 'verdict.json', json.dumps({'review': f'rv-{number:04}', 'verdict': 'revise'}))
            self.exit_agent('pm1rv1')
            self.wait_exit('pm1rv1')
            self.agent('pm1rv1', binary='codex', mode='persist')
            self.tick()
            self.wait_exit('pm1su1')
        self.tick()

    def large_review_given(self):
        self.authorised()
        directory = self.lane()
        self.write(directory / 'large.txt', 'a' * 149000 + '\n')
        self.trusted_git(directory, 'add', 'large.txt')
        self.clean_trusted_commit(directory)
        base = self.trusted_git(self.repo_a, 'merge-base', LANE, INTEGRATION).stdout.strip()
        diff = self.trusted_git(directory, 'diff', base + '..HEAD').stdout
        if not self.is_stub:
            self.write(directory / 'large.txt', 'a' * (149000 + 150000 - len(diff)) + '\n')
            self.trusted_git(directory, 'add', 'large.txt')
            self.trusted_git(directory, '-c', 'user.name=Example Builder', '-c', 'user.email=builder@example.invalid', 'commit', '--amend', '--no-edit')
        diff = self.trusted_git(directory, 'diff', base + '..HEAD').stdout
        self.agent('pm1rv1', binary='codex', mode='persist')
        self.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
        self.tick()
        return diff

    def release_script(self, results):
        path = self.root / 'checkpoint.json'
        self.write(path, json.dumps(results))
        return self.external([self.core_path('scripts/release-switches'), '--results', path,
                              '--out', self.root / 'release.yaml'])

    def pilot(self):
        if self.is_stub:
            return stub_a11.nothing()
        from collections import Counter
        replay = self.fixture('pilot1-replay.json')
        for step in replay['pm1']['steps']:
            if 'run' in step:
                step['run'] = [str(self.repo_a) if value == '<repo-a>' else value for value in step['run']]
        self.run('agents', 'allow-standard', '--yes')
        self.auto_alerts()
        self.pid(stages=[{'number': n, 'name': 'Foundations' if n == 1 else 'Build',
            'planned_end': '2026-11-19T17:00:00Z'} for n in (1, 2)])
        self.home_hook(code=0)
        self.pilot_base = self.ref('main')
        agents = {key: copy.deepcopy(value) for key, value in replay.items() if key != 'pm1rv1'}
        self.fake_options = {'claude': {'agents': agents}, 'codex': {'agents': {}}}
        self.fake_behavior(**self.fake_options)
        allowed_user_commands = []
        approved = False
        approved_stages = set()
        delivered_before_merge = False
        for _ in range(60):
            count = len(self.launches('pm1rv1'))
            if count < len(replay['pm1rv1']):
                self.agent('pm1rv1', binary='codex', **replay['pm1rv1'][count])
                self.agent('pm1rv1', **replay['pm1rv1'][count])
            self.tick()
            for node in ('pm1', 'pm1tl1', 'pm1su1', 'pm1rv1'):
                if self.launches(node):
                    self.wait_exit(node)
            alerts = self.alerts(user=True)
            if not approved and any(r['kind'] == 'ygg-pid-ready' for r in alerts):
                self.run('pid', 'approve', 'alpha-p1')
                allowed_user_commands.append(['pid', 'approve', 'alpha-p1'])
                approved = True
            project = self.project() or {}
            stage = project.get('stage') or {}
            if stage.get('state') == 'awaiting-approval' and stage.get('number') not in approved_stages:
                self.run('stage', 'approve', 'alpha-p1')
                allowed_user_commands.append(['stage', 'approve', 'alpha-p1'])
                approved_stages.add(stage['number'])
            sends = self.t3_sends('thr-l1')
            if project.get('state') == 'ready-to-merge' and any('ready to go into main' in ' '.join(r['argv']) for r in sends):
                delivered_before_merge = True
                self.run('merge', 'main', 'alpha-p1')
                allowed_user_commands.append(['merge', 'main', 'alpha-p1'])
            if (self.project() or {}).get('state') == 'closed':
                break
        user_alerts = Counter(r['kind'] for r in self.alerts(user=True))
        commits = self.trusted_git(self.repo_a, 'log', self.pilot_base + '..' + LANE, '--format=%an').stdout.splitlines()
        messages = self.trusted_git(self.repo_a, 'log', self.pilot_base + '..' + LANE, '--format=%B').stdout
        user_writes = [r for r in self.calls() if r.get('event') == 'step' and r.get('id', '').startswith(('commit-', 'merge-'))]
        return {'project': self.project(), 'packages': {p: (self.wp(p) or {}).get('state') for p in ['WP-1', 'WP-2']},
            'main_parents': self.parents('main'), 'integration': self.ref(INTEGRATION),
            'reviewer_launches': len(self.launches('pm1rv1')), 'authors': commits, 'messages': messages,
            'user_alerts': dict(user_alerts), 'unwanted_alerts': [r for r in self.alerts() if r['kind'] in {
                'ygg-stall', 'ygg-uncertain', 'ygg-launch-refused', 'ygg-review-held'}],
            'delivered_before_merge': delivered_before_merge,
            'home_hook_runs': len(self.read(self.root / 'home-hook-ran')),
            'user_commands_only_answers_and_main_merge': len(user_writes) >= 4 and all(
                command[:2] in [['pid', 'approve'], ['stage', 'approve'], ['merge', 'main']]
                for command in allowed_user_commands)}

    def no_dashboard_listener(self):
        if self.is_stub:
            return stub_a11.nothing()
        result = subprocess.run(['lsof', '-a', '-c', Path(self.cli).name, '-iTCP', '-sTCP:LISTEN', '-Fn'],
                                capture_output=True, text=True, check=False)
        return not result.stdout.strip()

    def pending_messages(self, node, kind=None):
        pending = (self.status() or {}).get('inbox_pending', [])
        ids = {row['msg_id'] for row in pending if row['node'] == node}
        return [row for row in self.state_lines('messages.jsonl') if row.get('msg_id') in ids
                and row.get('to') == node and row.get('outcome') == 'delivered'
                and (kind is None or row.get('kind') == kind)]
