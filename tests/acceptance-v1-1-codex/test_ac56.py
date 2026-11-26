import pytest


@pytest.mark.ac("AC-56")
@pytest.mark.release("v1.1.1-mandatory")
def test_ignored_file_survives_lane_sync_feed_and_next_launch(d):
    d.launch()
    c0 = d.head()
    exclude = d.clone / '.git/info/exclude'
    d.write(exclude, d.read(exclude) + '\nbuild/\n')
    d.write(d.clone / 'build/out.bin', 'mine-7')
    d.git_result(d.H, 'checkout', 'main')
    d.commit_file(d.H, 'build/out.bin', 'theirs-3')

    sync = d.sync()
    lane = d.lane_head()
    lane_file = d.git_result(d.H, 'show', f'{lane}:build/out.bin')
    fed = d.git_result(d.clone, 'cat-file', '-e', lane)
    launch = d.launch(steps=())

    assert sync.returncode == 0
    assert lane_file.stdout == 'theirs-3'
    assert fed.returncode == 0
    assert launch.returncode == 0
    assert d.launch_record().get('cwd') == str(d.clone)
    assert 'was not fast-forwarded' in launch.stderr
    assert 'build/out.bin' in launch.stderr
    assert d.read(d.clone / 'build/out.bin') == 'mine-7'
    assert d.head() == c0
    prompt = d.prompt()
    assert 'The lane moved to ' + lane[:12] in prompt
    assert '--no-overwrite-ignore' in prompt
    assert not d.exists(d.QF1)
