import os

import pytest

from yggdrasil import launcher
from yggdrasil.util import YggError


def test_ygg_executable_is_the_realpath_on_path(tmp_path, monkeypatch):
    real = tmp_path / "tools" / "ygg"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ygg").symlink_to(real)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert launcher.ygg_executable() == os.path.realpath(real)


def test_ygg_executable_refuses_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(YggError) as caught:
        launcher.ygg_executable()
    assert caught.value.code == 3
    assert caught.value.message == "REFUSED: launch precondition: no ygg executable on PATH"


def test_pre_push_script_calls_the_resolved_ygg(tmp_path, monkeypatch):
    monkeypatch.setenv("YGG_STATE_ROOT", str(tmp_path / "state"))
    path = launcher.hooks_dir("/opt/my tools/ygg")
    text = (path / "pre-push").read_text()
    # A11 AC-16: every lane hook calls the installed ygg's lane check.
    assert text == "#!/bin/sh\nexec '/opt/my tools/ygg' lane-check pre-push \"$@\"\n"
    assert os.access(path / "pre-push", os.X_OK)
