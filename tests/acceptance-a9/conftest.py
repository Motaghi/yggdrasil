from contextlib import ExitStack
from pathlib import Path
import sys

import pytest


_base = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_base / "suite" if (_base / "suite").is_dir() else _base / "acceptance"))

from driver_a9 import DriverA9


def pytest_configure(config):
    config.addinivalue_line("markers", "ac(*ids): signed A9 acceptance criterion IDs")


@pytest.fixture
def a9_factory(tmp_path):
    with ExitStack() as stack:
        def create(name):
            driver = stack.enter_context(DriverA9(tmp_path / name))
            driver.core_tree()
            return driver
        yield create


@pytest.fixture
def a9_tree(a9_factory):
    return a9_factory("tree")


@pytest.fixture
def a9_ready(a9_tree):
    a9_tree.ready()
    return a9_tree


@pytest.fixture
def a9_push(a9_tree):
    a9_tree.push_seed()
    return a9_tree
