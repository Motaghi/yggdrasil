import pytest

from driver import LANE, INTEGRATION


def option_values(argv, flag):
    return [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == flag]


def launch_count(d, node, binary):
    return sum(r['binary'] == binary for r in d.launches(node))


def assert_received_head(d):
    head = d.head()
    assert d.step('ready').returncode == 0
    assert d.lane_head() == head
    assert d.review('rv-0001').get('head') == head
    assert d.git_result(d.H, 'cat-file', '-e', head).returncode == 0


def assert_bad_head_kept(d):
    assert d.step('commit').returncode == 0
    assert d.head() == d.step('made-head').stdout.strip()


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-01')
@pytest.mark.parametrize('mode', ['builder_roles', 'config_codex', 'programme_reviewer'])
def test_builder_roles_and_programme_reviewer(factory, mode):
    if mode == 'builder_roles':
        d = factory(approved=False)
        results = [d.run('pid', 'approve', 'alpha-p1')]
        for node in ['pm1', 'pm1tl1', 'pm1tl1w1', 'pm1su1']:
            results.append(d.launch(node))
            assert launch_count(d, node, 'codex') == 1
            assert launch_count(d, node, 'claude') == 0
        assert all(r.returncode == 0 for r in results)
        assert all('Codex builds' not in r.stderr for r in results)
    elif mode == 'config_codex':
        d = factory(codex=False, approved=False, launcher='codex')
        assert d.run('pid', 'approve', 'alpha-p1').returncode == 0
        assert d.launch().returncode == 0
        assert launch_count(d, 'pm1tl1', 'codex') == 1
        assert 'gpt-6.1-sol' in d.launch_record().get('argv', [])
    else:
        d = factory()
        d.run('node', 'add', 'rv1', '--role', 'reviewer', '--parent', 'alpha')
        assert d.launch('rv1', binary='claude').returncode == 0
        assert launch_count(d, 'rv1', 'claude') == 1
        assert 'claude-opus-5-5' in d.launch_record('rv1').get('argv', [])
        assert launch_count(d, 'rv1', 'codex') == 0


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-02')
def test_private_clone_command_environment_and_shared_worker(d):
    d.launch()
    lead = d.launch_record()
    assert lead.get('cwd') == str(d.clone)
    argv, env = lead['argv'], lead['env']
    assert d.git_text(d.clone, 'rev-parse', '--absolute-git-dir') == str(d.clone / '.git')
    assert d.names(d.B) == ['.git', 'checkout', 'tmp']
    assert d.is_dir(d.B / '.git') and d.names(d.B / '.git') == []
    assert str(d.lanes) not in d.git_text(d.H, 'worktree', 'list', '--porcelain')
    assert option_values(argv, '--sandbox') == ['workspace-write']
    assert option_values(argv, '--cd') == [str(d.B)]
    for flag in ['--ignore-rules', '--strict-config']:
        assert flag in argv
    for value in ['hooks', 'apps', 'plugins', 'remote_plugin', 'plugin_sharing',
        'browser_use', 'browser_use_external', 'computer_use', 'in_app_browser',
        'in_app_local_automation', 'multi_agent', 'skill_mcp_dependency_install',
        'image_generation', 'tool_suggest', 'system_proxy_fallback', 'respect_system_proxy']:
        assert value in option_values(argv, '--disable')
    relay = d.S / 'relay' / d.activation()
    assert option_values(argv, '--add-dir') == [str(relay / 'in')]
    for setting in ['sandbox_workspace_write.writable_roots=[]',
        'sandbox_workspace_write.network_access=false',
        'sandbox_workspace_write.exclude_slash_tmp=true',
        'sandbox_workspace_write.exclude_tmpdir_env_var=true', 'approval_policy="never"',
        'mcp_servers={}', 'web_search="disabled"', 'agents.enabled=false',
        'skills.include_instructions=false']:
        assert setting in option_values(argv, '-c')
    for key in ['TMPDIR', 'TMP', 'TEMP']:
        assert env.get(key) == str(d.B / 'tmp')
    assert env.get('XDG_CACHE_HOME') == str(d.B / 'tmp/cache')
    assert env.get('CODEX_HOME') == str(d.S / 'launch' / d.activation() / 'codex-home')
    assert env.get('YGG_RELAY') == str(relay)
    writable = option_values(argv, '--cd') + option_values(argv, '--add-dir')
    for path in writable:
        for forbidden in [d.H / '.git', d.safety, d.F, d.hooks]:
            assert path != str(forbidden) and not path.startswith(str(forbidden) + '/')
        if path == str(d.S) or path.startswith(str(d.S) + '/'):
            assert path == str(relay / 'in')
    record = d.json_file(d.registry) or {}
    for key, value in {'kind': 'lane', 'generation': 1, 'state': 'ready',
        'box': str(d.B), 'path': str(d.clone)}.items():
        assert record.get(key) == value
    d.launch('pm1tl1w1')
    worker = d.launch_record('pm1tl1w1')
    assert worker.get('cwd') == str(d.clone)
    assert option_values(worker['argv'], '--add-dir') == [str(d.S / 'relay' / d.activation('pm1tl1w1') / 'in')]
    assert not d.exists(d.clone / '.git/objects/info/alternates')
    assert d.git_result(d.clone, 'fsck', '--connectivity-only').returncode == 0
    assert not set(d.inode_pairs(d.clone / '.git/objects')) & set(d.inode_pairs(d.H / '.git/objects'))
    assert (d.json_file(d.registry) or {}).get('given_head') == d.lane_head()


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-03')
@pytest.mark.parametrize('role', ['pm', 'testing_su', 'idle_su'])
def test_pm_and_senior_user_work_areas(d, role):
    head = d.received(ready_for_test=True) if role == 'testing_su' else None
    node = 'pm1' if role == 'pm' else 'pm1su1'
    result = d.launch(node, steps=[{'wait': 'read-work-area'}], settle=False)
    try:
        record = d.launch_record(node)
        activation = d.activation(node)
        box = (d.H / '.ygg-worktrees/alpha-p1/test' / ('pm1su1.' + activation)
            if role == 'testing_su' else d.S / 'scratch' / activation)
        cwd = box / 'checkout' if role == 'testing_su' else box
        assert record.get('cwd') == str(cwd)
        assert option_values(record['argv'], '--cd') == [str(box)]
        assert option_values(record['argv'], '--add-dir') == [str(d.S / 'relay' / activation / 'in')]
        if role == 'testing_su':
            assert d.head(cwd) == head
            assert d.git_text(cwd, 'rev-parse', '--absolute-git-dir') == str(cwd / '.git')
            assert str(d.H / '.ygg-worktrees/alpha-p1/test') not in d.git_text(d.H, 'worktree', 'list', '--porcelain')
        elif role == 'pm':
            assert record['env'].get('TMPDIR') == str(box / 'tmp')
            for path in [box, d.S / 'relay' / activation / 'in']:
                for forbidden in [d.H, d.F, d.safety]:
                    assert path != forbidden and not path.is_relative_to(forbidden)
        else:
            assert result.returncode == 0
            assert not d.exists(d.H / '.ygg-worktrees/alpha-p1/test')
    finally:
        d.control('read-work-area', node)
        d.wait_exit(node)


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-04')
def test_relay_ack_is_live_and_archived(d):
    d.send()
    d.launch(steps=[{'id': 'ack', 'run': ['ygg', 'inbox', 'pm1tl1', '--ack', 'm-0011']},
        {'wait': 'go-1'}], settle=False)
    try:
        assert d.wait_step('ack').returncode == 0
        assert not d.receipt().get('exited_at')
        assert 'm-0011' in d.inbox('pm1tl1')
        assert not d.unread()
    finally:
        d.control('go-1')
        d.wait_exit('pm1tl1')
    responses = d.responses()
    assert len(responses) == 1
    assert responses[0].get('exit') == 0
    assert not d.exists(d.relay(kept=False))


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-04')
@pytest.mark.parametrize('raw', [False, True])
def test_relay_identity_is_not_overridden(d, raw):
    argv = ['send', '--from', 'pm1', '--to', 'pm1tl2', '--kind', 'note', '--body', 'x']
    steps = (d.plant(7, {'argv': argv, 'stdin': '', 'files': {}, 'env': {'YGG_NODE': 'pm1'}})
        if raw else [{'id': 'spoof', 'run': ['ygg', *argv]}])
    d.launch(steps=steps)
    if raw:
        response = d.response(7)
        assert response.get('exit') == 1
        assert 'relay: request 00000000000000000000000000000007 refused: unknown key env' in response.get('stderr', '')
    else:
        assert d.step('spoof').returncode == 3
        assert 'identity:' in d.step('spoof').stderr
    assert not any(r.get('from') == 'pm1' for r in d.messages('pm1tl2'))


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-05')
@pytest.mark.parametrize('other_refs', [False, True])
def test_ready_receives_only_own_lane(d, other_refs):
    d.authorised()
    refs = ['refs/heads/ygg/alpha-p1/lane/pm1tl2', 'refs/heads/main', INTEGRATION]
    before = {r: d.git_text(d.H, 'rev-parse', r) for r in refs}
    steps = d.commit_steps('wp1.txt', 'one', 'WP-1: add wp1.txt')
    if other_refs:
        steps += [{'run': command} for command in [
            ['git', 'branch', '-f', 'ygg/alpha-p1/lane/pm1tl2', 'HEAD'],
            ['git', 'update-ref', 'refs/heads/main', 'HEAD'],
            ['git', 'update-ref', INTEGRATION, 'HEAD'], ['git', 'tag', 'v9', 'HEAD']]]
    d.launch(steps=steps + [d.ready_step()])
    assert_received_head(d)
    if other_refs:
        assert {r: d.git_text(d.H, 'rev-parse', r) for r in refs} == before
        assert d.git_result(d.H, 'rev-parse', '--verify', 'refs/tags/v9').returncode != 0


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-06')
@pytest.mark.parametrize('kind,reason', [
    ('denylist', 'zebra-orchid-7'), ('trailer', 'trailers are not allowed by the profile'),
    ('workflow', 'CI workflow files are refused on lanes')])
