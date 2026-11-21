import json

import pytest

from assertions import present, merge_parents


def rules(d, role, standard=False):
    tracker = str(d.tracker / 'projects/alpha-p1').lstrip('/')
    worktree = str(d.lane({'pm': 'pm1', 'tl': 'pm1tl1', 'worker': 'pm1tl1w1', 'su': 'pm1su1', 'reviewer': 'pm1rv1'}[role])).lstrip('/')
    base = {'Bash(ygg *)', f'Read(//{tracker}/**)'}
    if role in {'pm', 'tl', 'worker'}:
        base.add(f'Edit(//{worktree}/**)')
    if role in {'tl', 'worker'}:
        base.update(['Bash(git add *)', 'Bash(git commit *)'])
    if role == 'reviewer':
        scratch = str(d.state / 'scratch' / (d.activation('pm1rv1') or '')).lstrip('/')
        base.update([f'Edit(//{scratch}/**)', 'Bash(git diff *)', 'Bash(git show *)'])
    if standard:
        base.update(['Bash(git status)', 'Bash(git status *)', 'Bash(git diff)', 'Bash(git diff *)',
            'Bash(git log)', 'Bash(git log *)', 'Bash(git show *)', 'Bash(git rev-parse *)',
            'Bash(git branch --list)', 'Bash(git branch --list *)'])
        if role in {'tl', 'worker'}:
            base.update(['Bash(git mv *)', 'Bash(git rm *)', 'Bash(git restore *)'])
        if role == 'tl':
            base.add('Bash(git push origin ygg/alpha-p1/lane/pm1tl1)')
    return base


@pytest.mark.ac('AC-04')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac04_git_config_refusal(a11, row):
    a11.ready()
    for key, value in [('core.untrackedcache', 'true'), ('branch.main.gh-merge-base', 'main')]:
        a11.trusted_git(a11.repo_a, 'config', key, value)
    if row == 1:
        for key, value in [('foo.bar', '1'), ('baz.qux', '2')]:
            a11.trusted_git(a11.repo_a, 'config', key, value)
    if row == 3:
        a11.run('launch', 'pm1tl1')
        a11.wait_exit('pm1tl1')
        for key, value in [('commit.gpgsign', 'false'), ('core.editor', 'true'), ('sequence.editor', 'true')]:
            a11.trusted_git(a11.lane(), 'config', '--worktree', key, value)
    result = a11.run('launch', 'pm1tl1')
    assert result.returncode == (3 if row == 1 else 0)
    if row == 1:
        assert 'foo.bar' in result.stderr and 'baz.qux' in result.stderr
        assert 'core.untrackedcache' not in result.stderr and 'branch.main.gh-merge-base' not in result.stderr
        assert 'ygg agents' not in result.stderr


@pytest.mark.ac('AC-05')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac05_minimal_consent(a11_factory, row):
    d = a11_factory(agreement=False)
    d.ready()
    if row == 1:
        result = d.run('launch', 'pm1tl1')
        assert result.returncode == 3
        assert 'ygg agents allow-standard' in result.stderr and 'ygg agents allow-minimal' in result.stderr
    elif row == 2:
        d.run('agents', 'allow-minimal', '--yes')
        result = d.run('launch', 'pm1tl1')
        assert result.returncode == 0
        settings = d.hook_settings()
        assert settings is not None
        assert set(settings['permissions']['allow']) == rules(d, 'tl')
        assert settings['permissions']['defaultMode'] == 'dontAsk'
    else:
        for env in [{'YGG_NODE': 'pm1tl1'}, {'YGG_ACTIVATION': 'act-0001'}]:
            result = d.run('agents', 'allow-minimal', '--yes', env=env)
            assert result.returncode == 3 and 'user-only' in result.stderr
        assert not d.exists(d.tracker / '_ygg/agents-agreement.json')


