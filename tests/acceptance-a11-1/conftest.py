from contextlib import ExitStack
from pathlib import Path
import sys

import pytest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BASE = ROOT / 'suite' if (ROOT / 'suite').is_dir() else ROOT / 'acceptance'
for directory in (BASE, ROOT / 'suite-a9', ROOT / 'suite-a10', ROOT / 'suite-a11', HERE):
    if not directory.is_dir() and directory.name.startswith('suite-'):
        directory = ROOT / directory.name.replace('suite-', 'acceptance-', 1)
    sys.path.insert(0, str(directory))

from driver_a11_1 import DriverA11_1


def pytest_configure(config):
    config.addinivalue_line('markers', 'ac(*ids): signed A11.1 acceptance criterion IDs')
    config.addinivalue_line('markers', 'release(value): release mark in the signed criterion')


@pytest.fixture(params=['on', 'off'], ids=['stage-close-on', 'stage-close-off'])
def a11_1_factory(tmp_path, request):
    with ExitStack() as stack:
        def create(name='tree', *, tree=True):
            driver = stack.enter_context(DriverA11_1(tmp_path / name))
            if request.param == 'off':
                driver.release_off('stage_close')
            if tree:
                driver.core_tree()
            return driver
        yield create


@pytest.fixture
def a11_1(a11_1_factory):
    return a11_1_factory()
