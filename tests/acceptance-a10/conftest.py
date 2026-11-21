from contextlib import ExitStack
from pathlib import Path
import sys

import pytest


_base = Path(__file__).resolve().parents[1]
_ws = (_base / "suite").is_dir()
for directory in (("suite", "suite-a9") if _ws else ("acceptance", "acceptance-a9")):
    sys.path.insert(0, str(_base / directory))

from driver_a10 import DriverA10


def pytest_configure(config):
    config.addinivalue_line("markers", "ac(*ids): signed A10 acceptance criterion IDs")


@pytest.fixture
def a10_factory(tmp_path):
    with ExitStack() as stack:
        def create(name, *, tree=True):
            driver = stack.enter_context(DriverA10(tmp_path / name))
            if tree:
                driver.core_tree()
            return driver
        yield create


@pytest.fixture
def a10_tree(a10_factory):
    return a10_factory("tree")


@pytest.fixture
def a10_ready(a10_tree):
    a10_tree.ready()
    return a10_tree