@pytest.mark.ac('AC-18')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac18_stop_group_and_locks(a11, row):
    a11.config(stop_grace_seconds=2)
    a11.start(child={'ignore': ['INT', 'TERM'], 'hold_index_lock': True} if row == 1 else None)
    if row == 2:
        pid = a11.hold_lock_outside()
    if row == 3:
        a11.start('pm1tl2')
        a11.write(a11.repo_a / '.git/index.lock', '')
        a11.write(a11.index_lock('pm1tl2'), '')
    result = a11.run('stop', 'pm1tl1')
    if row == 1:
        events = [r for r in a11.child_events() if r['event'] == 'child-signal']
        assert [r['signal'] for r in events] == [2, 15]
        assert a11.seconds_between(events[0]['at'], events[1]['at']) >= 2
        assert a11.wait_group_dead(8) is True
        assert not a11.exists(a11.index_lock())
    elif row == 2:
        assert a11.alive(pid)
        assert a11.exists(a11.index_lock())
        assert 'index.lock kept: another process has it open' in result.stdout
    else:
        assert a11.exists(a11.repo_a / '.git/index.lock')
        assert a11.exists(a11.index_lock('pm1tl2'))


@pytest.mark.ac('AC-19')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac19_standard_consent(a11_factory, row):
    d = a11_factory(agreement=False)
    if row == 1:
        result = d.run('agents', 'allow-standard')
        assert result.returncode == 0
        for heading in ['Project manager:', 'Team lead:', 'Worker:', 'Senior user:', 'Reviewer:']:
            assert heading in result.stdout
        assert not d.exists(d.tracker / '_ygg/agents-agreement.json')
    else:
        d.run('agents', 'allow-standard', '--yes')
        if row == 2:
            record = d.json_file(d.tracker / '_ygg/agents-agreement.json')
            assert record is not None
            for key, value in [('profile', 'standard'), ('rules_version', 1), ('programmes', ['alpha', 'beta']), ('origin', 'user')]:
                assert record[key] == value
            d.start()
            assert set(d.hook_settings()['permissions']['allow']) == rules(d, 'tl', True)
        else:
            d.agreement(rules_version=0)
            result = d.run('launch', 'pm1tl1')
            assert result.returncode == 3 and 'agree again' in result.stderr


@pytest.mark.ac('AC-20')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row,include', [(1, False), (2, True), (3, False), (3, True)])
def test_ac20_project_settings_and_hooks(a11, row, include):
    if include:
        a11.config(agent_project_settings='include')
    a11.start()
    launches = a11.launches('pm1tl1')
    assert len(launches) == 1
    argv = launches[0]['argv']
    assert argv[argv.index('--setting-sources') + 1] == ('user,project,local' if include else 'user')
    if row == 1:
        assert argv[argv.index('--add-dir') + 1] == str(a11.lane())
        assert a11.launch_env()['CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD'] == '1'
    if row == 3:
        assert argv[argv.index('--settings') + 1] == str(a11.state / 'launch' / a11.activation() / 'settings.json')
        settings = a11.hook_settings()
        assert settings['disableAllHooks'] is False
        for event, command in [('PreToolUse', 'pretool'), ('PostToolUse', 'posttool'), ('PostToolUseFailure', 'posttool')]:
            assert 'hook ' + command in json.dumps(settings['hooks'][event])


L1_REQUIRED_SENTENCES = (
    'When a watch wakes you and nothing has changed, end the turn without a message.',
    'Send the user one short summary each hour while work is active, and nothing else unless it needs them or changes the outcome.',
    'Check state with ygg status or git log before you report it.',
    'Confirm a diagnosis from receipts before you act on it.',
    'Re-arm your watch on every wake.',
    'Before you apply an override, name its scope, and never turn a project override into a machine-wide change.',
    'Never run ygg agents allow-standard, ygg agents allow-minimal, ygg ruling record, ygg stage end, ygg stage approve, ygg merge main or ygg wp cancel --finish unless the user asked for it in this conversation.',
    'When a merge-ready alert arrives, tell the user in one plain sentence that the project passed its checks, and when the user says "merge this", run ygg merge main <project> for them.',
)


@pytest.mark.ac("AC-25")
@pytest.mark.release("v1.0-blocker")
def test_ac25_row1_l1_skill_required_sentences(a11_factory):
    text = a11_factory(tree=False).l1_skill()
    for sentence in L1_REQUIRED_SENTENCES:
        assert sentence in text


@pytest.mark.ac("AC-25")
@pytest.mark.release("v1.0-blocker")
def test_ac25_row2_l1_skill_no_routine_messages(a11_factory):
    text = a11_factory(tree=False).l1_skill()
    # Row 1 supplies a positive control so the empty stub cannot pass row 2.
    assert L1_REQUIRED_SENTENCES[0] in text
    for phrase in ('Routine:', 'Nothing new', 'Watch re-armed', 'watch confirms',
                   'No change in the last hour'):
        assert phrase.casefold() not in text.casefold()


