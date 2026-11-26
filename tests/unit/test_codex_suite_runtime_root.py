"""The Codex builders acceptance suite keeps its runtime folders out of the checkout (tests/conftest.py)."""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRATCH = REPO / "tests" / "acceptance-v1-1-codex" / ".scratch"
ONE_TEST = "tests/acceptance-v1-1-codex/test_codex_builders.py::test_builder_roles_and_programme_reviewer[builder_roles]"


def _entries(folder: Path):
    """None when the folder is absent, so creating even an empty `.scratch/` counts as a change."""
    return set(os.listdir(folder)) if folder.is_dir() else None


def test_a_codex_suite_test_runs_under_the_temp_folder_and_leaves_nothing_in_the_tree(tmp_path):
    before = _entries(SCRATCH)
    env = dict(os.environ, TMPDIR=str(tmp_path))
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env.pop("PYTEST_XDIST_WORKER", None)
    result = subprocess.run([sys.executable, "-m", "pytest", ONE_TEST, "-q", "-p", "no:cacheprovider", "-p", "no:xdist"],
                            cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert _entries(SCRATCH) == before
    assert (tmp_path.resolve() / "ygg-cb-acceptance").is_dir()
