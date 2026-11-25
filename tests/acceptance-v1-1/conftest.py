from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from driver import Driver


def pytest_addoption(parser):
    parser.addoption('--acceptance-wheel', default=None,
                     help='Built v1.1 wheel to inspect and install for AC-14 and AC-29')


def pytest_configure(config):
    config.addinivalue_line('markers', 'ac(*ids): signed v1.1 acceptance criterion IDs')
    config.addinivalue_line('markers', 'release(value): v1.1-mandatory or v1.1-optional')
    if not config.option.basetemp:
        config.option.basetemp = str(HERE.parent.parent / '.acceptance-v11-tmp')


@pytest.fixture
def d(tmp_path, request):
    with Driver(tmp_path, wheel=request.config.getoption('--acceptance-wheel')) as driver:
        yield driver


def pytest_collection_finish(session):
    expected = {'AC-' + str(n).zfill(2) for n in range(1, 31) if n != 25}
    parked = set()
    covered = set()
    for item in session.items:
        ids = [value for marker in item.iter_markers('ac') for value in marker.args]
        assert ids, item.nodeid + ': missing AC tag'
        covered.update(ids)
        marks = list(item.iter_markers('release'))
        assert len(marks) == 1, item.nodeid + ': needs exactly one release mark'
        release = 'v1.1-optional' if set(ids) <= {'AC-15', 'AC-19', 'AC-20'} else 'v1.1-mandatory'
        assert marks[0].args == (release,), item.nodeid + ': wrong release mark'
    assert not (covered & parked), 'Parked criteria must have no tests or AC tags'
    assert covered | parked == expected, 'Criterion coverage differs from the brief'
    rows = ['| AC-ID | test name |', '|---|---|']
    for item in session.items:
        for marker in item.iter_markers('ac'):
            for criterion in marker.args:
                rows.append(f'| {criterion} | {item.name} |')
    for criterion in sorted(parked):
        rows.append(f'| {criterion} | parked: see spec-gaps.md |')
    rows.append('| AC-25 | withdrawn: no test or tag |')
    (HERE / 'ac-table.md').write_text('\n'.join(rows) + '\n')


def pytest_sessionfinish(session, exitstatus):
    import json
    import os
    if os.environ.get('ACCEPTANCE_TARGET', 'real') != 'stub':
        return
    reporter = session.config.pluginmanager.get_plugin('terminalreporter')
    counts = {key: len(reporter.stats.get(key, [])) for key in ('failed', 'passed', 'skipped', 'error')}
    counts['total'] = session.testscollected
    passes = [report.nodeid for report in reporter.stats.get('passed', [])]
    (HERE / 'stub-run.json').write_text(json.dumps({'counts': counts, 'passes': passes,
        'exitstatus': exitstatus}, indent=2) + '\n')
    text = '\n'.join('- ' + name + ': AC-23 empty-config example expects exit 0, exactly the empty stub result.' for name in passes)
    (HERE / 'stub-passes.md').write_text((text or 'none') + '\n')