@pytest.mark.ac('AC-26')
@pytest.mark.release('v1.0-should')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac26_timezone_precedence(a11, row):
    if row == 1:
        a11.profile(timezone='Europe/Copenhagen')
    if row == 3:
        a11.config(agent_env_passthrough=['TZ'])
    a11.start()
    env = a11.launch_env()
    assert env is not None
    if row == 2:
        assert 'TZ' not in env
    else:
        assert env['TZ'] == ('Europe/Copenhagen' if row == 1 else 'UTC')
        if row == 1:
            assert present(a11.current())['agent_tz'] == 'Europe/Copenhagen'


@pytest.mark.ac('AC-32')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac32_reviewer_rules(a11, row):
    if row == 2:
        a11.run('agents', 'allow-standard', '--yes')
    a11.open_review(model='claude-opus-5-5')
    if row < 3:
        assert a11.hook_settings('pm1rv1') is not None
        assert set(a11.hook_settings('pm1rv1')['permissions']['allow']) == rules(a11, 'reviewer', row == 2)
        assert a11.launch_env('pm1rv1')['YGG_REVIEW_DIR'] == str(a11.state / 'scratch' / a11.activation('pm1rv1'))
    else:
        head = a11.ref('HEAD', a11.lane())
        assert a11.call('git diff ' + head + '..' + head, node='pm1rv1').returncode == 0
        assert a11.call('git show ' + head, node='pm1rv1').returncode == 0
        assert a11.call('git commit -m x', node='pm1rv1').returncode == 2


@pytest.mark.ac('AC-33')
@pytest.mark.release('v1.0-should')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac33_git_does_not_wait_for_gpg_or_editor(a11, row):
    if row == 1:
        script = a11.root / 'gpg'
        a11.executable(script, 'sleep 600\n')
        a11.write(a11.root / 'home/.gitconfig', '[commit]\n gpgsign = true\n[gpg]\n program = ' + str(script) + '\n')
        result, elapsed = a11.elapsed(lambda: a11.commit())
        assert result.returncode == 0 and elapsed <= 10
        assert a11.trusted_git(a11.lane(), 'log', '-1', '--format=%G?').stdout.strip() == 'N'
    elif row == 2:
        script, marker = a11.marker_script('editor-ran')
        a11.executable(script, 'touch ' + str(marker) + '\nsleep 600\n')
        a11.write(a11.root / 'home/.gitconfig', '[core]\n editor = ' + str(script) + '\n')
        result, elapsed = a11.elapsed(lambda: a11.commit(arguments=[]))
        assert result.returncode == 1 and elapsed <= 10
        assert not a11.exists(marker)
    else:
        a11.start()
        for key, expected in [('user.name', 'Example Builder'), ('user.email', 'builder@example.invalid'),
                               ('commit.gpgsign', 'false'), ('core.editor', 'true')]:
            assert a11.trusted_git(a11.lane(), 'config', '--worktree', '--get', key).stdout.strip() == expected


@pytest.mark.ac('AC-34')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac34_agreement_bump(a11, row):
    if row == 1:
        result = a11.start()
        assert result.returncode == 0
        assert 'ygg agents' not in result.stderr
    else:
        a11.ready()
        for index, node in enumerate(['pm1tl1', 'pm1tl2', 'pm1su1'], 10):
            a11.send(index, recipient=node)
        a11.agreement(rules_version=0)
        a11.clear_calls()
        a11.tick(2)
        if row == 2:
            alerts = a11.alerts('ygg-agreement-needed', user=True)
            assert len(alerts) == 1
            assert alerts[0]['text'] == "Yggdrasil's agent rules changed. Run ygg agents allow-standard to see them, then agree again."
            assert a11.launches() == []
            for node in ['pm1tl1', 'pm1tl2', 'pm1su1']:
                assert a11.work_owed(node, 'x')
        else:
            a11.run('agents', 'allow-standard', '--yes')
            a11.tick()
            for node in ['pm1tl1', 'pm1tl2', 'pm1su1']:
                assert len(a11.launches(node)) == 1


