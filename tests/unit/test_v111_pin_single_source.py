"""The Codex version pin has one source, codexlaunch; the helper's check in clones follows it."""

import pytest

from yggdrasil import clones, codexlaunch, util


def test_the_probed_pairs_live_only_in_codexlaunch():
    assert codexlaunch.CODEX_PROBED_VERSIONS == (("darwin", "codex-cli 0.170.0"),)
    assert not hasattr(clones, "CODEX_PROBED_VERSIONS")


def test_the_helper_check_follows_a_changed_pin_in_test_mode(monkeypatch):
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.setattr(codexlaunch, "CODEX_PROBED_VERSIONS", (("linux", "codex-cli 9.9.9"),))
    monkeypatch.setenv("YGG_TEST_PLATFORM", "linux")
    monkeypatch.setenv("YGG_TEST_CODEX_VERSION", "codex-cli 9.9.9")
    assert clones.check_codex() == {"path": None, "identity": None, "version": "codex-cli 9.9.9"}
    monkeypatch.setenv("YGG_TEST_CODEX_VERSION", "codex-cli 0.170.0")
    with pytest.raises(util.YggError) as error:
        clones.check_codex()
    assert error.value.message == ("REFUSED: re-check refused: codex-cli 0.170.0 has not been probed with "
                                   "Yggdrasil; Codex builders run only on codex-cli 9.9.9")
    monkeypatch.setenv("YGG_TEST_PLATFORM", "darwin")
    with pytest.raises(util.YggError) as error:
        clones.check_codex()
    assert error.value.message == ("REFUSED: re-check refused: Codex builders have not been probed on darwin; "
                                   "they run only on linux with codex-cli 9.9.9")


def test_the_helper_check_never_needs_a_binary_in_test_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("YGG_TEST_MODE", "1")
    monkeypatch.setenv("YGG_TEST_PLATFORM", "darwin")
    monkeypatch.setenv("YGG_TEST_CODEX_VERSION", "codex-cli 0.170.0\n")
    monkeypatch.setenv("YGG_CODEX_BIN", str(tmp_path / "no-such-codex"))
    assert clones.check_codex()["version"] == "codex-cli 0.170.0"
    monkeypatch.delenv("YGG_TEST_CODEX_VERSION")
    assert clones.check_codex()["version"] == "not checked (test mode)"


def test_outside_test_mode_the_helper_check_is_the_strict_probe(monkeypatch):
    monkeypatch.delenv("YGG_TEST_MODE", raising=False)
    calls = []

    def probe(**kwargs):
        calls.append(kwargs)
        return {"path": "/x/codex", "identity": {}, "version": "codex-cli 0.170.0", "probed": True, "checked": True}
    monkeypatch.setattr(codexlaunch, "probe", probe)
    assert clones.check_codex("re-check refused")["path"] == "/x/codex"
    assert calls == [{"strict": True, "prefix": "re-check refused: "}]
