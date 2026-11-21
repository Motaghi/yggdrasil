from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
_ws = (ROOT / "suite").is_dir()
sys.path.insert(0, str(ROOT / ("suite" if _ws else "acceptance")))
sys.path.insert(0, str(ROOT / ("suite-a7" if _ws else "acceptance-a7")))
sys.path.insert(0, str(ROOT / ("suite-a8" if _ws else "acceptance-a8")))

from driver_a8 import DriverA8


def pytest_configure(config):
    config.addinivalue_line("markers", "ac(*ids): signed A8 acceptance criterion IDs")


@pytest.fixture
def a8(tmp_path):
    with DriverA8(tmp_path) as driver:
        yield driver