@pytest.mark.ac('AC-36')
@pytest.mark.release('fallback-v1.1')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac36_t3_live_thread(a11, row):
    a11.t3_running()
    a11.send(12, urgent=row != 3, body='stop and re-read the brief')
    if row == 2:
        a11.tick(2)
    sends = a11.t3_sends('thr-tl1')
    assert len(sends) == (0 if row == 3 else 1)
    if row != 3:
        text = ' '.join(sends[0]['argv'])
        assert 'Message m-0012 from pm1 (urgent):' in text
        assert '```' in text and 'stop and re-read the brief' in text


@pytest.mark.ac('AC-38')
@pytest.mark.release('v1.0-should')
def test_ac38_pm_claims_instruction(a11):
    text = a11.read(a11.core_path('src/yggdrasil/data/roles/pm.md'))
    assert 'Before you make a provisional decision on a claim, record the command you ran and its output in the decision.' in text


@pytest.mark.ac('AC-41')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row,key,value', [(1, 'core.fsmonitor', 'script'), (2, 'core.fsmonitor', 'false'),
    (3, 'commit.gpgsign', 'true'), (3, 'core.editor', 'vim'), (3, 'sequence.editor', 'vim')])
def test_ac41_unsafe_helper_values(a11, row, key, value):
    a11.ready()
    directory = a11.repo_a
    if row == 1:
        script, marker = a11.marker_script('fsmon-ran', path=a11.root / 'fsmon.sh')
        value = str(script)
    if row == 3:
        a11.run('launch', 'pm1tl1')
        a11.wait_exit('pm1tl1')
        directory = a11.lane()
    a11.trusted_git(directory, 'config', *(['--worktree'] if row == 3 else []), key, value)
    before = a11.trusted_git(directory, 'config', '--worktree', '--list').stdout
    result = a11.run('launch', 'pm1tl1')
    assert result.returncode == (0 if row == 2 else 3)
    if row != 2:
        assert key in result.stderr
    if row == 1:
        assert not a11.exists(marker)
    if row == 3:
        assert a11.trusted_git(directory, 'config', '--worktree', '--list').stdout == before


@pytest.mark.ac('AC-42')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2])
def test_ac42_minimal_role_rules(a11, row):
    a11.run('agents', 'allow-minimal', '--yes')
    a11.start('pm1su1' if row == 1 else 'pm1tl1')
    if row == 1:
        assert a11.hook_settings('pm1su1') is not None
        assert set(a11.hook_settings('pm1su1')['permissions']['allow']) == rules(a11, 'su')
    else:
        assert a11.call('git commit -m x').returncode == 0


@pytest.mark.ac('AC-71')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac71_stop_only_as_far_as_needed(a11, row):
    a11.config(stop_grace_seconds=2)
    if row == 3:
        a11.wall_clock()
        a11.ready()
        a11.agent(mode='exit0', child=True, child_seconds=3)
        a11.run('launch', 'pm1tl1')
        a11.wait_exit('pm1tl1')
        events = [r for r in a11.calls('claude') if r.get('event') == 'exit']
        assert events
        assert a11.seconds_between_whole_seconds(events[-1]['at'], present(a11.current())['exited_at']) >= 3
        assert a11.group_alive() is False
    else:
        a11.start(child={'ignore': ['INT']} if row == 1 else None)
        a11.run('stop', 'pm1tl1')
        if row == 1:
            assert [r['signal'] for r in a11.child_events() if r['event'] == 'child-signal'] == [2, 15]
            assert a11.wait_group_dead(6) is True
        else:
            before = a11.child_events()
            result = a11.run('stop', 'pm1tl1')
            assert result.returncode == 0
            assert a11.child_events() == before


@pytest.mark.ac('AC-72')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac72_stop_hold_release_and_resume(a11, row):
    a11.ready()
    a11.send(10)
    a11.agent(mode='persist')
    a11.tick()
    before = len(a11.launches('pm1tl1'))
    a11.run('stop', 'pm1tl1')
    a11.wait_exit('pm1tl1')
    a11.tick(2)
    assert len(a11.alerts('ygg-stop-held', user=True)) == 1
    if row == 1:
        alerts = a11.alerts('ygg-stop-held', user=True)
        assert len(alerts) == 1
        assert len(a11.launches('pm1tl1')) == before
    else:
        a11.run('node', 'release', 'pm1tl1', '--as', 'aborted' if row == 2 else 'failed')
        a11.tick()
        assert len(a11.launches('pm1tl1')) == before
        a11.run('resume', 'pm1tl1')
        a11.tick()
        assert len(a11.launches('pm1tl1')) - before == (1 if row == 2 else 0)
        if row == 2:
            assert 'm-0010' in a11.receipt_text()


