import json
import os
import stat

from yggdrasil import init, util

TOKENS = ("gh" + "o_" + "A" * 36, "sk-" + "svcacct-" + "B" * 24, "AK" + "IA" + "C" * 16,
          "xo" + "xb-" + "1" * 12 + "-" + "D" * 24, "gh" + "s_" + "F" * 36, "sk-" + "admin-" + "G" * 24)


def test_atomic_write_and_append_line_redact(tmp_path):
    target = tmp_path / "doc.md"
    util.atomic_write(target, " ".join(TOKENS))
    text = target.read_text()
    assert all(token not in text for token in TOKENS)
    assert text.count("[REDACTED]") == len(TOKENS)
    log = tmp_path / "log.jsonl"
    util.append_line(log, {"message": TOKENS[0]})
    assert TOKENS[0] not in log.read_text()


def test_logs_are_created_and_tightened_owner_only(tmp_path):
    log = tmp_path / "alerts.jsonl"
    util.append_line(log, {"a": 1})
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    os.chmod(log, 0o644)
    util.append_line(log, {"a": 2})
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert [json.loads(line)["a"] for line in log.read_text().splitlines()] == [1, 2]
    for loose in (0o700, 0o200, 0o640):
        os.chmod(log, loose)
        util.append_line(log, {"a": 3})
        assert stat.S_IMODE(log.stat().st_mode) == 0o600, oct(loose)


def test_state_root_is_tightened(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir(mode=0o755)
    monkeypatch.setenv("YGG_STATE_ROOT", str(root))
    util.state_root()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_state_root_refuses_when_it_cannot_be_made_private(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir(mode=0o755)
    monkeypatch.setenv("YGG_STATE_ROOT", str(root))

    def denied(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(util.os, "chmod", denied)
    try:
        util.state_root()
    except util.YggError as error:
        assert "must have mode 700" in str(error)
    else:
        raise AssertionError("state_root accepted a folder it could not make private")


def test_gitignore_lines_added_once(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/")
    init.ignore_runtime(tmp_path, False)
    init.ignore_runtime(tmp_path, False)
    assert (tmp_path / ".gitignore").read_text().splitlines() == ["node_modules/", "ygg-state/", "ygg-state.safety/",
                                                                  "ygg-tracker/"]
    other = tmp_path / "k"
    other.mkdir()
    init.ignore_runtime(other, True)
    assert (other / ".gitignore").read_text() == "ygg-state/\nygg-state.safety/\n"


def test_unit_tests_never_use_the_real_state_root(tmp_path):
    assert util.state_root() == tmp_path / "isolated-state"
