import pytest

from driver_a11 import LANE


@pytest.mark.ac('AC-14')
@pytest.mark.release('v1.0-blocker')
def test_ac14_row1_preview_lists_every_discard_and_preserves_guard_files(a11_1):
    d = a11_1
    _, c2, _ = d.interrupted()
    own = d.preview_mutations()
    before = d.cancel_snapshot()
    preview = d.finish()
    assert preview.returncode == 3
    lines = preview.stdout.splitlines()
    for path in ['scripts/d.py', '.env.local', 'scripts/b.py', 'scripts/a.py']:
        assert any(path in line and 'not in any recorded commit' in line for line in lines)
    assert '.claude/settings.json' not in preview.stdout and '.ygg-managed' not in preview.stdout
    assert lines[-1].startswith('finish code:')
    assert d.finish_code(preview)
    assert d.cancel_snapshot() == before
    assert d.ref(LANE) == c2 and (d.wp() or {}).get('state') == 'authorised'
    assert d.exists(d.lane() / '.env.local') is True
    assert d.exists(d.lane() / 'scripts/a.py/note.txt') is True
    assert d.read(d.lane() / 'scripts/d.py') == 'mine'
    assert d.trusted_git(d.lane(), 'diff', '--cached').stdout != ''
    result = d.finish(d.finish_code(preview))
    assert result.returncode == 0
    assert d.ref(LANE) == d.l0
    assert d.exists(d.lane() / '.env.local') is False
    assert d.read(d.lane() / 'scripts/a.py').splitlines()[:1] == ['x = 1']
    assert d.exists(d.lane() / 'scripts/a.py/note.txt') is False
    assert d.trusted_git(d.lane(), 'diff', '--cached').stdout == ''
    status = d.clean_status(ignored=True, all_files=True)
    assert {line[3:] for line in status.splitlines()} == {'.claude/settings.json', '.ygg-managed'}
    for path, text in own.items():
        assert d.exists(d.lane() / path) is True and d.read(d.lane() / path) == text
    assert (d.wp() or {}).get('state') == 'cancelled'
    assert d.run('launch', 'pm1tl1').returncode == 0


@pytest.mark.ac('AC-14')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', ['changed', 'new-commit', 'main'])
def test_ac14_row2_preview_code_binds_current_discard_set(a11_1, run):
    d = a11_1
    c1, c2, _ = d.interrupted()
    c9 = None
    if run == 'new-commit':
        d.trusted_git(d.lane(), 'reset', '--hard', d.l0)
        d.write(d.lane() / 'scripts/c.py', 'C9\n')
        d.trusted_git(d.lane(), 'add', 'scripts/c.py')
        d.clean_trusted_commit()
        c9 = d.ref(LANE)
    elif run == 'main':
        d.trusted_git(d.repo_a, 'merge', '--ff-only', c1)
    before = d.lane_snapshot()
    preview = d.finish()
    if run == 'changed':
        d.write(d.lane() / 'scripts/late.py', 'late\n')
    result = d.finish('00000000' if run == 'main' else d.finish_code(preview))
    if run == 'changed':
        assert result.returncode == 3
        assert 'the lane changed since the preview; run --finish again' in result.stderr
        assert d.exists(d.lane() / 'scripts/late.py') is True
        assert d.ref(LANE) == c2 and (d.wp() or {}).get('state') == 'authorised'
    elif run == 'new-commit':
        assert 'commits not in the drop record:' in preview.stdout and c9[:7] in preview.stdout
        assert result.returncode == 0 and d.ref(LANE) == d.l0
        d.send(16, body='x')
        result = d.owed_git('reset', '--soft', c9)
        assert result.returncode != 0
        assert "the lane can't go back to the work of cancelled WP-1" in result.stderr
        assert d.ref(LANE) == d.l0
    else:
        for response in [preview, result]:
            assert response.returncode == 3
            assert 'WP-1 is already in main; send a revert through review instead' in response.stderr
        assert 'finish code:' not in preview.stdout
        assert d.lane_snapshot() == before and (d.wp() or {}).get('state') == 'authorised'


@pytest.mark.ac('AC-14')
@pytest.mark.release('v1.0-blocker')
@pytest.mark.parametrize('run', ['node', 'activation', 'no-interruption'])
def test_ac14_row3_finish_is_user_only(a11_1, run):
    d = a11_1
    if run == 'no-interruption':
        d.one_stage()
        d.authorise('WP-4', 'pm1tl2')
        result = d.finish(node='pm1tl2')
        assert result.returncode == 3 and 'no interrupted cancel on this lane' in result.stderr
    else:
        _, c2, _ = d.interrupted()
        preview = d.finish()
        before = d.lane_snapshot()
        env = {'YGG_NODE': 'pm1'} if run == 'node' else {'YGG_ACTIVATION': 'act-0001'}
        result = d.finish(d.finish_code(preview), env=env)
        assert result.returncode == 3 and 'user-only' in result.stderr
        assert d.lane_snapshot() == before
        assert d.ref(LANE) == c2 and (d.wp() or {}).get('state') == 'authorised'