@pytest.mark.ac('AC-74')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2])
def test_ac74_approved_named_commands(a11, row):
    a11.run('agents', 'allow-standard', '--yes')
    a11.ready(**({'agent_commands': ['make test']} if row == 1 else {}))
    if row == 2:
        a11.pid(agent_commands=['make test'])
    for node in (['pm1tl1', 'pm1tl1w1', 'pm1su1', 'pm1'] if row == 1 else ['pm1tl1']):
        a11.run('launch', node)
        a11.wait_exit(node)
        settings = a11.hook_settings(node)
        assert settings is not None
        allow = settings['permissions']['allow']
        for rule in ['Bash(make test)', 'Bash(make test *)']:
            assert (rule in allow) == (row == 1 and node != 'pm1')


@pytest.mark.ac('AC-75')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac75_claude_folder_and_instruction_precedence(a11, row):
    a11.ready()
    if row == 1:
        folder = a11.make_cwd('untrusted')
        a11.write(folder / 'yggdrasil.yaml', 'agent_project_settings: include\n')
        result = a11.run('launch', 'pm1tl1', cwd=folder)
        assert result.returncode == 1
        assert 'agent_project_settings may be set only in a config named by YGG_CONFIG, the home config, a profile or the environment' in result.stderr
    else:
        if row == 3:
            a11.write(a11.repo_a / 'CLAUDE.md', 'never run raw git commit\n')
        a11.run('launch', 'pm1tl1')
        if row == 2:
            env = a11.launch_env()
            assert env is not None
            assert env['CLAUDE_CONFIG_DIR'] == str(a11.root / 'home/.config/yggdrasil/agent-claude')
            assert a11.exists(env['CLAUDE_CONFIG_DIR'])
        else:
            assert "Yggdrasil's commit path and role rules come first: where your project's own instructions differ, follow Yggdrasil's." in a11.prompt()


@pytest.mark.ac('AC-81')
@pytest.mark.release('v1.0-should')
@pytest.mark.parametrize('row', [1, 2])
def test_ac81_timezone_fixed_at_reservation(a11, row):
    a11.profile(timezone='Europe/Copenhagen')
    if row == 1:
        a11.config(agent_env_passthrough=['TZ'])
        a11.start()
    else:
        a11.delayed_given(delay=15)
        a11.profile(timezone='Europe/London')
        a11.wait_launch('pm1tl1', timeout=20)
    assert a11.launch_env() is not None
    assert a11.launch_env()['TZ'] == 'Europe/Copenhagen'


@pytest.mark.ac('AC-82')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row,name', [(1, 'Mars/Base'), (2, 'TZ'), (3, 'PATH'), (3, 'GIT_DIR'), (3, 'YGG_NODE')])
def test_ac82_invalid_agent_environment(a11, row, name):
    a11.ready()
    if row == 1:
        a11.profile(timezone=name)
    elif row == 2:
        a11.profile(launch_env={'TZ': 'UTC'})
    else:
        a11.config(agent_env_passthrough=[name])
    result = a11.run('launch', 'pm1tl1')
    assert result.returncode == 3
    assert {1: "timezone 'Mars/Base' is not a known zone", 2: "launch_env may not set TZ; use the profile's timezone",
            3: 'agent_env_passthrough may not name ' + name}[row] in result.stderr


