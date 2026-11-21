import os
import subprocess
from pathlib import Path

import pytest

SCAN = str(Path(__file__).resolve().parents[2] / "scripts" / "leak-scan")
TERM = "zz" + "-leak-" + "term"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    denylist = tmp_path / "deny.txt"
    denylist.write_text(TERM + "\n")
    monkeypatch.setenv("YGG_LEAK_DENYLIST", str(denylist))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global.gitconfig"))
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    monkeypatch.chdir(path)
    return path


def _scan(*args):
    return subprocess.run([SCAN, *args], capture_output=True, text=True)


def test_a_zero_colon_name_is_scanned_as_itself(repo):
    (repo / "clean.txt").write_text("hello\n")
    (repo / "0:clean.txt").write_text(TERM + "\n")
    subprocess.run(["git", "add", "clean.txt", "0:clean.txt"], check=True)
    result = _scan("--staged")
    assert result.returncode == 1 and "staged content matches a denylist entry" in result.stderr


def test_clean_staged_content_passes(repo):
    (repo / "clean.txt").write_text("hello\n")
    subprocess.run(["git", "add", "clean.txt"], check=True)
    assert _scan("--staged").returncode == 0


def test_repository_fsmonitor_never_runs(repo, tmp_path):
    marker = tmp_path / "ran"
    script = tmp_path / "monitor.sh"
    script.write_text("#!/bin/sh\ntouch " + str(marker) + "\n")
    script.chmod(0o755)
    subprocess.run(["git", "config", "core.fsmonitor", str(script)], check=True)
    (repo / "clean.txt").write_text("hello\n")
    subprocess.run(["git", "-c", "core.fsmonitor=false", "add", "clean.txt"], check=True)
    assert _scan("--staged").returncode == 0
    assert not marker.exists()


def test_the_hook_index_is_the_one_scanned(repo, tmp_path):
    (repo / "clean.txt").write_text("hello\n")
    subprocess.run(["git", "add", "clean.txt"], check=True)
    other = tmp_path / "other-index"
    environment = dict(os.environ, GIT_INDEX_FILE=str(other))
    (repo / "secret.txt").write_text(TERM + "\n")
    subprocess.run(["git", "add", "secret.txt"], check=True, env=environment)
    assert _scan("--staged").returncode == 0  # the default index holds clean.txt only
    assert subprocess.run([SCAN, "--staged"], capture_output=True, text=True, env=environment).returncode == 1


def test_a_ref_named_like_a_short_id_cannot_stand_in(repo):
    (repo / "clean.txt").write_text("hello\n")
    clean = subprocess.run(["git", "hash-object", "-w", "clean.txt"], capture_output=True, text=True,
                           check=True).stdout.strip()
    (repo / "secret.txt").write_text(TERM + "\n")
    subprocess.run(["git", "add", "secret.txt"], check=True)
    staged = subprocess.run(["git", "rev-parse", ":0:secret.txt"], capture_output=True, text=True,
                            check=True).stdout.strip()
    for length in range(4, 13):  # whatever abbreviation git picks, a tag of that name points at clean content
        subprocess.run(["git", "tag", staged[:length], clean], check=True)
    result = _scan("--staged")
    assert result.returncode == 1 and "staged content matches a denylist entry" in result.stderr