def test_receive_rechecks_commits_that_skipped_hooks(d, kind, reason):
    d.authorised()
    d.made_lane()
    before = d.lane_head()
    name = '.github/workflows/ci.yml' if kind == 'workflow' else 'a.txt'
    text = 'zebra-orchid-7' if kind == 'denylist' else ('name: x' if kind == 'workflow' else 'clean')
    steps = d.commit_steps(name, text, 'x', bypass=True)
    if kind == 'trailer':
        steps[2]['run'] = ['git', '-c', 'core.hooksPath=/dev/null', 'commit', '--no-verify',
            '-m', 'x', '-m', 'Co-Authored-By: Claude <noreply@anthropic.com>']
    if kind == 'workflow':
        steps.insert(0, {'run': ['git', '-c', 'core.hooksPath=/dev/null', 'config', 'core.hooksPath', '/dev/null']})
    d.launch(steps=steps + [d.ready_step()])
    assert_bad_head_kept(d)
    result = d.step('ready')
    assert result.returncode == 3
    assert 're-check refused' in result.stderr and reason in result.stderr
    assert d.lane_head() == before
    if kind == 'denylist':
        assert d.wp().get('state') == 'authorised'
        assert d.reviews() == []


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-07')
@pytest.mark.parametrize('state', ['accepted', 'in-review'])
def test_lane_must_move_forward_and_wait_for_verdict(d, state):
    head_a = d.received(accepted=state == 'accepted')
    d.authorised('WP-2')
    steps = d.commit_steps('b.txt' if state == 'accepted' else 'c.txt', 'clean',
        'b' if state == 'accepted' else 'c', bypass=True)
    if state == 'accepted':
        steps.insert(0, {'run': ['git', '-c', 'core.hooksPath=/dev/null', 'reset', '--hard', 'HEAD~1']})
    d.launch(steps=steps + [d.ready_step('WP-2')])
    assert_bad_head_kept(d)
    result = d.step('ready')
    assert result.returncode == 3
    if state == 'accepted':
        assert 're-check refused: ygg/alpha-p1/lane/pm1tl1 can only move forward' in result.stderr
        assert head_a[:12] in result.stderr
        assert d.git_text(d.clone, 'rev-parse', 'HEAD^') != head_a
        assert d.wp('WP-2').get('state') == 'authorised'
    else:
        assert 're-check refused: WP-1 is being checked; the lane takes new commits after its verdict' in result.stderr
        assert d.review().get('head') == head_a
    assert d.lane_head() == head_a


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-08')
def test_unread_mail_prevents_receive_and_later_ticks(d):
    d.authorised()
    d.made_lane()
    before = d.lane_head()
    d.send()
    d.launch(steps=d.commit_steps(bypass=True) + [d.ready_step()])
    d.tick(2)
    assert d.step('ready').returncode == 3
    assert 'read your inbox first (1 unread): ygg inbox pm1tl1' in d.step('ready').stderr
    assert d.lane_head() == before
    assert d.lane_head() != d.head()
    assert d.wp().get('state') == 'authorised'
    assert d.reviews() == []


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-09')
def test_clone_config_is_never_executed_and_is_reset(d):
    d.authorised()
    d.recording_programs()
    d.launch(steps=d.tampered_config_steps())
    assert d.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1').returncode == 0
    assert d.lane_head() == d.head()
    assert not any(n.startswith('ran-') for n in d.names(d.T / 'rec'))
    # Row 3 provides the positive control for row 1's empty marker expectation.
    assert d.launch().returncode == 0
    config = d.clone / '.git/config'
    assert d.git_text(d.clone, 'config', '--file', config, '--get', 'core.hooksPath') == str(d.hooks)
    for key in ['core.fsmonitor', 'filter.x.clean', 'uploadpack.packObjectsHook', 'core.alternateRefsCommand']:
        assert d.git_text(d.clone, 'config', '--file', config, '--get', key) == ''
    assert not any(n.startswith('ran-') for n in d.names(d.T / 'rec'))


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-09')
def test_lane_sync_does_not_execute_clone_config(d):
    d.authorised()
    d.recording_programs()
    d.launch(steps=d.tampered_config_steps())
    d.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
    d.complete_package_review('WP-1')
    d.run('acceptance', 'record', '--from', 'pm1su1', '--package', 'WP-1', '--sha', d.lane_head(), '--verdict', 'accept')
    d.remove(d.clone / '.gitattributes')
    d.commit_file(d.H, 'm.txt', 'clean', 'main')
    result = d.sync()
    d.tick(2)
    assert result.returncode == 0
    assert not any(n.startswith('ran-') for n in d.names(d.T / 'rec'))
    # Same criterion's row 3 supplies a nonempty control on the next launch.
    assert d.launch().returncode == 0
    assert d.git_text(d.clone, 'config', '--file', d.clone / '.git/config', '--get', 'core.hooksPath') == str(d.hooks)


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-10')
def test_clone_operations_leave_home_git_and_hooks_intact(d):
    d.launch()
    d.authorised()
    before_config = d.digest(d.H / '.git/config')
    before_hooks = d.snapshot_files(d.H / '.git/hooks')
    before_safety = d.snapshot_files(d.hooks)
    before_refs = d.git_text(d.H, 'for-each-ref').splitlines()
    before_hook_path = d.git_text(d.H, 'config', '--get', 'core.hooksPath')
    steps = [{'run': ['git', 'config', 'core.hooksPath', '/dev/null']},
        {'write_file': {'path': '.git/hooks/pre-commit', 'text': 'exit 0'}},
        {'run': ['git', 'gc']}, {'run': ['git', 'pack-refs', '--all']}]
    commit_steps = d.commit_steps()
    commit_steps[2]['run'] += ['--author', 'Example Builder <builder@example.invalid>']
    d.launch(steps=steps + commit_steps + [d.ready_step()])
    assert d.step('ready').returncode == 0
    assert d.lane_head() == d.head()
    assert d.digest(d.H / '.git/config') == before_config
    after_hooks = d.snapshot_files(d.H / '.git/hooks')
    after_safety = d.snapshot_files(d.hooks)
    assert {k: after_hooks.get(k) for k in before_hooks} == before_hooks
    assert {k: after_safety.get(k) for k in before_safety} == before_safety
    after_refs = d.git_text(d.H, 'for-each-ref').splitlines()
    changed = set(before_refs) ^ set(after_refs)
    assert any(line.endswith('\t' + LANE) for line in changed)
    assert all(line.endswith('\t' + LANE) for line in changed)
    assert d.git_text(d.H, 'config', '--get', 'core.hooksPath') == before_hook_path


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-11')
@pytest.mark.parametrize('mode', ['launch', 'package', 'tick'])
def test_lane_runtime_cannot_change(factory, mode):
    d = factory(codex=False, launcher='codex' if mode == 'tick' else None)
    lane_name = 'ygg/alpha-p1/lane/pm1tl1'
    suffix = "; a lane's team lead and workers use one runtime"
    if mode == 'launch':
        d.launch(binary='claude')
        d.config(launcher_cli='codex')
        result = d.launch()
        assert result.returncode == 3
        assert f'pm1tl1 would run on Codex but lane {lane_name} runs on Claude Code{suffix}' in result.stderr
        assert launch_count(d, 'pm1tl1', 'codex') == 0
        worktree = d.lanes / 'pm1tl1'
        assert d.exists(worktree)
        assert str(worktree) in d.git_text(d.H, 'worktree', 'list')
    elif mode == 'package':
        d.package('WP-1', criteria=True)
        d.set_header(d.F / 'projects/alpha-p1/issues/WP-1.md', models={'model': 'gpt-6.1-sol'})
        result = d.run('wp', 'authorise', 'alpha-p1', '--package', 'WP-1', '--lane', 'pm1tl1')
        assert result.returncode == 3
        assert f'WP-1 would run on Codex but lane {lane_name} runs on Claude Code{suffix}' in result.stderr
        assert d.wp().get('state') == 'planned'
    else:
        d.launch()
        d.config(launcher_cli='claude-code')
        d.send(10)
        d.tick()
        assert launch_count(d, 'pm1tl1', 'claude') == 0
        alerts = d.alerts('pm1tl1')
        assert len(alerts) == 1
        assert f'pm1tl1 would run on Claude Code but lane {lane_name} runs on Codex{suffix}' in alerts[0]['text']
        assert d.exists(d.B)
        assert str(d.lanes) not in d.git_text(d.H, 'worktree', 'list', '--porcelain')


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-12')
@pytest.mark.parametrize('state', ['clean', 'running', 'dirty'])
def test_lane_sync_preserves_work_and_waits_when_busy(d, state):
    if state == 'clean':
        d.received()
        d.commit_file(d.H, 'm.txt', 'clean', 'main')
    else:
        d.made_lane()
    before = d.lane_head()
    if state == 'running':
        d.launch(steps=[{'wait': 'go-1'}], settle=False)
    elif state == 'dirty':
        # Use a tracked file, independent of the repository's initial file names.
        d.commit_file(d.clone, 'a.txt', 'clean', 'a')
        d.write(d.clone / 'a.txt', 'edited')
    snapshot = d.snapshot_files(d.clone) if state == 'dirty' else None
    try:
        result = d.sync()
        if state == 'clean':
            assert result.returncode == 0
            lane = d.lane_head()
            assert len(d.git_text(d.H, 'rev-list', '--parents', '-1', lane).split()[1:]) > 1
            assert d.git_result(d.H, 'merge-base', '--is-ancestor', 'main', lane).returncode == 0
            d.launch()
            assert d.head() == lane
            assert d.exists(d.clone / 'm.txt')
        else:
            assert result.returncode == 3
            reason = ('an activation of lane ygg/alpha-p1/lane/pm1tl1 is running'
                if state == 'running' else 'the clone of ygg/alpha-p1/lane/pm1tl1 has uncommitted changes')
            assert 'lane sync waits: ' + reason in result.stderr
            assert d.lane_head() == before
            if state == 'dirty':
                assert d.snapshot_files(d.clone) == snapshot
    finally:
        if state == 'running':
            d.control('go-1')
            d.wait_exit('pm1tl1')


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-13')
@pytest.mark.parametrize('extra', ['none', 'commit', 'untracked'])
def test_merged_lane_box_is_quarantined_with_all_work(factory, extra):
    d = factory(approved=False)
    head_w = d.merge_fixture()
    head_e = d.commit_file(d.clone, 'e.txt', 'clean', 'E') if extra == 'commit' else None
    if extra == 'untracked':
        d.write(d.clone / 'notes.txt', 'keep me')
    d.ticks_until(lambda: d.exists(d.QF1))
    if extra == 'none':
        assert d.B.name in d.names(d.lanes)
        assert d.is_dir(d.B) and d.is_dir(d.clone)
        assert not any(n.startswith('.creating-') for n in d.names(d.lanes))
        assert d.head() == head_w
        record = d.json_file(d.registry) or {}
        assert record.get('state') == 'closed' and record.get('generation') == 1
        assert str(d.B) in d.read(d.QF1)
        assert d.git_text(d.H, 'for-each-ref', 'refs/ygg/quarantine') == ''
        entry = d.quarantine_status(d.B)
        for text in ['quarantined', 'kept after lane ygg/alpha-p1/lane/pm1tl1 merged',
            'may hold work that was not received',
            'the report saw refs 0, stash 0, commits 0, changed 0, untracked 0, ignored 0',
            'metadata the report saw: none']:
            assert text in entry
        assert 'nothing' not in entry and 'live; do not remove' not in entry
        assert any(line.startswith('quarantined boxes: 1') for line in d.run('status').stdout.splitlines())
        assert d.git_text(d.H, 'for-each-ref', 'refs/ygg/kept') == ''
    elif extra == 'commit':
        assert d.lane_head() == head_w
        assert d.head() == head_e
        assert d.git_result(d.H, 'cat-file', '-e', head_e).returncode != 0
        entry = d.quarantine_status(d.B)
        for text in ['quarantined', 'may hold work that was not received', 'commits 1']:
            assert text in entry
        assert d.git_text(d.H, 'for-each-ref', 'refs/ygg/kept') == ''
    else:
        d.tick(2)
        assert d.read(d.clone / 'notes.txt') == 'keep me'
        assert d.B.name in d.names(d.lanes)
        assert d.names(d.QF) == [d.QF1.name]
        assert d.git_text(d.H, 'for-each-ref', 'refs/ygg/quarantine') == ''


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-14')
def test_receive_crash_recovers_without_duplicate_review(d):
    d.authorised()
    d.made_lane()
    before = d.lane_head()
    before_count = d.git_text(d.H, 'rev-list', '--count', LANE)
    head_d = d.commit_file(d.clone, 'd.txt', 'clean', 'D')
    first = d.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1',
        env={'YGG_FAULT_AT': 'boundary:receive-after-import'})
    assert first.returncode == 97
    assert d.lane_head() == before
    assert d.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1').returncode == 0
    assert d.lane_head() == head_d
    assert int(d.git_text(d.H, 'rev-list', '--count', LANE)) == int(before_count) + 1
    assert len(d.reviews()) == 1


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-14')
@pytest.mark.parametrize('point', ['clone-before-rename', 'clone-after-rename'])
def test_interrupted_clone_assembly_uses_next_generation(d, point):
    first = d.launch(env={'YGG_FAULT_AT': 'boundary:' + point})
    assert first.returncode == 97
    assert d.launch().returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    entries = d.names(d.lanes)
    if point == 'clone-before-rename':
        creating = [name for name in entries if name.startswith('.creating-')]
        assert len(entries) == 2 and len(creating) == 1
        assert 'pm1tl1.g000002' in entries and not d.exists(d.B)
        assert d.names(d.QF) == [d.QF1.name]
        interrupted = d.lanes / creating[0]
        record_text = d.read(d.QF1)
        assert str(interrupted) in record_text and 'assembly was interrupted' in record_text
        status = d.quarantine_status(interrupted)
        assert 'quarantined' in status and 'assembly was interrupted' in status
    else:
        assert entries == ['pm1tl1.g000001', 'pm1tl1.g000002']
        assert not any(n.startswith('.creating-') for n in entries)
        status = d.quarantine_status(d.B)
        assert 'quarantined' in status and 'assembly was interrupted' in status
        assert 'live; do not remove' in d.quarantine_status(d.B2)
    assert d.head(d.B2 / 'checkout') == d.lane_head()
    record = d.json_file(d.registry) or {}
    assert record.get('generation') == 2 and record.get('state') == 'ready'


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-15')
def test_claude_lanes_keep_worktrees_and_guard(factory):
    d = factory(codex=False)
    d.launch(binary='claude')
    record = d.launch_record()
    assert launch_count(d, 'pm1tl1', 'claude') == 1
    worktree = d.lanes / 'pm1tl1'
    assert record.get('cwd') == str(worktree)
    assert str(worktree) in d.git_text(d.H, 'worktree', 'list', '--porcelain')
    assert 'YGG_RELAY' not in record['env']
    assert record['env'].get('CODEX_HOME') == d.env.get('CODEX_HOME')
    assert not d.names(d.S / 'lane-clones/alpha-p1')
    d.write(worktree / 'a.txt', 'zebra-orchid-7')
    d.git_result(worktree, 'add', 'a.txt')
    before = d.lane_head()
    result = d.hook({'tool_name': 'Bash', 'tool_input': {'command': 'git commit --no-verify -m x'}},
        node='pm1tl1', cwd=worktree)
    assert result.returncode == 2
    assert d.lane_head() == before


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-17')
def test_malformed_and_linked_relay_requests_are_refused(d):
    value = {'argv': ['send', '--from', 'pm1tl1', '--to', 'pm1', '--kind', 'note', '--body', 'linked'], 'stdin': '', 'files': {}}
    source = d.T / 'req.json'
    d.write_json(source, value)
    before = d.read(source)
    steps = d.malformed_requests(source)
    d.launch(steps=steps)
    for number, reason in [(1, 'refused: not a regular file'),
        (2, 'refused: larger than 1 MiB'), (3, 'refused: not one JSON object')]:
        response = d.response(number)
        assert response.get('exit') == 1
        assert reason in response.get('stderr', '')
    assert d.read(source) == before
    assert not any(r.get('body') == 'linked' for r in d.messages())


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-17')
def test_reused_relay_request_is_not_replayed(d):
    d.send()
    d.relay_hold(2)
    first = {'argv': ['inbox', 'pm1tl1', '--ack', 'm-0011'], 'stdin': '', 'files': {}}
    second = {'argv': ['send', '--from', 'pm1tl1', '--to', 'pm1', '--kind', 'note', '--body', 'again'], 'stdin': '', 'files': {}}
    steps = d.plant(4, first)
    steps += [{'run': ['sh', '-c',
        'ygg send --from pm1tl1 --to pm1 --kind note --body slow >/dev/null 2>&1 &']}]
    steps += [d.poll_relay_entered(2)] + d.plant(4, second, poll=0)
    steps += [{'run': ['sh', '-c', 'kill -KILL 0']}]
    d.launch(steps=steps, settle=False)
    d.wait_relay_entered(2)
    d.wait_pending_and_group_dead(4)
    d.release_relay(2)
    d.wait_exit('pm1tl1')
    assert d.response(4).get('exit') == 0
    assert 'acknowledged m-0011' in d.response(4).get('stdout', '')
    assert 'm-0011' in d.inbox('pm1tl1') and not d.unread()
    messages = d.messages()
    assert not any(r.get('body') == 'again' for r in messages)
    assert sum(r.get('from') == 'pm1tl1' and r.get('body') == 'slow' for r in messages) == 1
    taken = d.names(d.relay() / 'taken')
    assert len(taken) == 2 and f'{4:032x}.json' in taken
    responses = d.responses()
    assert len(responses) == 2
    assert all(r.get('exit') == 0 for r in responses)
    refused = d.read(d.relay() / 'refused.jsonl').splitlines()
    assert len(refused) == 1
    assert f'{4:032x}' in refused[0] and 'id already used' in refused[0]


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-17')
def test_concurrent_relay_requests_each_run_once(d):
    d.launch(steps=[{'id': 'send-two', 'run': ['sh', '-c',
        'ygg send --from pm1tl1 --to pm1 --kind note --body one & ygg send --from pm1tl1 --to pm1 --kind note --body two & wait']}])
    assert d.step('send-two').returncode == 0
    messages = [r for r in d.messages() if r.get('from') == 'pm1tl1']
    for body in ['one', 'two']:
        assert sum(r.get('body') == body for r in messages) == 1
    responses = d.responses()
    assert len(responses) == 2
    assert all(r.get('exit') == 0 for r in responses)


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-18')
def test_relay_refuses_unavailable_managed_verbs(d):
    steps = []
    for number, argv in [(17, ['push-guard', str(d.B / 'checkout'), 'origin']),
        (18, ['lane-check', 'pre-commit']), (19, ['launch', 'pm1tl2'])]:
        steps += d.plant(number, {'argv': argv, 'stdin': '', 'files': {}})
    d.launch(steps=steps)
    for number in [17, 18, 19]:
        response = d.response(number)
        assert response.get('exit') == 1
        assert 'is not available to managed sessions' in response.get('stderr', '')
    assert d.launches('pm1tl2') == []


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-18')
def test_relay_repository_argument_must_name_home(d):
    d.made_lane()
    before = d.git_text(d.H, 'rev-parse', INTEGRATION)
    d.launch('pm1', steps=[{'id': 'merge', 'run': ['ygg', 'merge', 'lane', 'alpha-p1',
        '--lane', 'pm1tl1', '--repo', str(d.B / 'checkout')]}])
    result = d.step('merge', 'pm1')
    assert result.returncode == 3
    assert '--repo must name the home of programme alpha' in result.stderr
    assert d.git_text(d.H, 'rev-parse', INTEGRATION) == before


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-18')
def test_relay_carries_body_files_only_from_worktree(d):
    d.launch(steps=[{'write_file': {'path': 'note.txt', 'text': 'hello body'}},
        {'id': 'inside', 'run': ['ygg', 'send', '--from', 'pm1tl1', '--to', 'pm1', '--kind', 'note', '--body-file', 'note.txt']},
        {'id': 'outside', 'run': ['ygg', 'send', '--from', 'pm1tl1', '--to', 'pm1', '--kind', 'note', '--body-file', '/etc/hosts']}])
    assert d.step('inside').returncode == 0
    messages = [r for r in d.messages() if r.get('from') == 'pm1tl1']
    assert len(messages) == 1 and messages[0].get('body') == 'hello body'
    assert d.step('outside').returncode != 0
    assert 'body file must be inside your worktree' in d.step('outside').stderr


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-20')
def test_lane_sync_refuses_unreceived_forbidden_history(d):
    d.made_lane()
    before = d.lane_head()
    commit = d.commit_file(d.clone, 'z.txt', 'zebra-orchid-7', 'z')
    d.commit_file(d.H, 'm.txt', 'clean', 'main')
    result = d.sync()
    assert result.returncode == 3
    assert 're-check refused' in result.stderr and 'zebra-orchid-7' in result.stderr
    assert d.lane_head() == before
    assert d.head() == commit


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-20')
def test_launch_preserves_dirty_clone_after_home_lane_moves(d):
    d.received()
    clone_head = d.head()
    d.commit_file(d.H, 'm.txt', 'clean', 'main')
    d.sync()
    moved = d.lane_head()
    d.write(d.clone / 'a.txt', 'edited')
    changed_text = d.read(d.clone / 'a.txt')
    result = d.launch()
    assert result.returncode == 0
    assert d.head() == clone_head
    assert d.read(d.clone / 'a.txt') == changed_text
    assert 'The lane moved to' in d.prompt() and moved[:12] in d.prompt()


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-21')
def test_git_folder_links_are_refused_then_box_is_replaced(d):
    d.authorised()
    d.made_lane()
    victim = d.T / 'victim'
    for name in ['config', 'index', 'packed-refs']:
        d.write(victim / name, 'victim')
    d.mkdir(victim / 'refs')
    hashes = d.snapshot_files(victim)
    listing = d.tree_names(victim)
    refs = d.git_text(d.H, 'for-each-ref')
    for name, target in [('index', 'index'), ('config.worktree', 'config'),
        ('packed-refs', 'packed-refs'), ('refs/heads/ygg', 'refs')]:
        d.symlink(d.clone / '.git' / name, victim / target)
    ready = d.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
    sync = d.sync()
    for result in [ready, sync]:
        assert result.returncode == 3
        assert "the clone's git folder holds links" in result.stderr
    assert d.launch().returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    for name in ['index', 'packed-refs', 'refs/heads/ygg']:
        assert not d.is_link(d.B2 / 'checkout/.git' / name)
    assert d.is_link(d.B / 'checkout/.git/index')
    assert d.readlink(d.B / 'checkout/.git/index') == str(victim / 'index')
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status and 'odd:' in status
    assert d.snapshot_files(victim) == hashes and d.tree_names(victim) == listing
    assert d.git_text(d.H, 'for-each-ref') == refs


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-21')
def test_replaced_checkout_does_not_redirect_trusted_git(d):
    d.authorised()
    d.made_lane()
    hashes = {n: d.digest(d.H / '.git' / n) for n in ['config', 'index']}
    refs = d.git_text(d.H, 'for-each-ref')
    d.rename(d.clone, d.T / 'old-checkout')
    d.symlink(d.clone, d.H)
    result = d.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
    assert result.returncode == 3
    assert 're-check refused' in result.stderr and 'is not a folder Yggdrasil made' in result.stderr
    assert d.launch().returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.B.name in d.names(d.lanes)
    assert d.is_link(d.B / 'checkout') and d.readlink(d.B / 'checkout') == str(d.H)
    assert {n: d.digest(d.H / '.git' / n) for n in ['config', 'index']} == hashes
    assert d.git_text(d.H, 'for-each-ref') == refs


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-21')
def test_fifo_receive_times_out_without_blocking_status(d):
    d.authorised()
    d.made_lane()
    before = d.lane_head()
    d.fifo(d.clone / '.gitignore')
    started = d.clock()
    with d.background_ready() as process:
        d.pause(5)
        status, elapsed = d.elapsed(lambda: d.run('status', timeout=10))
        assert status.returncode == 0 and elapsed <= 10
        result = d.finish_process(process, timeout=85)
        assert d.clock() - started <= 90
        assert result.returncode == 3
        assert 're-check refused' in result.stderr and 'the clone could not be read in time' in result.stderr
    assert d.lane_head() == before


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-23')
@pytest.mark.parametrize('config', ['untrusted', 'provider'])
def test_codex_home_contains_only_permitted_config(d, config):
    for name in ['mcp', 'hook', 'notify']:
        d.recorder(name)
    source = d.T / 'codex-home'
    d.env['CODEX_HOME'] = str(source)
    d.write(source / 'auth.json', '{"x": 1}')
    if config == 'untrusted':
        for name in ['AGENTS.md', 'skills/s/SKILL.md', 'hooks.json']:
            d.write(source / name, 'untrusted')
        d.write(source / 'config.toml', d.untrusted_toml())
    else:
        d.write(source / 'config.toml', d.provider_toml())
    d.launch()
    home = d.S / 'launch' / d.activation() / 'codex-home'
    assert d.launch_record().get('env', {}).get('CODEX_HOME') == str(home)
    data = d.toml(home / 'config.toml')
    if config == 'untrusted':
        assert d.names(home) == ['config.toml']
        assert all(d.read_bytes(path) != d.read_bytes(source / 'auth.json')
            for path in d.files(home.parent))
        assert all(key not in data for key in ['mcp_servers', 'hooks', 'profiles', 'projects'])
        assert data.get('approval_policy') == 'never'
        assert data.get('agents', {}).get('enabled') is False
        assert data.get('skills', {}).get('include_instructions') is False
        for key in ['hooks', 'apps', 'plugins', 'remote_plugin', 'plugin_sharing',
        'browser_use', 'browser_use_external', 'computer_use', 'in_app_browser',
        'in_app_local_automation', 'multi_agent', 'skill_mcp_dependency_install',
        'image_generation', 'tool_suggest', 'system_proxy_fallback', 'respect_system_proxy']:
            assert data.get('features', {}).get(key) is False
        assert not any(n.startswith('ran-') for n in d.names(d.T / 'rec'))
    else:
        assert data.get('model_provider') == 'hub'
        assert data.get('model_providers', {}).get('hub') == {'name': 'hub',
            'base_url': 'http://127.0.0.1:18080/v1', 'env_key': 'OPENAI_API_KEY', 'wire_api': 'responses'}
        assert 'notify' not in data


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-24')
def test_codex_worker_requires_team_lead_parent(d):
    d.run('node', 'add', 'pm1su1w1', '--role', 'worker', '--parent', 'pm1su1')
    result = d.launch('pm1su1w1')
    assert result.returncode == 3
    assert "pm1su1w1 would run on Codex as a worker of pm1su1, which is not a team lead; Codex workers work only in a team lead's lane" in result.stderr
    assert launch_count(d, 'pm1su1w1', 'codex') == 0


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-24')
def test_codex_package_requires_team_lead_lane(d):
    d.package('WP-1', criteria=True)
    result = d.run('wp', 'authorise', 'alpha-p1', '--package', 'WP-1', '--lane', 'pm1su1')
    assert result.returncode == 3
    assert 'WP-1 would run on Codex on lane pm1su1, which is a senior user; Codex packages go only to team-lead lanes' in result.stderr
    assert d.wp().get('state') == 'planned'


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-24')
def test_test_clone_commits_never_reach_home(d, factory):
    head_c = d.received(ready_for_test=True)
    d.launch('pm1su1', steps=d.commit_steps('t.txt', '', 't'))
    activation = d.activation('pm1su1')
    d.tick(2)
    assert d.step('commit', 'pm1su1').returncode == 0
    assert d.lane_head() == head_c
    assert 't' not in d.git_text(d.H, 'log', '--all', '--format=%s').splitlines()
    assert not d.exists(d.H / '.ygg-worktrees/alpha-p1/test' / ('pm1su1.' + activation))
    # AC-24 row 2 is the control for the empty home-history and cleanup results.
    control = factory()
    control.package('WP-1', criteria=True)
    result = control.run('wp', 'authorise', 'alpha-p1', '--package', 'WP-1', '--lane', 'pm1su1')
    assert result.returncode == 3
    assert 'WP-1 would run on Codex on lane pm1su1, which is a senior user; Codex packages go only to team-lead lanes' in result.stderr
    assert control.wp().get('state') == 'planned'


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-27')
@pytest.mark.parametrize('version', ['codex-cli 0.171.0', 'codex-cli 0.170.0'])
def test_codex_builder_launch_requires_probed_version(d, version):
    result = d.launch(env={'YGG_TEST_CODEX_VERSION': version})
    if version == 'codex-cli 0.171.0':
        assert result.returncode == 3
        assert 'launch precondition: codex-cli 0.171.0 has not been probed with Yggdrasil; Codex builders run only on codex-cli 0.170.0' in result.stderr
        assert launch_count(d, 'pm1tl1', 'codex') == 0
        assert not d.exists(d.B)
    else:
        assert result.returncode == 0
        assert launch_count(d, 'pm1tl1', 'codex') == 1
        assert d.read(d.S / 'launch' / d.activation() / 'codex-version').strip() == 'codex version: codex-cli 0.170.0'


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-27')
def test_receive_helper_requires_probed_version(d):
    d.authorised()
    d.made_lane()
    before = d.lane_head()
    d.commit_file(d.clone, 'd.txt', 'clean', 'D')
    result = d.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1',
        env={'YGG_TEST_CODEX_VERSION': 'codex-cli 0.171.0'})
    assert result.returncode == 3
    assert 're-check refused: codex-cli 0.171.0 has not been probed with Yggdrasil' in result.stderr
    assert d.lane_head() == before
    assert d.wp().get('state') == 'authorised'


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-29')
def test_odd_clone_replacement_waits_for_worker_then_keeps_box(d):
    d.made_lane()
    d.launch('pm1tl1w1', steps=[{'wait': 'go-2'}], settle=False)
    victim = d.T / 'victim/packed-refs'
    d.write(victim, 'victim')
    d.symlink(d.clone / '.git/packed-refs', victim)
    before = d.digest(victim)
    count = launch_count(d, 'pm1tl1', 'codex')
    try:
        result = d.launch()
        assert result.returncode == 3
        assert 'launch precondition: the clone of lane ygg/alpha-p1/lane/pm1tl1 must be replaced, which waits until no activation of the lane runs' in result.stderr
        assert launch_count(d, 'pm1tl1', 'codex') == count
        assert d.exists(d.B) and not d.exists(d.B2) and d.names(d.QF) == []
    finally:
        d.control('go-2', 'pm1tl1w1')
        d.wait_exit('pm1tl1w1')
    assert d.launch().returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.is_link(d.B / 'checkout/.git/packed-refs')
    assert d.readlink(d.B / 'checkout/.git/packed-refs') == str(victim)
    assert d.digest(victim) == before
    prompt = d.prompt()
    assert 'Yggdrasil replaced your clone because odd:' in prompt and str(d.B) in prompt
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status and 'odd:' in status


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-29')
def test_odd_index_replacement_keeps_unreceived_commit(d):
    d.made_lane()
    head_d = d.commit_file(d.clone, 'd.txt', 'clean', 'D')
    d.fifo(d.clone / '.git/index')
    result, elapsed = d.elapsed(lambda: d.launch(timeout=60))
    assert result.returncode == 0 and elapsed <= 60
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.head(d.B2 / 'checkout') == d.lane_head()
    assert d.head(d.B2 / 'checkout') != head_d
    assert d.head(d.B / 'checkout') == head_d
    assert 'commits that ygg wp ready had not received are only there' in d.prompt()


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-19')
def test_stop_cancels_delayed_relay_without_acknowledging(d):
    d.send()
    d.relay_hold(1)
    d.launch(steps=[{'id': 'ack', 'run': ['ygg', 'inbox', 'pm1tl1', '--ack', 'm-0011']}],
        settle=False)
    d.wait_relay_entered(1)
    started = d.clock()
    d.run('stop', 'pm1tl1')
    d.wait_exit('pm1tl1', timeout=15)
    assert d.clock() - started <= 15
    responses = d.responses()
    assert len(responses) == 1
    assert responses[0].get('exit') == 75
    assert 'relay: the session was stopped while request' in responses[0].get('stderr', '')
    assert d.unread()


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-19')
def test_session_end_drains_running_call_and_refuses_late_request(d):
    d.relay_hold(1)
    late = {'argv': ['send', '--from', 'pm1tl1', '--to', 'pm1', '--kind', 'note',
        '--body', 'late'], 'stdin': '', 'files': {}}
    steps = [{'run': ['sh', '-c',
        'ygg send --from pm1tl1 --to pm1 --kind note --body first >/dev/null 2>&1 &']}]
    steps += [d.poll_relay_entered(1)] + d.plant(0x22, late, poll=0)
    steps += [{'run': ['sh', '-c', 'kill -KILL 0']}]
    d.launch(steps=steps, settle=False)
    d.wait_relay_entered(1)
    d.wait_pending_and_group_dead(0x22)
    d.release_relay(1)
    d.wait_exit('pm1tl1')
    messages = d.messages()
    assert sum(r.get('from') == 'pm1tl1' and r.get('body') == 'first' for r in messages) == 1
    assert not any(r.get('body') == 'late' for r in messages)
    response = d.response(0x22)
    assert response.get('exit') == 75
    assert 'relay: the session ended before request 00000000000000000000000000000022 ran' in response.get('stderr', '')


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-19')
def test_supervisor_crash_reports_unknown_outcome_without_replay(d):
    started = d.clock()
    d.launch(steps=[{'id': 'once', 'run': ['ygg', 'send', '--from', 'pm1tl1', '--to',
        'pm1', '--kind', 'note', '--body', 'once']}], settle=False,
        env={'YGG_SUPERVISOR_FAULT_AT': 'boundary:relay-after-run'})
    result = d.wait_step('once', timeout=30)
    assert d.clock() - started <= 30
    assert result.returncode == 75
    assert 'the relay stopped answering; the outcome of this call is unknown' in result.stderr
    d.tick(2)
    d.wait_exit('pm1tl1')
    assert sum(r.get('from') == 'pm1tl1' and r.get('body') == 'once' for r in d.messages()) == 1
    assert len(d.names(d.relay() / 'taken')) == 1
    assert d.names(d.relay() / 'out') == []


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-22')
def test_cancel_quarantines_unreceived_work_and_rebuilds_at_drop_point(d):
    head_a, head_r, head_e = d.cancel_fixture()
    result = d.run('wp', 'cancel', 'alpha-p1', '--package', 'WP-2')
    d.launch()
    assert result.returncode == 0
    assert d.lane_head() == head_a
    assert d.head(d.B / 'checkout') == head_e
    assert d.git_text(d.B / 'checkout', 'rev-parse', 'HEAD^') == head_r
    assert d.git_text(d.H, 'for-each-ref', 'refs/ygg/quarantine') == ''
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status
    assert str(d.B) in status and 'kept when WP-2 was cancelled' in status
    assert 'may hold work that was not received' in status
    assert d.git_text(d.H, 'for-each-ref', 'refs/ygg/kept') == ''
    assert (d.json_file(d.registry) or {}).get('generation') == 2
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.head(d.B2 / 'checkout') == head_a
    assert 'Yggdrasil replaced your clone because' in d.prompt() and str(d.B) in d.prompt()
    assert 'live; do not remove' in d.quarantine_status(d.B2)
    assert d.B.name in d.names(d.lanes)


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-22')
def test_cancel_preserves_uncommitted_tracked_change(d):
    head_a, _, head_e = d.cancel_fixture()
    d.write(d.clone / 'a.txt', 'edited')
    result = d.run('wp', 'cancel', 'alpha-p1', '--package', 'WP-2')
    assert result.returncode == 0
    assert d.lane_head() == head_a
    assert d.wp('WP-2').get('state') == 'cancelled'
    assert d.read(d.B / 'checkout/a.txt') == 'edited'
    assert d.head(d.B / 'checkout') == head_e


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-22')
def test_finish_interrupted_cancel_preserves_untracked_work(d):
    head_a, head_r, head_e = d.cancel_fixture()
    d.write(d.clone / 'wip.txt', 'draft')
    d.run('wp', 'cancel', 'alpha-p1', '--package', 'WP-2',
        env={'YGG_FAULT_AT': 'boundary:cancel-after-drop-record'})
    result = d.finish_cancel()
    assert result.returncode == 0
    assert d.lane_head() == head_a
    assert d.read(d.B / 'checkout/wip.txt') == 'draft'
    assert d.head(d.B / 'checkout') == head_e
    assert d.git_text(d.H, 'for-each-ref', 'refs/ygg/quarantine') == ''
    assert (d.json_file(d.registry) or {}).get('generation') == 2
    assert d.git_text(d.H, 'for-each-ref', 'refs/ygg/kept') == ''


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-26')
@pytest.mark.parametrize('name', ['.lfsconfig', '.LfsConfig'])
def test_receive_refuses_case_folded_lfsconfig_without_running_lfs(d, name):
    for key, value in [('filter.lfs.clean', 'git-lfs clean -- %f'),
        ('filter.lfs.smudge', 'git-lfs smudge -- %f'),
        ('filter.lfs.process', 'git-lfs filter-process'), ('filter.lfs.required', 'true')]:
        d.git_result(d.H, 'config', key, value)
    d.executable(d.T / 'bin/git-lfs', 'touch ' + str(d.T / 'rec/ran-git-lfs') + '\nexit 0\n')
    d.mkdir(d.T / 'rec')
    d.authorised()
    d.made_lane()
    before = d.lane_head()
    d.launch(steps=d.commit_steps(name, '[lfs]\nurl = https://example.invalid/lfs', 'l',
        bypass=True) + [d.ready_step()])
    assert_bad_head_kept(d)
    result = d.step('ready')
    assert result.returncode == 3
    assert 're-check refused' in result.stderr
    assert '.lfsconfig files are refused on Codex lanes' in result.stderr
    assert d.lane_head() == before
    assert not d.exists(d.T / 'rec/ran-git-lfs')


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-30')
def test_relay_merge_check_uses_trusted_temporary_environment(factory):
    d = factory(approved=False)
    d.merge_fixture(setup=d.merge_setup_recorder(), close=False, packages=1)
    d.launch('pm1', steps=[{'write_file': {'path': '{env:TMPDIR}/planted.txt', 'text': 'planted'}},
        d.cache_listing_step('before'), {'id': 'check', 'run': ['ygg', 'merge', 'check',
        'alpha-p1', '--repo', str(d.H)]}, d.cache_listing_step('after')])
    assert d.step('check', 'pm1').returncode == 0
    responses = d.responses('pm1')
    assert any(response.get('exit') == 0 for response in responses)
    lines = d.read(d.T / 'rec/setup-env').splitlines()
    assert len(lines) == 4
    prefix = str(d.S / 'relay' / d.activation('pm1') / 'tmp') + '/'
    assert lines[0].startswith(prefix)
    assert all(not line or line.startswith(lines[0].rstrip('/') + '/') or line == lines[0]
        for line in lines[1:])
    for line in lines:
        assert not line.startswith(str(d.S / 'scratch' / d.activation('pm1')))
        assert not line.startswith(str(d.H / '.ygg-worktrees'))
    assert 'planted.txt' not in d.read(d.T / 'rec/setup-tmp').splitlines()
    assert d.read(d.T / 'rec/after') == d.read(d.T / 'rec/before')
    assert d.names(d.relay('pm1', kept=False) / 'tmp') == []
    assert not d.exists(d.relay('pm1', kept=False))


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-31')
def test_reopened_lane_uses_next_generation_and_preserves_quarantine(factory):
    d = factory(approved=False)
    d.merge_fixture()
    d.ticks_until(lambda: d.exists(d.QF1))
    head = d.head(d.B / 'checkout')
    record = d.read_bytes(d.quarantine_record_path())
    reopened = d.run('wp', 'reopen', 'alpha-p1', '--package', 'WP-1', env={'YGG_NODE': 'pm1'})
    launched = d.launch()
    assert reopened.returncode == 0 and launched.returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    current = d.json_file(d.registry) or {}
    assert current.get('state') == 'ready' and current.get('generation') == 2
    assert d.B.name in d.names(d.lanes)
    assert d.head(d.B / 'checkout') == head
    assert d.read_bytes(d.quarantine_record_path()) == record
    assert d.git_text(d.H, 'for-each-ref', 'refs/ygg/quarantine') == ''


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-31')
def test_merged_lane_keeps_box_while_second_stage_waits(d):
    d.merge_fixture(last=False, close=False, packages=1)
    d.tick(2)
    assert d.exists(d.B)
    assert d.names(d.QF) == []
    record = d.json_file(d.registry) or {}
    assert record.get('state') == 'ready' and record.get('generation') == 1
    assert d.git_text(d.H, 'for-each-ref', 'refs/ygg/quarantine') == ''


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-32')
@pytest.mark.parametrize('boundary', ['quarantine-before-metadata', 'quarantine-after-metadata'])
def test_cleanup_quarantine_recovers_both_metadata_boundaries(factory, boundary):
    d = factory(approved=False)
    d.merge_fixture()
    faulted = d.run('conductor', 'tick', env={'YGG_FAULT_AT': 'boundary:' + boundary})
    assert faulted.returncode == 97
    assert (d.json_file(d.registry) or {}).get('state') == 'quarantining'
    reason = 'kept after lane ygg/alpha-p1/lane/pm1tl1 merged'
    if boundary == 'quarantine-before-metadata':
        assert not d.exists(d.QF1)
        d.ticks_until(lambda: d.exists(d.QF1))
        assert d.names(d.lanes) == [d.B.name]
    else:
        assert d.exists(d.QF1) and reason in d.read(d.QF1)
        d.tick(2)
        assert d.B.name in d.names(d.lanes)
    assert d.names(d.QF) == [d.QF1.name]
    assert reason in d.read(d.QF1)
    assert d.git_text(d.H, 'for-each-ref', 'refs/ygg/quarantine') == ''
    record = d.json_file(d.registry) or {}
    assert record.get('state') == 'closed' and record.get('generation') == 1


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-33')
def test_linked_tmp_is_quarantined_without_writing_to_target(d):
    d.made_lane()
    victim = d.T / 'victim-tmp'
    d.mkdir(victim)
    before = d.names(victim)
    d.rename(d.B / 'tmp', d.T / 'old-tmp')
    d.symlink(d.B / 'tmp', victim)
    result = d.launch()
    assert result.returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.names(victim) == before == []
    assert d.is_link(d.B / 'tmp') and d.readlink(d.B / 'tmp') == str(victim)
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status and 'odd:' in status


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-34')
def test_cancelled_box_owns_dropped_history_through_home_gc(d):
    head_a, head_r, head_e = d.cancel_fixture()
    result = d.run('wp', 'cancel', 'alpha-p1', '--package', 'WP-2')
    d.git_result(d.H, 'reflog', 'expire', '--expire-unreachable=now', '--all')
    d.git_result(d.H, 'gc', '--prune=now', '--quiet')
    assert result.returncode == 0
    assert not d.exists(d.B / 'checkout/.git/objects/info/alternates')
    assert d.git_text(d.H, 'for-each-ref', 'refs/ygg') == ''
    assert d.git_result(d.B / 'checkout', 'cat-file', '-e', head_r).returncode == 0
    assert d.git_result(d.B / 'checkout', 'fsck', '--connectivity-only').returncode == 0
    history = d.git_text(d.B / 'checkout', 'rev-list', head_e).splitlines()
    assert all(head in history for head in [head_e, head_r, head_a])


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-35')
@pytest.mark.parametrize('boundary', ['quarantine-after-metadata', 'quarantine-before-metadata'])
def test_interrupted_cancel_waits_for_finish_then_rebuilds_at_drop_point(d, boundary):
    head_a, head_r, head_e = d.cancel_fixture()
    result = d.run('wp', 'cancel', 'alpha-p1', '--package', 'WP-2',
        env={'YGG_FAULT_AT': 'boundary:' + boundary})
    assert result.returncode == 97
    d.tick()
    assert d.lane_head() == head_r
    if boundary == 'quarantine-after-metadata':
        assert d.exists(d.QF1)
        assert 'pm1tl1.g000002' not in d.names(d.lanes)
    else:
        assert d.B.name in d.names(d.lanes)
        assert d.names(d.QF) == []
    assert d.finish_cancel().returncode == 0
    assert d.lane_head() == head_a
    assert d.names(d.QF) == [d.QF1.name]
    assert d.B.name in d.names(d.lanes) and d.head() == head_e
    assert d.git_text(d.H, 'for-each-ref', 'refs/ygg/quarantine') == ''
    d.launch()
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.head(d.B2 / 'checkout') == head_a
    record = d.json_file(d.registry) or {}
    assert record.get('state') == 'ready' and record.get('generation') == 2


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-36')
def test_cancel_waits_for_running_lane_activation(d):
    _, head_r, _ = d.cancel_fixture()
    d.launch(steps=[{'wait': 'go-3'}], settle=False)
    before = d.snapshot_files(d.B)
    try:
        result = d.run('wp', 'cancel', 'alpha-p1', '--package', 'WP-2')
    finally:
        d.control('go-3')
        d.wait_exit('pm1tl1')
    assert result.returncode == 3
    assert 'the lane still has a running agent (pm1tl1); stop it first' in result.stderr
    assert d.snapshot_files(d.B) == before
    assert not d.exists(d.B2) and d.names(d.QF) == []
    assert d.lane_head() == head_r
    assert d.wp('WP-2').get('state') == 'authorised'


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-36')
def test_conductor_refuses_odd_clone_replacement_while_worker_runs(d):
    d.odd_clone_with_waiting_worker()
    before = launch_count(d, 'pm1tl1', 'codex')
    try:
        d.send(10)
        d.tick()
        assert launch_count(d, 'pm1tl1', 'codex') == before
        alerts = d.alerts('pm1tl1')
        assert len(alerts) == 1
        assert 'must be replaced, which waits until no activation of the lane runs' in alerts[0]['text']
        assert d.exists(d.B) and not d.exists(d.B2) and d.names(d.QF) == []
    finally:
        d.control('go-2', 'pm1tl1w1')
        d.wait_exit('pm1tl1w1')


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-37')
def test_conductor_launch_refuses_unprobed_version(d):
    d.send(10)
    d.run('conductor', 'tick', env={'YGG_TEST_CODEX_VERSION': 'codex-cli 0.171.0'})
    assert launch_count(d, 'pm1tl1', 'codex') == 0
    alerts = d.alerts('pm1tl1')
    assert len(alerts) == 1
    assert 'launch precondition: codex-cli 0.171.0 has not been probed with Yggdrasil; Codex builders run only on codex-cli 0.170.0' in alerts[0]['text']
    assert not d.exists(d.B)


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-37')
def test_pm_launch_refuses_unprobed_version(d):
    result = d.launch('pm1', env={'YGG_TEST_CODEX_VERSION': 'codex-cli 0.171.0'})
    assert result.returncode == 3
    assert 'launch precondition: codex-cli 0.171.0 has not been probed with Yggdrasil' in result.stderr
    assert launch_count(d, 'pm1', 'codex') == 0


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-37')
def test_lane_sync_helper_refuses_unprobed_version(d):
    d.received()
    d.commit_file(d.H, 'm.txt', 'clean', 'main')
    before = d.lane_head()
    result = d.run('lane', 'sync', 'alpha-p1', '--lane', 'pm1tl1',
        env={'YGG_TEST_CODEX_VERSION': 'codex-cli 0.171.0'})
    assert result.returncode == 3
    assert 're-check refused: codex-cli 0.171.0 has not been probed with Yggdrasil' in result.stderr
    assert d.lane_head() == before


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-38')
def test_version_pin_requires_exact_line(d):
    for version, text in [
        ('codex-cli 0.170.0-alpha.1', 'codex-cli 0.170.0-alpha.1'),
        ('codex-cli 0.170.01', 'codex-cli 0.170.01'),
        ('codex-cli 0.170.0\nextra', 'an unknown Codex version'),
        ('garbage', 'an unknown Codex version')]:
        result = d.launch(env={'YGG_TEST_CODEX_VERSION': version})
        assert result.returncode == 3
        assert 'launch precondition: ' + text + ' has not been probed with Yggdrasil' in result.stderr
    assert launch_count(d, 'pm1tl1', 'codex') == 0
    assert not d.exists(d.B)


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-38')
def test_version_pin_refuses_unprobed_platform(d):
    result = d.launch(env={'YGG_TEST_PLATFORM': 'linux'})
    assert result.returncode == 3
    assert 'launch precondition: Codex builders have not been probed on linux; they run only on darwin with codex-cli 0.170.0' in result.stderr
    assert launch_count(d, 'pm1tl1', 'codex') == 0
    assert not d.exists(d.B)


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-39')
def test_reviewer_records_unprobed_version_and_gets_hermetic_home(d):
    d.authorised()
    d.launch(steps=d.commit_steps('a.txt', 'clean', 'a') + [d.ready_step()])
    d.env['YGG_TEST_CODEX_VERSION'] = 'codex-cli 0.171.0'
    d.ticks_until(lambda: launch_count(d, 'pm1rv1', 'codex') > 0)
    assert launch_count(d, 'pm1rv1', 'codex') == 1
    assert d.alerts('pm1rv1') == []
    activation = d.activation('pm1rv1')
    home = d.S / 'launch' / activation / 'codex-home'
    assert d.launch_record('pm1rv1').get('env', {}).get('CODEX_HOME') == str(home)
    assert d.is_dir(home)
    assert 'config.toml' in d.names(home)
    assert {str(path.relative_to(home)) for path in d.files(home)} <= {'config.toml', 'auth.json'}
    version = d.read(d.S / 'launch' / activation / 'codex-version')
    assert 'codex-cli 0.171.0' in version and '(not probed)' in version


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-40')
def test_quarantine_report_counts_stash(factory):
    d = factory(approved=False)
    d.merge_fixture()
    tracked = d.git_text(d.clone, 'ls-files').splitlines()
    if tracked:
        d.write(d.clone / tracked[0], 'changed for stash')
    d.git_result(d.clone, 'stash')
    d.ticks_until(lambda: d.exists(d.QF1))
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status
    assert str(d.B) in status and 'may hold work that was not received' in status
    assert 'the report saw refs 0, stash 1' in status
    assert len(d.git_text(d.B / 'checkout', 'stash', 'list').splitlines()) == 1


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-40')
def test_quarantine_report_counts_side_branch(factory):
    d = factory(approved=False)
    head_w = d.merge_fixture()
    d.git_result(d.clone, 'switch', '-c', 'side')
    side = d.commit_file(d.clone, 's.txt', 'side', 'side')
    d.git_result(d.clone, 'switch', 'ygg/alpha-p1/lane/pm1tl1')
    d.ticks_until(lambda: d.exists(d.QF1))
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status
    assert str(d.B) in status and 'may hold work that was not received' in status
    assert 'the report saw refs 1' in status and 'commits 1' in status
    assert d.git_text(d.B / 'checkout', 'rev-parse', 'refs/heads/side') == side


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-40')
def test_quarantine_report_counts_ignored_file(factory):
    d = factory(approved=False)
    d.merge_fixture()
    d.mkdir(d.clone / '.git/info')
    d.write(d.clone / '.git/info/exclude', 'build/\n')
    d.write(d.clone / 'build/out.bin', 'x')
    d.ticks_until(lambda: d.exists(d.QF1))
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status
    assert str(d.B) in status and 'may hold work that was not received' in status
    assert 'untracked 0' in status and 'ignored 1' in status
    assert d.read(d.B / 'checkout/build/out.bin') == 'x'


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-41')
def test_taken_duplicate_cannot_claim_call_was_not_run(d):
    value = {'argv': ['send', '--from', 'pm1tl1', '--to', 'pm1', '--kind', 'note',
        '--body', 'once'], 'stdin': '', 'files': {}}
    started = d.clock()
    d.launch(steps=[d.duplicate_taken_request_step(value)], settle=False,
        env={'YGG_TEST_RELAY_DELAY': '3',
             'YGG_SUPERVISOR_FAULT_AT': 'boundary:relay-after-run'})
    result = d.wait_step('once', timeout=30)
    assert d.clock() - started <= 30
    assert result.returncode == 75
    assert 'the relay stopped answering; the outcome of this call is unknown' in result.stderr
    assert 'it was not run' not in result.stderr
    d.tick(2)
    d.wait_exit('pm1tl1')
    assert sum(r.get('from') == 'pm1tl1' and r.get('body') == 'once'
        for r in d.messages()) == 1
    assert len(d.names(d.relay() / 'taken')) == 1
    assert d.names(d.relay() / 'withdrawn') == []


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-41')
def test_stop_refuses_pending_request_without_withdrawal(d):
    d.relay_hold(1)
    late = {'argv': ['send', '--from', 'pm1tl1', '--to', 'pm1', '--kind', 'note',
        '--body', 'late'], 'stdin': '', 'files': {}}
    steps = [{'run': ['sh', '-c',
        'ygg send --from pm1tl1 --to pm1 --kind note --body first >/dev/null 2>&1 &']}]
    steps += [d.poll_relay_entered(1)] + d.plant(0x23, late, poll=0)
    steps += [{'wait': 'go-4'}]
    d.launch(steps=steps, settle=False)
    d.wait_relay_entered(1)
    d.until(lambda: d.exists(d.relay(kept=False) / 'in' / f'{0x23:032x}.json'))
    d.run('stop', 'pm1tl1')
    d.wait_exit('pm1tl1')
    response = d.response(0x23)
    assert response.get('exit') == 75
    assert 'relay: the session is stopping; request 00000000000000000000000000000023 was not run' in response.get('stderr', '')
    responses = d.responses()
    assert len(responses) == 2
    others = [r for r in responses if r != response]
    assert len(others) == 1 and others[0].get('exit') == 75
    assert 'relay: the session was stopped while request' in others[0].get('stderr', '')
    assert not any(r.get('from') == 'pm1tl1' for r in d.messages())
    assert d.names(d.relay() / 'withdrawn') == []


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-42')
def test_quarantined_box_owns_history_with_home_git_removed(d):
    head_a, head_r, head_e = d.cancel_fixture()
    result = d.run('wp', 'cancel', 'alpha-p1', '--package', 'WP-2')
    d.git_result(d.H, 'reflog', 'expire', '--expire-unreachable=now', '--all')
    d.git_result(d.H, 'gc', '--prune=now', '--quiet')
    assert result.returncode == 0
    assert d.git_result(d.H, 'cat-file', '-e', head_r).returncode != 0
    assert d.git_text(d.H, 'for-each-ref', 'refs/ygg') == ''
    assert not d.exists(d.B / 'checkout/.git/objects/info/alternates')
    away = d.T / 'H-git-away'
    checkout = d.B / 'checkout'
    d.rename(d.H / '.git', away)
    try:
        assert d.git_result(checkout, 'fsck', '--connectivity-only').returncode == 0
        assert d.git_result(checkout, 'cat-file', '-e', head_r).returncode == 0
        history = d.git_text(checkout, 'rev-list', head_e).splitlines()
        assert all(head in history for head in [head_e, head_r, head_a])
        assert d.git_text(checkout, 'cat-file', '-p', head_e + ':r.txt') == 'r'
    finally:
        d.rename(away, d.H / '.git')
    assert 'self_contained' not in (d.json_file(d.quarantine_record_path()) or {})


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-43')
def test_quarantine_report_counts_stash_ref_with_empty_reflog(factory):
    d = factory(approved=False)
    d.merge_fixture()
    tracked = d.git_text(d.clone, 'ls-files').splitlines()
    if tracked:
        d.write(d.clone / tracked[0], 'changed for stash')
    d.git_result(d.clone, 'stash')
    d.git_result(d.clone, 'reflog', 'delete', '--rewrite', 'refs/stash@{0}')
    d.ticks_until(lambda: d.exists(d.QF1))
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status
    assert 'may hold work that was not received' in status and 'stash 1' in status
    assert d.git_result(d.B / 'checkout', 'rev-parse', '--verify', 'refs/stash').returncode == 0


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-43')
def test_quarantine_report_counts_linked_worktree_commit(factory):
    d = factory(approved=False)
    d.merge_fixture()
    d.git_result(d.clone, 'worktree', 'add', '--detach', '../wt', 'HEAD')
    head_w = d.commit_file(d.B / 'wt', 'w.txt', 'w', 'w')
    d.ticks_until(lambda: d.exists(d.QF1))
    assert d.B.name in d.names(d.lanes)
    status = d.quarantine_status(d.B)
    for text in ['quarantined', 'may hold work that was not received',
        'report did not finish', 'outside dependencies the walk saw:']:
        assert text in status
    assert 'worktrees/wt' in status.partition('outside dependencies the walk saw:')[2]
    assert d.read(d.B / 'wt/w.txt') == 'w'
    assert d.head(d.B / 'wt') == head_w
    assert d.git_result(d.clone, 'cat-file', '-e', head_w).returncode == 0
    worktrees = d.git_text(d.clone, 'worktree', 'list', '--porcelain')
    assert str(d.B / 'wt') in worktrees and 'prunable' not in worktrees


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-44')
def test_relative_alternates_quarantine_keeps_dependency_resolving(d):
    head_x, recorded_hash = d.alternate_dependency(relative=True)
    assert d.launch().returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.B.name in d.names(d.lanes)
    assert d.digest(d.clone / '.git/objects/info/alternates') == recorded_hash
    assert d.git_text(d.clone, 'rev-parse', 'refs/heads/dep') == head_x
    assert d.git_text(d.clone, 'cat-file', '-p', head_x + ':x.txt') == 'x'
    record = d.json_file(d.registry) or {}
    assert record.get('state') == 'ready' and record.get('generation') == 2
    assert 'hold' not in record
    assert (d.json_file(d.QF1) or {}).get('odd_check') == '3a'
    assert not any(row.get('kind') == 'ygg-box-held'
        for row in (d.command_json('alerts', '--json') or []))
    status = d.run('status').stdout
    live = d.quarantine_status(d.B2)
    old = d.quarantine_status(d.B)
    assert 'live; do not remove' in live
    assert live and old and status.index(live) < status.index(old)
    for text in ['quarantined', 'odd:', 'outside dependencies the walk saw:']:
        assert text in old
    assert str(d.T / 'other/.git/objects') in old.partition('outside dependencies the walk saw:')[2]


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-44')
def test_merged_box_with_alternates_is_quarantined_untouched(factory):
    d = factory(approved=False)
    d.merge_fixture()
    other = d.T / 'other-objects'
    d.mkdir(other)
    alternates = d.clone / '.git/objects/info/alternates'
    d.write(alternates, str(other))
    d.ticks_until(lambda: d.exists(d.QF1))
    assert d.B.name in d.names(d.lanes)
    assert d.read(alternates) == str(other)
    record = d.json_file(d.registry) or {}
    assert record.get('state') == 'closed' and record.get('generation') == 1
    assert 'kept after lane ygg/alpha-p1/lane/pm1tl1 merged' in d.read(d.QF1)
    status = d.quarantine_status(d.B)
    for text in ['quarantined', 'may hold work that was not received',
        'report did not finish', 'outside dependencies the walk saw:']:
        assert text in status
    assert str(other) in status.partition('outside dependencies the walk saw:')[2]
    assert not any(row.get('kind') == 'ygg-box-held'
        for row in (d.command_json('alerts', '--json') or []))


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-44')
def test_linked_object_pack_refuses_receive_then_keeps_dependency(d):
    d.made_lane()
    d.authorised()
    head_d = d.commit_file(d.clone, 'd.txt', 'clean', 'D')
    target = d.move_pack_outside_box()
    before = d.lane_head()
    ready = d.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
    assert ready.returncode == 3
    assert 're-check refused: the clone depends on something outside it' in ready.stderr
    assert 'objects/pack' in ready.stderr
    assert d.lane_head() == before and d.wp().get('state') == 'authorised'
    assert d.launch().returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.head(d.B2 / 'checkout') == d.lane_head()
    pack = d.clone / '.git/objects/pack'
    assert d.is_link(pack) and d.readlink(pack) == target
    assert d.git_result(d.clone, 'cat-file', '-e', head_d).returncode == 0
    assert d.git_result(d.clone, 'fsck', '--connectivity-only').returncode == 0
    assert (d.json_file(d.QF1) or {}).get('odd_check') == '3a'
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status and 'outside dependencies the walk saw:' in status
    assert str(d.T / 'packs') in status.partition('outside dependencies the walk saw:')[2]


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-45')
def test_fourth_odd_clone_replacement_is_refused(d):
    box = d.three_odd_replacements()
    before = launch_count(d, 'pm1tl1', 'codex')
    result = d.launch()
    assert result.returncode == 3
    assert 'launch precondition: lane ygg/alpha-p1/lane/pm1tl1 replaced its clone 3 times in the last 24 hours' in result.stderr
    assert 'moved out of the lane folder' in result.stderr
    assert launch_count(d, 'pm1tl1', 'codex') == before
    assert d.exists(box) and d.is_link(box / 'checkout/.git/packed-refs')
    assert len(d.names(d.QF)) == 3
    assert 'pm1tl1.g000005' not in d.names(d.lanes)


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-45')
def test_removing_kept_box_allows_next_odd_clone_generation(d):
    d.three_odd_replacements()
    d.launch()
    d.remove_tree(d.B)
    result = d.launch()
    assert result.returncode == 0
    new = d.lanes / 'pm1tl1.g000005'
    assert d.launch_record().get('cwd') == str(new / 'checkout')
    kept = d.lanes / 'pm1tl1.g000004'
    assert d.exists(kept) and d.is_link(kept / 'checkout/.git/packed-refs')
    old_status = d.quarantine_status(kept)
    assert 'quarantined' in old_status and 'odd:' in old_status
    assert 'live; do not remove' in d.quarantine_status(new)


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-46')
def test_lane_sync_feeds_objects_before_any_launch(d):
    head_l0, main = d.feed_fixture()
    result = d.feed_sync()
    head_m = d.lane_head()
    assert result.returncode == 0
    assert len(d.git_text(d.H, 'rev-list', '--parents', '-1', head_m).split()[1:]) > 1
    assert d.git_result(d.H, 'merge-base', '--is-ancestor', main, head_m).returncode == 0
    assert d.git_result(d.clone, 'cat-file', '-e', head_m).returncode == 0
    assert d.git_text(d.clone, 'rev-parse', 'refs/ygg/given') == head_m
    assert d.head() == head_l0
    assert not d.exists(d.clone / 'm.txt')
    record = d.json_file(d.registry) or {}
    assert record.get('given_head') == head_m and record.get('given_ref') == head_m
    assert 'feeding' in record and record['feeding'] is None
    assert not d.exists(d.clone / '.git/objects/info/alternates')
    assert d.files(d.S / 'feed/alpha-p1') == []


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-46')
def test_failed_feed_keeps_given_refs_and_removes_feed_file(d):
    head_l0, main = d.feed_fixture()
    given_before = d.git_text(d.clone, 'rev-parse', 'refs/ygg/given')
    recorded_before = (d.json_file(d.registry) or {}).get('given_head')
    result = d.feed_sync('ingest-before-index')
    head_m = d.lane_head()
    assert result.returncode == 0
    assert 'the clone of ygg/alpha-p1/lane/pm1tl1 gets the new commits at its next launch' in result.stdout
    assert len(d.git_text(d.H, 'rev-list', '--parents', '-1', head_m).split()[1:]) > 1
    assert d.git_result(d.H, 'merge-base', '--is-ancestor', main, head_m).returncode == 0
    record = d.json_file(d.registry) or {}
    assert record.get('given_head') == recorded_before
    assert record.get('given_ref') == given_before
    assert d.git_result(d.clone, 'cat-file', '-e', head_m).returncode != 0
    assert d.git_text(d.clone, 'rev-parse', 'refs/ygg/given') == given_before
    assert d.files(d.S / 'feed/alpha-p1') == []


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-46')
def test_launch_retries_failed_feed_without_replacing_box(d):
    d.feed_fixture()
    d.feed_sync('ingest-before-index')
    head_m = d.lane_head()
    result = d.launch()
    assert result.returncode == 0
    assert d.launch_record().get('cwd') == str(d.clone)
    assert d.head() == head_m
    assert d.read(d.clone / 'm.txt') == d.git_text(d.H, 'show', 'main:m.txt')
    record = d.json_file(d.registry) or {}
    assert record.get('given_head') == head_m and record.get('given_ref') == head_m
    assert 'feeding' in record and record['feeding'] is None
    assert d.git_text(d.clone, 'rev-parse', 'refs/ygg/given') == head_m
    assert d.git_result(d.clone, 'fsck', '--connectivity-only').returncode == 0
    assert d.names(d.QF) == []


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-47')
def test_repeated_feed_after_index_recovers_in_same_box(d):
    head_l0, main = d.feed_fixture()
    before_given_ref = d.git_text(d.clone, 'rev-parse', 'refs/ygg/given')
    sync = d.feed_sync('ingest-after-index')
    head_m = d.lane_head()
    assert sync.returncode == 0
    assert 'the clone of ygg/alpha-p1/lane/pm1tl1 gets the new commits at its next launch' in sync.stdout
    assert len(d.git_text(d.H, 'rev-list', '--parents', '-1', head_m).split()[1:]) > 1
    assert d.git_result(d.clone, 'cat-file', '-e', head_m).returncode == 0
    assert d.git_text(d.clone, 'rev-parse', 'refs/ygg/given') == before_given_ref
    assert (d.json_file(d.registry) or {}).get('given_head') == head_l0
    result = d.launch()
    assert result.returncode == 0
    assert d.launch_record().get('cwd') == str(d.clone)
    assert d.head() == head_m
    record = d.json_file(d.registry) or {}
    assert record.get('given_head') == head_m and record.get('given_ref') == head_m
    assert 'feeding' in record and record['feeding'] is None
    assert d.git_text(d.clone, 'rev-parse', 'refs/ygg/given') == head_m
    assert d.git_result(d.clone, 'fsck', '--connectivity-only').returncode == 0
    assert d.names(d.QF) == []


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-47')
def test_assembly_tops_up_history_after_home_lane_moves(d):
    hold = d.T / 'hold'
    d.mkdir(hold)
    with d.background_launch(env={'YGG_TEST_ASSEMBLY_HOLD': str(hold)}) as process:
        d.until(lambda: d.exists(hold / 'entered-assembly'))
        try:
            head_u = d.advance_home_lane_during_assembly()
        finally:
            d.write(hold / 'release-assembly', '')
        result = d.finish_process(process, timeout=90)
        d.wait_launch('pm1tl1')
        d.wait_exit('pm1tl1')
    assert result.returncode == 0
    assert d.launch_record().get('cwd') == str(d.clone)
    assert d.head() == head_u
    assert d.read(d.clone / 'u.txt') == 'u'
    record = d.json_file(d.registry) or {}
    assert record.get('state') == 'ready' and record.get('generation') == 1
    assert record.get('given_head') == head_u and record.get('given_ref') == head_u
    assert d.git_text(d.clone, 'rev-parse', 'refs/ygg/given') == head_u
    assert not d.exists(d.clone / '.git/objects/info/alternates')
    assert d.git_result(d.clone, 'fsck', '--connectivity-only').returncode == 0


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-48')
def test_moved_given_ref_is_quarantined_without_overwrite(d):
    d.made_lane()
    head_d = d.commit_file(d.clone, 'd.txt', 'clean', 'D')
    d.git_result(d.clone, 'update-ref', 'refs/ygg/given', head_d)
    result = d.launch()
    assert result.returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.git_text(d.B / 'checkout', 'rev-parse', 'refs/ygg/given') == head_d
    assert d.git_text(d.B2 / 'checkout', 'rev-parse', 'refs/ygg/given') == d.lane_head()
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status
    assert 'odd:' in status and 'may hold work that was not received' in status
    assert 'given' in status


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-48')
def test_missing_given_objects_are_quarantined_without_repair(d):
    d.made_lane()
    d.remove_clone_objects()
    result = d.launch()
    assert result.returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.head(d.B2 / 'checkout') == d.lane_head()
    assert d.names(d.B / 'checkout/.git/objects/pack') == []
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status
    assert 'odd:' in status and 'may hold work that was not received' in status


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-48')
def test_receive_refuses_missing_newest_lane_commits(d):
    d.feed_fixture()
    d.feed_sync('ingest-before-index')
    head_m = d.lane_head()
    d.authorised()
    head_d = d.commit_file(d.clone, 'd.txt', 'clean', 'D')
    result = d.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
    assert result.returncode == 3
    assert "re-check refused: the clone lacks the lane's newest commits; the next launch brings them" in result.stderr
    assert 'could not be read in time' not in result.stderr
    assert d.lane_head() == head_m
    assert d.wp().get('state') == 'authorised' and d.reviews() == []
    launch = d.launch()
    assert launch.returncode == 0
    assert d.launch_record().get('cwd') == str(d.clone)
    assert d.git_result(d.clone, 'cat-file', '-e', head_m).returncode == 0
    assert d.head() == head_d
    assert d.names(d.QF) == []


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-49')
def test_commit_hook_reads_home_object_without_storing_alternates(d):
    head_x = d.accepted_other_lane_file()
    d.made_lane()
    d.authorised()
    before = d.head()
    d.launch(steps=[{'write_file': {'path': 'shared.txt', 'text': 'one'}},
        {'run': ['git', 'add', 'shared.txt']},
        {'id': 'commit', 'run': ['git', 'commit', '-m', 's']}])
    result = d.step('commit')
    assert result.returncode == 1
    assert 'shared.txt belongs to accepted WP-3; ask the PM to reopen WP-3' in result.stderr
    assert d.head() == before
    assert not d.exists(d.clone / '.git/objects/info/alternates')
    assert d.git_result(d.clone, 'cat-file', '-e', head_x).returncode != 0


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-50')
def test_git_commondir_fifo_is_quarantined_within_thirty_seconds(d):
    d.made_lane()
    d.fifo(d.clone / '.git/commondir')
    result, elapsed = d.elapsed(lambda: d.launch(timeout=30))
    assert result.returncode == 0 and elapsed <= 30
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.is_fifo(d.B / 'checkout/.git/commondir')
    assert 'Yggdrasil replaced your clone because odd:' in d.prompt()
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status
    assert 'odd:' in status and 'commondir' in status
    assert (d.json_file(d.quarantine_record_path()) or {}).get('odd_check') == '3'


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-51')
def test_feed_crash_after_ingest_recovers_record_in_same_generation(d):
    head_l0, main = d.feed_fixture()
    before_given_ref = d.git_text(d.clone, 'rev-parse', 'refs/ygg/given')
    sync = d.feed_sync('feed-after-ingest')
    head_m = d.lane_head()
    assert sync.returncode == 97
    assert len(d.git_text(d.H, 'rev-list', '--parents', '-1', head_m).split()[1:]) > 1
    assert d.git_result(d.H, 'merge-base', '--is-ancestor', main, head_m).returncode == 0
    assert d.git_text(d.clone, 'rev-parse', 'refs/ygg/given') == head_m
    assert d.git_result(d.clone, 'cat-file', '-e', head_m).returncode == 0
    record = d.json_file(d.registry) or {}
    assert record.get('given_head') == head_l0 and record.get('given_ref') == before_given_ref
    assert record.get('feeding') == head_m
    launch = d.launch()
    assert launch.returncode == 0
    assert d.launch_record().get('cwd') == str(d.clone)
    assert d.head() == head_m
    record = d.json_file(d.registry) or {}
    assert record.get('generation') == 1
    assert record.get('given_head') == head_m and record.get('given_ref') == head_m
    assert 'feeding' in record and record['feeding'] is None
    assert d.git_result(d.clone, 'fsck', '--connectivity-only').returncode == 0
    assert d.names(d.QF) == []


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-51')
def test_given_ref_without_unfinished_feed_objects_is_quarantined(d):
    d.feed_fixture()
    d.feed_sync('ingest-before-index')
    head_m = d.lane_head()
    d.write(d.clone / '.git/refs/ygg/given', head_m + '\n')
    launch = d.launch()
    assert launch.returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.head(d.B2 / 'checkout') == head_m
    assert d.lane_head() == head_m
    assert d.read(d.B / 'checkout/.git/refs/ygg/given') == head_m + '\n'
    assert d.git_result(d.B / 'checkout', 'cat-file', '-e', head_m).returncode != 0
    assert (d.json_file(d.quarantine_record_path()) or {}).get('odd_check') == '4'
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status and 'odd:' in status


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-52')
@pytest.mark.parametrize('override', ['shallow', 'info/grafts'])
def test_history_override_cannot_hide_missing_feed_parent(d, override):
    head_l0, head_m, parent_n = d.feed_without_parent()
    path = d.clone / '.git' / override
    d.mkdir(path.parent)
    d.write(path, head_m + '\n')
    d.write(d.clone / '.git/refs/ygg/given', head_m + '\n')
    assert d.launch().returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.head(d.B2 / 'checkout') == d.lane_head() == head_m
    record = d.json_file(d.registry) or {}
    assert record.get('generation') == 2
    if override == 'shallow':
        assert record.get('given_head') == head_m
    assert 'feeding' in record and record['feeding'] is None
    assert (d.json_file(d.QF1) or {}).get('odd_check') == '3'
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status and 'odd:' in status and override in status
    assert d.read(path) == head_m + '\n'
    assert d.git_result(d.clone, 'cat-file', '-e', parent_n).returncode != 0
    assert d.git_result(d.clone, 'rev-list', '--objects', '--quiet', head_m,
        '--not', head_l0).returncode == 0


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-52')
def test_packed_replace_ref_refuses_receive(d):
    d.made_lane()
    d.authorised()
    parent_p = d.head()
    before = d.lane_head()
    head_d = d.commit_file(d.clone, 'd.txt', 'clean', 'D')
    d.git_result(d.clone, 'replace', head_d, parent_p)
    d.git_result(d.clone, 'pack-refs', '--all')
    result = d.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
    assert result.returncode == 3
    assert "re-check refused: the clone's git folder overrides its history" in result.stderr
    assert 'refs/replace/' in result.stderr
    assert d.lane_head() == before
    assert d.wp().get('state') == 'authorised' and d.reviews() == []
    assert d.files(d.clone / '.git/refs/replace') == []
    assert len(d.git_text(d.clone, 'for-each-ref', 'refs/replace/').splitlines()) == 1


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-53')
@pytest.mark.parametrize('kind', ['file', 'link'])
def test_commondir_quarantine_preserves_dependency(d, kind):
    d.made_lane()
    head_x = d.other_repository()
    commondir = d.clone / '.git/commondir'
    if kind == 'file':
        d.write(commondir, d.relative_path(d.T / 'other/.git', d.clone / '.git') + '\n')
        recorded_hash = d.digest(commondir)
    else:
        d.write(d.T / 'cd', str(d.T / 'other/.git') + '\n')
        d.symlink(commondir, d.T / 'cd')
    assert d.launch().returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    if kind == 'file':
        assert d.digest(commondir) == recorded_hash
    else:
        assert d.is_link(commondir) and d.readlink(commondir) == str(d.T / 'cd')
    assert d.git_result(d.clone, 'cat-file', '-e', head_x).returncode == 0
    assert (d.json_file(d.QF1) or {}).get('odd_check') == '3a'
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status and 'outside dependencies the walk saw:' in status
    if kind == 'file':
        assert 'odd:' in status
    target = d.T / ('other/.git' if kind == 'file' else 'cd')
    assert str(target) in status.partition('outside dependencies the walk saw:')[2]


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-53')
def test_gitfile_refuses_receive_and_quarantine_keeps_resolving(d):
    d.made_lane()
    d.authorised()
    head_d = d.commit_file(d.clone, 'd.txt', 'clean', 'D')
    before = d.lane_head()
    gitfile = d.clone / '.git'
    d.rename(gitfile, d.T / 'real-git')
    d.write(gitfile, 'gitdir: ' + d.relative_path(d.T / 'real-git', d.clone) + '\n')
    recorded_hash = d.digest(gitfile)
    ready = d.run('wp', 'ready', '--from', 'pm1tl1', '--package', 'WP-1')
    assert ready.returncode == 3
    assert 're-check refused' in ready.stderr and 'is not a folder Yggdrasil made' in ready.stderr
    assert d.lane_head() == before
    assert d.launch().returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.is_regular(gitfile) and d.digest(gitfile) == recorded_hash
    assert d.head() == head_d
    assert (d.json_file(d.QF1) or {}).get('odd_check') == '3a'
    status = d.quarantine_status(d.B)
    assert 'quarantined' in status and 'odd:' in status and 'checkout/.git' in status


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-54')
def test_unlistable_git_folder_is_quarantined_without_skipping(d):
    d.made_lane()
    folder = d.clone / '.git/objects/zz'
    d.mkdir(folder)
    d.fifo(folder / 'f')
    try:
        d.chmod(folder, 0o111)
        d.require_unlistable(folder)
        result, elapsed = d.elapsed(lambda: d.launch(timeout=30))
        assert result.returncode == 0 and elapsed <= 30
        assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
        assert d.mode(folder) == 0o111 and d.is_fifo(folder / 'f')
        assert (d.json_file(d.QF1) or {}).get('odd_check') == '3'
        status = d.quarantine_status(d.B)
        for text in ['quarantined', 'odd:', 'could not be checked', 'objects/zz']:
            assert text in status
    finally:
        d.chmod(folder, 0o755)


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-54')
def test_changed_repository_format_is_quarantined_without_rewriting(d):
    d.made_lane()
    head_d = d.commit_file(d.clone, 'd.txt', 'clean', 'D')
    d.migrate_reftable()
    config = d.clone / '.git/config'
    recorded_hash = d.digest(config)
    assert d.launch().returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.head(d.B2 / 'checkout') == d.lane_head()
    assert d.digest(config) == recorded_hash
    assert d.git_text(d.clone, 'config', '--file', config, '--get', 'extensions.refstorage') == 'reftable'
    assert d.head() == head_d
    assert (d.json_file(d.QF1) or {}).get('odd_check') == '3'
    status = d.quarantine_status(d.B)
    for text in ['quarantined', 'odd:', 'repository format']:
        assert text in status
    assert d.git_text(d.B2 / 'checkout', 'config', '--file',
        d.B2 / 'checkout/.git/config', '--get', 'extensions.refstorage') == ''


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-55')
def test_quarantined_box_unchanged_through_later_sync_and_feed(d):
    d.odd_clone_with_waiting_worker()
    try:
        d.launch()
    finally:
        d.control('go-2', 'pm1tl1w1')
        d.wait_exit('pm1tl1w1')
    d.launch()
    hashes = d.snapshot_files(d.B)
    listing = d.tree_names(d.B)
    record_hash = d.digest(d.QF1)
    d.commit_file(d.H, 'm.txt', 'clean', 'main')
    sync = d.feed_sync('ingest-before-index')
    launch = d.launch()
    assert sync.returncode == 0
    assert 'the clone of ygg/alpha-p1/lane/pm1tl1 gets the new commits at its next launch' in sync.stdout
    assert launch.returncode == 0
    assert d.launch_record().get('cwd') == str(d.B2 / 'checkout')
    assert d.head(d.B2 / 'checkout') == d.lane_head()
    assert d.snapshot_files(d.B) == hashes and d.tree_names(d.B) == listing
    assert d.digest(d.QF1) == record_hash
    status = d.run('status').stdout
    live = d.quarantine_status(d.B2)
    old = d.quarantine_status(d.B)
    assert live and old and status.index(live) < status.index(old)
    assert 'live; do not remove' in live
    assert 'quarantined' in old and 'may hold work that was not received' in old
    assert 'live; do not remove' not in old