@pytest.mark.ac('AC-94')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac94_reviewer_isolation(a11, row):
    if row == 3:
        a11.independent_review_and_test_given()
        a11.tick_until_quiet()
        reviewer = a11.prompts('pm1rv1')
        senior = a11.prompts('pm1su1')
        assert len(reviewer) == len(senior) == 2
        assert sorted(sum((name in prompt for name in ['rv-0001', 'rv-0002'])) for prompt in reviewer) == [1, 1]
        assert sorted(sum((name in prompt for name in ['WP-3', 'WP-4'])) for prompt in senior) == [1, 1]
        return
    a11.open_review(model='claude-opus-5-5' if row == 1 else None)
    if row == 1:
        result = a11.edit(a11.lane('pm1rv1') / 'a.txt')
        assert result.returncode == 2
        assert 'Reviewers review read-only; write your verdict to $YGG_REVIEW_DIR/verdict.json.' in result.stderr
    else:
        launches = a11.launches('pm1rv1')
        assert len(launches) == 1
        argv = launches[0]['argv']
        sequence = ['--sandbox', 'workspace-write', '--skip-git-repo-check', '--cd',
                    str(a11.state / 'scratch' / a11.activation('pm1rv1'))]
        assert any(argv[i:i+len(sequence)] == sequence for i in range(len(argv)))
        assert '--add-dir' not in argv


@pytest.mark.ac('AC-95')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac95_review_checkout_and_topology(a11, row):
    if row == 2:
        a11.ready()
        result = a11.run('send', '--from', 'pm1', '--to', 'pm1rv1', '--kind', 'work', '--body', 'x', env={'YGG_NODE': 'pm1'})
        assert result.returncode == 3 and 'topology' in result.stderr
        assert a11.inbox('pm1rv1').stdout == ''
    else:
        diff = a11.large_review_given() if row == 3 else None
        if row == 1:
            a11.open_review(model='claude-opus-5-5')
        head = a11.ref('HEAD', a11.lane())
        checkout = a11.repo_a / '.ygg-worktrees/alpha-p1/_review-rv-0001'
        assert a11.ref('HEAD', checkout) == head
        if row == 1:
            assert a11.trusted_git(checkout, 'symbolic-ref', '-q', 'HEAD').returncode != 0
        else:
            prompt = a11.prompt('pm1rv1')
            assert str(checkout) in prompt
            assert diff[:100000] + '(diff cut at 100000 characters; the full change is in the checkout)' in prompt


@pytest.mark.ac('AC-96')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac96_git_environment_and_hook_repair(a11, row):
    if row == 2:
        a11.ready()
        a11.wait_step(steps=[{'id': 'file', 'write_file': {'path': 'a.txt', 'text': 'zebra-orchid-7'}},
            {'id': 'add', 'run': ['git', 'add', 'a.txt']}, {'id': 'commit', 'run': ['git', 'commit', '-m', 'x']}])
        a11.empty_state()
        result = a11.wait_steps_done()
        assert result.returncode == 1 and 'zebra-orchid-7' in result.stderr
    else:
        a11.start()
        if row == 1:
            assert a11.launch_env() is not None
            assert a11.launch_env()['GIT_TERMINAL_PROMPT'] == '0'
        else:
            files = a11.hook_files()
            assert files is not None and files
            for text in files.values():
                assert str(a11.root / 'bin/ygg') in text
                assert 'scripts/' not in text and 'leak-scan' not in text
            folder = a11.state.with_name('state.safety') / 'hooks'
            a11.write(folder / 'pre-commit', 'exit 0')
            a11.tick()
            assert a11.read(folder / 'pre-commit') == files['pre-commit']


@pytest.mark.ac('AC-97')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('row', [1, 2, 3])
def test_ac97_running_consent_and_new_programme(a11, row):
    if row == 1:
        a11.start()
        a11.agreement(rules_version=0)
        a11.tick(2)
        assert present(a11.node())['state'] == 'running'
        assert [r for r in a11.calls() if r.get('event') == 'interrupt'] == []
    else:
        a11.run('agents', 'allow-standard', '--yes')
        a11.fresh_programme()
        for project, pm, programme, repo in (
                ('alpha-p1', 'pm1', 'alpha', a11.repo_a),
                ('beta-p1', 'pm2', 'beta', a11.repo_b),
                ('gamma-p1', 'pm4', 'gamma', a11.make_cwd('gamma'))):
            a11.pid(project, pm, programme, repo)
            a11.run('pid', 'approve', project)
        a11.send(10)
        a11.tick()
        if row == 2:
            alerts = a11.alerts('ygg-agreement-needed')
            assert len(alerts) == 1 and 'gamma' in json.dumps(alerts[0])
            assert a11.launches('pm4tl1') == []
            assert a11.launches('pm1tl1')
        else:
            a11.run('agents', 'allow-standard', '--yes', '--programme', 'gamma')
            a11.tick()
            assert len(a11.launches('pm4tl1')) == 1
