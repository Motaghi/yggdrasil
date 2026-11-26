"""The once-per-run repository-root check (tests/conftest.py): a file the run was asked to write with --junitxml
is allowed, but any other new root entry fails the run, including a symlink that points at that report. The
probes run in a temporary git repository holding a copy of the real conftest, so their root
files never meet another test's run at this checkout's root."""
import shutil
import subprocess
import sys
from pathlib import Path

CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    shutil.copyfile(CONFTEST, repo / "tests" / "conftest.py")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _run(repo: Path, body: str):
    (repo / "tests" / "test_probe.py").write_text(body)
    return subprocess.run([sys.executable, "-m", "pytest", "tests/test_probe.py", "-q", "-p", "no:cacheprovider",
                           "--junitxml=junit-probe.xml"], cwd=repo, capture_output=True, text=True)


def test_the_requested_report_alone_passes(tmp_path):
    result = _run(_repository(tmp_path), "def test_ok():\n    pass\n")
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_symlink_to_the_requested_report_fails_the_run(tmp_path):
    repo = _repository(tmp_path)
    body = ("import os\nfrom pathlib import Path\ndef test_link():\n"
            "    root = Path(__file__).resolve().parents[1]\n"
            "    os.symlink(root / 'junit-probe.xml', root / 'report-link')\n")
    result = _run(repo, body)
    assert result.returncode == 1 and "report-link" in result.stdout, result.stdout + result.stderr
