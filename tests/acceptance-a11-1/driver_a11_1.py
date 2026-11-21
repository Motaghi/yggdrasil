import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time

from driver_a11 import DriverA11, INTEGRATION, LANE, T
import stub_a11_1


class DriverA11_1(DriverA11):
    def run(self, *args, **kwargs):
        if self.is_stub:
            self.__dict__.pop('_next_crash_point', None)
            return stub_a11_1.run()
        return super().run(*args, **kwargs)

    def core_tree(self, *, agreement=True):
        super().core_tree(agreement=agreement)
        for name, body in {'scripts/a.py': 'x = 1\n', 'scripts/b.py': 'base\n',
                           'scripts/c.py': 'base\n', '.gitignore': 'build/\n.env.local\n'}.items():
            self.write(self.repo_a / name, body)
        self.trusted_git(self.repo_a, 'add', '.')
        self.clean_trusted_commit(self.repo_a)
        self.l0 = self.ref('main')
        self.review_number = 0

    def agent(self, node='pm1tl1', **values):
        self.remove(self.root / 'fake-control' / node / 'exit')
        return super().agent(node, **values)

    def one_stage(self):
        return self.ready(stages=[{'number': 1, 'name': 'Foundations',
                                  'planned_end': '2026-11-19T17:00:00Z'}])

    def authorise(self, package='WP-1', lane='pm1tl1'):
        self.ready()
        prepared = getattr(self, '_prepared_lanes', set())
        if lane not in prepared:
            self.agent(lane, mode='exit', exit_code=0)
            self.run('launch', lane)
            self.wait_exit(lane)
            prepared.add(lane)
            self._prepared_lanes = prepared
        self.package(package, lane, criteria=True, authorise=True)
        return self.ref('HEAD', self.lane(lane))

    def build(self, file='scripts/a.py', text='x = 2\n', message='C1', node='pm1tl1'):
        result = self.commit(file=file, text=text, message=message, node=node)
        return result, self.ref('HEAD', self.lane(node))

    def review(self, package='WP-1', verdict='approve'):
        self.review_number += 1
        review = f'rv-{self.review_number:04}'
        self.agent('pm1rv1', binary='codex', mode='persist')
        self.agent('pm1rv1', mode='persist')
        result = self.run('wp', 'ready', '--from', 'pm1tl1', '--package', package)
        self.tick()
        self.wait_launch('pm1rv1')
        if verdict is not None:
            self.verdict(review, verdict)
        return result, self.ref('HEAD', self.lane())

    def accept(self, package='WP-1', head=None, verdict='accept'):
        return self.run('acceptance', 'record', '--from', 'pm1su1', '--package', package,
                        '--sha', head if head is not None else self.ref('HEAD', self.lane()),
                        '--verdict', verdict)

    def reviewed_accept(self, package='WP-1', *, file='scripts/b.py', text='work\n'):
        self.authorise(package)
        self.build(file, text, package)
        _, head = self.review(package)
        self.accept(package, head)
        self.wait_exit('pm1su1')
        return head

    def two_accepted(self, *, syncs=False, merge=False):
        self.one_stage()
        s1 = self.reviewed_accept('WP-1', file='scripts/b.py')
        s2 = self.reviewed_accept('WP-2', file='scripts/c.py')
        if syncs:
            self.main_change('docs/notes.md', 'first\n')
            self.sync()
            self.main_change('docs/more.md', 'second\n')
            self.sync()
        head = self.ref(LANE)
        if merge:
            self.merge_lane()
        return s1, s2, head

    def merge_lane(self):
        return self.run('merge', 'lane', 'alpha-p1', '--lane', 'pm1tl1', '--repo', self.repo_a,
                        env={'YGG_NODE': 'pm1'})

    def sync(self):
        return self.run('lane', 'sync', 'alpha-p1', '--lane', 'pm1tl1', env={'YGG_NODE': 'pm1'})

    def main_change(self, file='docs/notes.md', text='main\n'):
        self.write(self.repo_a / file, text)
        self.trusted_git(self.repo_a, 'add', '--', file)
        self.trusted_git(self.repo_a, '-c', 'user.name=Other Person', '-c',
                         'user.email=other@example.invalid', 'commit', '-m', 'main')
        return self.ref('main')

    def merge_base(self, *, check=True, hook=None):
        self.accepted_heads = self.two_accepted(merge=True)
        self.staged_files_hook() if hook is None else self.home_hook(body=hook)
        self.close_stage_number(1)
        self.run('stage', 'approve', 'alpha-p1')
        if check:
            self.check_finished(timeout=40)

    def staged_files_hook(self):
        self.home_hook(body='git diff --cached --name-only >> ' +
                       shlex.quote(str(self.root / 'home-hook-ran')) + '\nexit 0\n')

    def end_and_check(self):
        self.close_stage_number(1)
        self.run('stage', 'approve', 'alpha-p1')
        return self.check_finished(timeout=40)

    def merge_main(self):
        return self.run('merge', 'main', 'alpha-p1', env={'YGG_NODE': None, 'YGG_ACTIVATION': None})

    def contains(self, branch, commit):
        if self.is_stub:
            return stub_a11_1.nothing()
        return self.trusted_git(self.repo_a, 'merge-base', '--is-ancestor', commit, branch).returncode == 0

    def tree_text(self, branch, file):
        return self.trusted_git(self.repo_a, 'show', branch + ':' + file).stdout

    def clean_status(self, *, ignored=False, all_files=False):
        args = ['status', '--porcelain']
        if ignored:
            args.append('--ignored')
        if all_files:
            args.append('--untracked-files=all')
        return self.trusted_git(self.lane(), *args).stdout

    def lane_snapshot(self):
        if self.is_stub:
            return stub_a11_1.nothing()
        directory = self.lane()
        return {'head': self.ref(LANE),
                'index': self.trusted_git(directory, 'ls-files', '--stage').stdout,
                'files': {str(p.relative_to(directory)): p.read_bytes()
                          for p in directory.rglob('*') if p.is_file() and p.name != '.git'}}

    def cancel_snapshot(self):
        if self.is_stub:
            return stub_a11_1.nothing()
        return {'lane': self.lane_snapshot(), 'durable': self.state_and_tracker_snapshot()}

    def records(self, node='pm1tl1'):
        return self.a5_receipts(node)

    def work_packages(self, receipt):
        if self.is_stub:
            return stub_a11_1.entries()
        def find(value):
            if isinstance(value, dict):
                if isinstance(value.get('packages'), list):
                    return value['packages']
                for child in value.values():
                    result = find(child)
                    if result is not None:
                        return result
            return None
        return find(receipt) or []

    def pending_ids(self, node='pm1tl1'):
        return [r['msg_id'] for r in (self.status() or {}).get('inbox_pending', []) if r['node'] == node]

    def node_alerts(self, kind, node='pm1tl1'):
        return [r for r in self.alerts(kind) if r.get('node') == node]

    def retry_given(self, *, limit=1):
        self.ready()
        self.quiesce()
        self.clear_calls()
        self.config(retry_limit=limit, stall_threshold_minutes=1, stop_grace_seconds=2)
        self.retry_baseline = len(self.records())
        self.send(10, body='x')

    def timed_tick(self, seconds, *, wait=True):
        self.later(seconds)
        result = self.tick()
        if wait:
            self.wait_exit('pm1tl1')
        return result

    def retry_records(self):
        return self.records()[self.retry_baseline:]

    def pair_exhausted(self, packages=('WP-1',)):
        for package in packages:
            for number in range(1, 6):
                self.run('review', 'record', '--from', 'pm1tl1', '--package', package,
                         '--round', str(number), '--verdict', 'changes')

    def suspended(self, packages=('WP-1',)):
        self.run('envelope', 'grant', 'alpha-p1', '--until', '2026-11-19T12:01:00Z')
        self.decision(number=1)
        self.later(120)
        self.run('node', 'add', 'rv1', '--role', 'reviewer', '--parent', 'alpha')
        self.run('morning-go', 'build', '--programme', 'alpha', '--reviewer-node', 'rv1')
        for package in packages:
            self.set_header(self.tracker / 'projects/alpha-p1/issues' / (package + '.md'),
                            depends_on=['D-0001'])
        items = self.json_records('initiatives/alpha/morning-go')
        item = next((r['id'] for r in items if r.get('ref') == 'D-0001'), '')
        return self.run('morning-go', 'decide', item, 'not-ok')

    def cap_current(self, package='WP-1'):
        for _ in range(5):
            self.review(package, 'revise')

    def cancel(self, *packages, env=None):
        args = ['wp', 'cancel', 'alpha-p1']
        for package in packages or ('WP-1',):
            args += ['--package', package]
        return self.run(*args, env={'YGG_NODE': 'pm1'} if env is None else env)

    def cancel_commits(self, *, rejected=False):
        self.one_stage()
        self.authorise()
        _, c1 = self.build()
        _, c2 = self.build('scripts/d.py', 'new\n', 'C2')
        if rejected:
            self.review()
            self.accept(head=c2, verdict='reject')
        return c1, c2

    def interrupted(self):
        c1, c2 = self.cancel_commits()
        self.crash_at('cancel-after-drop-record')
        result = self.cancel()
        return c1, c2, result

    def finish(self, code=None, *, node='pm1tl1', env=None):
        args = ['wp', 'cancel', 'alpha-p1', '--finish', node]
        if code is not None:
            args += ['--yes', code]
        return self.run(*args, env=env)

    def finish_code(self, result):
        match = re.search(r'(?m)^finish code: (\S+)\s*$', result.stdout)
        return match[1] if match else ''

    def wait_terminal(self, node='pm1tl1', timeout=15):
        if self.is_stub:
            return stub_a11_1.nothing()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.current(node) or {}
            if record.get('exited_at') or record.get('outcome') == 'aborted' or record.get('state') == 'aborted':
                return record
            time.sleep(.02)
        return self.current(node)

    def activation_aborted(self, record):
        if self.is_stub:
            return stub_a11_1.nothing()
        return 'aborted' in (record.get('outcome'), record.get('state'))

    def erase(self, path):
        if self.is_stub:
            return stub_a11_1.nothing()
        path = Path(path)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def preview_mutations(self):
        lane = self.lane()
        self.write(lane / 'scripts/d.py', 'mine')
        self.write(lane / '.env.local', 'local\n')
        old = self.read(lane / 'scripts/b.py')
        self.write(lane / 'scripts/b.py', 'index-only\n')
        self.trusted_git(lane, 'add', 'scripts/b.py')
        self.write(lane / 'scripts/b.py', old)
        self.erase(lane / 'scripts/a.py')
        self.write(lane / 'scripts/a.py/note.txt', 'mine\n')
        own = {'.claude/settings.json': '{"guard":"keep"}\n', '.ygg-managed': 'keep\n'}
        exclude = self.trusted_git(lane, 'rev-parse', '--git-path', 'info/exclude').stdout.strip()
        path = Path(exclude) if Path(exclude).is_absolute() else lane / exclude
        self.append(path, '\n.claude/settings.json\n.ygg-managed\n')
        for name, text in own.items():
            self.write(lane / name, text)
        return own

    def integration_change(self, file='docs/notes.md', text='integration\n'):
        directory = None
        worktrees = self.trusted_git(self.repo_a, 'worktree', 'list', '--porcelain').stdout
        for worktree in worktrees.split('\n\n'):
            lines = worktree.splitlines()
            if f'branch refs/heads/{INTEGRATION}' in lines:
                directory = Path(next(line[9:] for line in lines if line.startswith('worktree ')))
                break
        temporary = directory is None
        if temporary:
            directory = self.make_cwd('integration-change')
            self.trusted_git(self.repo_a, 'worktree', 'add', directory, INTEGRATION)
        self.write(directory / file, text)
        self.trusted_git(directory, 'add', file)
        self.clean_trusted_commit(directory)
        head = self.ref('HEAD', directory)
        if temporary:
            self.trusted_git(self.repo_a, 'worktree', 'remove', directory)
        return head

    def combine_main(self):
        directory = None
        worktrees = self.trusted_git(self.repo_a, 'worktree', 'list', '--porcelain').stdout
        for worktree in worktrees.split('\n\n'):
            lines = worktree.splitlines()
            if f'branch refs/heads/{INTEGRATION}' in lines:
                directory = Path(next(line[9:] for line in lines if line.startswith('worktree ')))
                break
        temporary = directory is None
        if temporary:
            directory = self.make_cwd('combine-main')
            self.trusted_git(self.repo_a, 'worktree', 'add', directory, INTEGRATION)
        self.trusted_git(directory, 'merge', 'main')
        self.write(directory / 'scripts/b.py', 'work and main\n')
        self.trusted_git(directory, 'add', 'scripts/b.py')
        self.clean_trusted_commit(directory)
        head = self.ref('HEAD', directory)
        if temporary:
            self.trusted_git(self.repo_a, 'worktree', 'remove', directory)
        return head

    def alert_id(self, kind='ygg-launch-refused'):
        rows = self.alerts(kind)
        return rows[-1]['id'] if rows else None

    def alert_delivery(self, identifier):
        return [r for r in self.delivery()
                if r.get('alert_id', r.get('alert', r.get('id'))) == identifier]

    def alert_calls(self, binary, identifier):
        if self.is_stub:
            return stub_a11_1.entries()
        return [r for r in self.calls(binary) if identifier and identifier in json.dumps(r)]

    def owed_git(self, *arguments):
        self._tool_sequence += 1
        identifier = 'owed-git-' + str(self._tool_sequence)
        self.agent(mode='script', posttool=True, auto_ack=False,
                   steps=[{'id': identifier, 'run': ['git', *map(str, arguments)]}])
        self.tick()
        self.wait_exit('pm1tl1')
        if self.is_stub:
            return stub_a11_1.run()
        records = [r for r in self.calls() if r.get('id') == identifier]
        step = next((r for r in reversed(records) if r['event'] == 'step'), {})
        output = next((r for r in reversed(records) if r['event'] == 'step-output'), {})
        return subprocess.CompletedProcess([], step.get('exit_code', 127),
                                          output.get('stdout', ''), output.get('stderr', ''))
