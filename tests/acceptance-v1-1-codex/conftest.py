from contextlib import ExitStack
from pathlib import Path
import sys
import uuid

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from driver import CodexDriver


def pytest_configure(config):
    config.addinivalue_line('markers', 'ac(*ids): signed Codex builders acceptance criteria')
    config.addinivalue_line('markers', 'release(value): release gate membership')


@pytest.fixture
def factory():
    with ExitStack() as stack:
        def create(*, codex=True, approved=True, launcher=None):
            # Keep even pytest scratch paths inside the isolated workspace.
            driver = stack.enter_context(CodexDriver(HERE / '.scratch' / uuid.uuid4().hex))
            driver.standard(codex=codex, approved=approved, launcher=launcher)
            return driver
        yield create


@pytest.fixture
def d(factory):
    return factory()
