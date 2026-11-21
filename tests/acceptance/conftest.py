import pytest

from driver import Driver


def pytest_configure(config):
    config.addinivalue_line("markers", "ac(*ids): signed acceptance criterion IDs")


@pytest.fixture
def empty(tmp_path):
    with Driver(tmp_path) as driver:
        yield driver


@pytest.fixture
def tree(empty):
    empty.core_tree()
    return empty


@pytest.fixture
def quiet_tree(tree):
    tree.quiesce()
    tree.clear_calls()
    return tree
