"""Suite-wide collection rules that sit outside the frozen acceptance folders.

Ruling 134 (19 Nov 2026): the A6 North Star tests are frozen ahead of the work they describe, so they are
expected to fail until A6 is built. They run and are reported as expected failures, and a test that starts
passing shows up as XPASS rather than going unnoticed.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
CODEX_SUITE = TESTS / "acceptance-v1-1-codex"
CODEX_SCRATCH = CODEX_SUITE / ".scratch"

EXPECTED_RED = {
    "tests/acceptance/core/test_a6_northstar_and_page.py": "A6 North Star is frozen ahead of its build (ruling 134)",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        reason = EXPECTED_RED.get(item.nodeid.split("::", 1)[0])
        if reason:
            item.add_marker(pytest.mark.xfail(reason=reason, strict=False))


def codex_runtime_root() -> Path:
    """Where the Codex builders suite's per-test folders go: the system temp folder, resolved so a symlinked
    temp path (/var on macOS) never differs from what the product resolves."""
    return Path(tempfile.gettempdir()).resolve() / "ygg-cb-acceptance"


def _codex_driver_class():
    """The frozen suite's CodexDriver, found by its file so another suite's `driver` module is never patched."""
    target = CODEX_SUITE / "driver.py"
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if path and Path(path).resolve() == target and hasattr(module, "CodexDriver"):
            return module.CodexDriver
    return None


@pytest.fixture(autouse=True)
def _codex_suite_outside_the_tree(request, monkeypatch):
    """The frozen Codex builders conftest puts each test's runtime folder under the suite's own `.scratch/`,
    inside the checkout. Those folders hold credential-like paths (home/.config/yggdrasil/config.yaml) that a
    checkout guard refuses, so the same folder name is moved under the system temp folder instead. The frozen
    files stay as they are; only the root a driver is handed changes."""
    if CODEX_SUITE not in Path(str(request.node.path)).resolve().parents:
        yield
        return
    driver_class = _codex_driver_class()
    if driver_class is None:
        yield
        return
    original = driver_class.__init__
    base = codex_runtime_root()

    def relocated(self, root, *args, **kwargs):
        root = Path(root)
        if root.parent == CODEX_SCRATCH:
            base.mkdir(parents=True, exist_ok=True)
            root = base / root.name
        original(self, root, *args, **kwargs)

    monkeypatch.setattr(driver_class, "__init__", relocated)
    yield


REPO_ROOT = TESTS.parent


def _root_entries():
    return set(os.listdir(REPO_ROOT))


def pytest_sessionstart(session):
    session.config._ygg_root_before = _root_entries()


def pytest_sessionfinish(session, exitstatus):
    """A run that leaves a new file or folder at the repository root fails (a default root such as
    ./ygg-tracker resolved against the checkout). Checked once per run, on the controller, so under
    xdist a leak never fails an unrelated test that happened to run at the same moment. Entries git
    ignores (.pytest_cache, __pycache__) are not counted."""
    before = getattr(session.config, "_ygg_root_before", None)
    if before is None or hasattr(session.config, "workerinput"):
        return
    added = sorted(_root_entries() - before)
    requested = session.config.getoption("xmlpath", None)  # --junitxml: a file the run was asked to write
    if requested:
        # Compared by name in its folder, never by following the entry: a root symlink that points at the report
        # is still a new entry.
        report = Path(os.path.abspath(requested))
        if report.parent.resolve() == REPO_ROOT.resolve():
            added = [name for name in added if name != report.name]
    if added:
        ignored = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "--", *added],
                                 capture_output=True, text=True).stdout.split()
        added = [name for name in added if name not in ignored]
    if added:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        message = "the test run left new entries at the repository root: " + ", ".join(added)
        if reporter:
            reporter.write_line(message, red=True)
        else:
            print(message, file=sys.stderr)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
