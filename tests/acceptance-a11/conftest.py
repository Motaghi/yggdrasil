from contextlib import ExitStack
from pathlib import Path
import sys

import pytest


HERE = Path(__file__).resolve().parent
_ws = (HERE.parent / 'suite').is_dir()
for name in (('suite', 'suite-a9', 'suite-a10') if _ws else ('acceptance', 'acceptance-a9', 'acceptance-a10')):
    sys.path.insert(0, str(HERE.parent / name))
sys.path.insert(0, str(HERE))

from driver_a11 import DriverA11


def pytest_configure(config):
    config.addinivalue_line('markers', 'ac(*ids): signed A11 acceptance criterion IDs')
    config.addinivalue_line('markers', 'release(value): v1.0-blocker, v1.0-should or fallback-v1.1')


@pytest.fixture
def a11_factory(tmp_path):
    with ExitStack() as stack:
        drivers = []
        def create(name='tree', *, tree=True, agreement=True):
            driver = stack.enter_context(DriverA11(tmp_path / name))
            drivers.append(driver)
            if tree:
                driver.core_tree(agreement=agreement)
            return driver
        yield create
        for driver in drivers:
            for observation in driver.commit_observations:
                if observation['exit'] == 1:
                    assert observation['after'] == observation['before'], (
                        'A refused agent commit must leave HEAD and the staged index unchanged')


@pytest.fixture
def a11(a11_factory):
    return a11_factory()
