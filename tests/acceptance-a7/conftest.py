from pathlib import Path
import sys

import pytest


_base = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_base / "suite" if (_base / "suite").is_dir() else _base / "acceptance"))

from driver_a7 import DriverA7


def pytest_configure(config):
    config.addinivalue_line("markers", "ac(*ids): signed A7 acceptance criterion IDs")


@pytest.fixture
def a7_tree(tmp_path):
    with DriverA7(tmp_path) as driver:
        driver.core_tree()
        driver.ready()
        yield driver