@pytest.mark.release('v1.1.1-mandatory')
@pytest.mark.ac('AC-55')
def test_quarantined_box_unchanged_through_later_cancel(d):
    d.cancel_fixture()
    d.run('wp', 'cancel', 'alpha-p1', '--package', 'WP-2')
    d.launch()
    hashes = d.snapshot_files(d.B)
    listing = d.tree_names(d.B)
    record_hash = d.digest(d.QF1)
    d.authorised('WP-3')
    head_f = d.commit_file(d.B2 / 'checkout', 'f.txt', 'clean', 'F')
    cancel = d.run('wp', 'cancel', 'alpha-p1', '--package', 'WP-3')
    d.launch()
    assert cancel.returncode == 0
    third = d.lanes / 'pm1tl1.g000003'
    assert d.launch_record().get('cwd') == str(third / 'checkout')
    assert (d.json_file(d.registry) or {}).get('generation') == 3
    assert d.head(d.B2 / 'checkout') == head_f
    assert d.snapshot_files(d.B) == hashes and d.tree_names(d.B) == listing
    assert d.digest(d.QF1) == record_hash
    status = d.run('status').stdout
    live = d.quarantine_status(third)
    second = d.quarantine_status(d.B2)
    first = d.quarantine_status(d.B)
    assert live and second and first
    assert status.index(live) < status.index(second) < status.index(first)
    assert 'live; do not remove' in live
    assert 'quarantined' in second and 'kept when WP-3 was cancelled' in second
    assert 'quarantined' in first and 'kept when WP-2 was cancelled' in first
