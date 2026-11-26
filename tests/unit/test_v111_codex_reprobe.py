"""v1.1.1: scripts/codex-reprobe writes one result per row and fails closed. No Codex CLI is started here: a stand-in
binary only answers `--version`, and only the live rows (which the script never runs itself) are asked for."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "codex-reprobe"


@pytest.fixture
def reprobe():
    loader = importlib.machinery.SourceFileLoader("codex_reprobe", str(SCRIPT))
    spec = importlib.util.spec_from_loader("codex_reprobe", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def codex(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n[ \"$1\" = --version ] && echo 'codex-cli 0.170.0' && exit 0\nexit 9\n")
    binary.chmod(0o755)
    return str(binary)


def test_every_row_the_pin_needs_is_listed(reprobe):
    assert set(reprobe.ROWS) == {"P-1", "P-3", "P-4", "P-5", "P-7", "P-8", "P-10", "P-12", "P-13", "CB-16", "CB-25",
                                 "CB-28"}


def test_every_row_but_the_live_ones_is_registered_with_a_runner(reprobe):
    assert set(reprobe.RUNNERS) | set(reprobe.LIVE) == set(reprobe.ROWS)
    assert not set(reprobe.RUNNERS) & set(reprobe.LIVE)
    for row in ("P-12", "P-13"):
        assert callable(getattr(reprobe.Probe, reprobe.RUNNERS[row]))


def test_the_sign_in_rows_are_run_by_main_not_reported_as_unknown(reprobe, codex, tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(reprobe.Probe, "p12", lambda self: (ran.append("P-12"), (True, "stub"))[1])
    monkeypatch.setattr(reprobe.Probe, "p13", lambda self: (ran.append("P-13"), (False, "stub"))[1])
    work = tmp_path / "work"
    assert reprobe.main(["--work", str(work), "--codex", codex, "--rows", "P-12,P-13"]) == 1
    assert ran == ["P-12", "P-13"]
    summary = json.loads((work / "reprobe.json").read_text())
    assert summary["rows"] == {"P-12": "pass", "P-13": "fail"}


def test_the_p12_config_keeps_every_key_name_out_of_the_agents_commands(reprobe, tmp_path):
    import tomllib
    probe = reprobe.Probe("codex", tmp_path)
    data = tomllib.loads(probe.hermetic(4242, keys=True))
    assert data["cli_auth_credentials_store"] == "file"
    assert set(data["shell_environment_policy"]["exclude"]) == set(reprobe.KEY_NAMES)
    provider = data["model_providers"]["reprobe"]
    assert provider["env_key"] in reprobe.KEY_NAMES
    assert set(provider["env_http_headers"].values()) <= set(reprobe.KEY_NAMES)
    assert probe.hermetic(4242) == probe.hermetic(4242, keys=False)
    assert "shell_environment_policy" not in tomllib.loads(probe.hermetic(4242))


def test_the_p13_sandbox_script_quotes_the_codex_path():
    import shlex
    loader = importlib.machinery.SourceFileLoader("codex_reprobe_q", str(SCRIPT))
    spec = importlib.util.spec_from_loader("codex_reprobe_q", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    path = "/tmp/odd dir/$(touch pwned)/`id`/codex"
    text = module.p13_sandbox_script(path)
    words = shlex.split(text.split("|", 1)[1].split(";", 1)[0])
    assert words[0] == path
    assert shlex.quote(path) in text
    assert "/" not in module.P12_COMMAND  # P-12's command names no path at all


def test_live_rows_are_recorded_from_marks_and_the_verdict_fails_closed(reprobe, codex, tmp_path):
    work = tmp_path / "work"
    code = reprobe.main(["--work", str(work), "--codex", codex, "--rows", "CB-16,CB-25", "--mark", "CB-16=pass"])
    assert code == 1  # the probe rows were not run, so the pair stays unprobed
    summary = json.loads((work / "reprobe.json").read_text())
    assert summary["version"] == "codex-cli 0.170.0" and summary["verdict"] == "fail"
    assert summary["rows"] == {"CB-16": "pass", "CB-25": "not run"}
    record = json.loads((work / "records" / "CB-16.json").read_text())
    assert record["result"] == "pass" and record["detail"] == "marked by the operator"


def test_a_work_folder_that_is_not_empty_is_refused(reprobe, codex, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "x").write_text("x")
    assert reprobe.main(["--work", str(work), "--codex", codex, "--rows", "CB-16"]) == 2


def test_an_unknown_row_is_refused(reprobe, codex, tmp_path):
    assert reprobe.main(["--work", str(tmp_path / "w"), "--codex", codex, "--rows", "P-99"]) == 2


def test_a_version_output_of_more_than_one_line_is_unknown(reprobe, tmp_path):
    binary = tmp_path / "codex2"
    binary.write_text("#!/bin/sh\necho 'codex-cli 0.170.0'\necho extra\n")
    binary.chmod(0o755)
    assert reprobe.version_line(str(binary)) == "an unknown Codex version"


def test_paths_in_generated_shell_text_are_shell_quoted():
    """A path is quoted for the shell (shlex.quote), never with JSON's double quotes, which still expand `$(...)`,
    backticks and `$NAME`."""
    source = SCRIPT.read_text()
    assert "json.dumps(str(" not in source
    assert source.count("shlex.quote(str(") >= 8


@pytest.fixture
def standin(reprobe, tmp_path):
    """P-13's stand-in, started over plain HTTP (no certificate) on 127.0.0.1; yields (port, log)."""
    import subprocess
    import sys
    import time
    server, log, portfile = tmp_path / "standin.py", tmp_path / "standin.log", tmp_path / "standin.port"
    server.write_text(reprobe.STANDIN)
    process = subprocess.Popen([sys.executable, "-I", str(server), str(log), str(portfile), reprobe.ACCOUNT, "", ""],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if portfile.exists() and portfile.read_text().strip():
                break
            time.sleep(0.05)
        yield int(portfile.read_text()), log
    finally:
        process.kill()
        process.wait()


def _call(port, method, path, body=None, headers=None):
    import http.client
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


def test_the_standin_routes_the_workspace_with_no_constraint(standin):
    port, _ = standin
    status, data = _call(port, "GET", "/backend-api/wham/accounts/check")
    entry = json.loads(data)["accounts"][0]
    assert status == 200
    assert entry["account_routing_override"] == "NO_CONSTRAINT"
    assert entry["workspace_backend_origin"].startswith("http://127.0.0.1:")  # https once a certificate is given


def test_the_standin_matches_a_readable_model_request_to_its_session(standin):
    port, log = standin
    status, _ = _call(port, "POST", "/backend-api/codex/responses", json.dumps({"input": "reprobe-S2"}).encode(),
                      {"content-type": "application/json"})
    assert status == 200
    assert " MODEL S2 gen=" in log.read_text()


@pytest.mark.parametrize("body, headers", [
    (b"\x28\xb5\x2f\xfd compressed", {"content-encoding": "zstd"}),
    (b"not json at all reprobe-S1", {}),
])
def test_the_standin_refuses_a_body_it_cannot_read_loudly(standin, body, headers):
    port, log = standin
    status, _ = _call(port, "POST", "/backend-api/codex/responses", body, headers)
    text = log.read_text()
    assert status == 400
    assert " MODEL-UNREADABLE " in text and " MODEL " not in text.replace("MODEL-UNREADABLE", "")


def test_p13_sessions_send_uncompressed_model_requests(reprobe, tmp_path):
    argv = reprobe.p13_session_argv("/bin/codex", tmp_path / "box")
    at = argv.index("--disable")
    assert argv[at + 1] == "enable_request_compression"
    assert argv[:2] == ["/bin/codex", "exec"] and argv[-1] == "-"


def test_the_standin_certificate_is_a_leaf_signed_by_a_separate_dummy_ca(reprobe, tmp_path):
    import shutil
    import ssl
    import subprocess
    if not shutil.which("openssl"):
        pytest.skip("openssl is not installed")
    made = reprobe.standin_certificates(tmp_path)
    assert made is not None
    ca, cert, key = made
    assert ca != cert and all(path.is_file() for path in made)
    shown = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-text"], capture_output=True, text=True,
                           check=True).stdout
    assert "CA:FALSE" in shown and "TLS Web Server Authentication" in shown and "IP Address:127.0.0.1" in shown
    verified = subprocess.run(["openssl", "verify", "-CAfile", str(ca), str(cert)], capture_output=True, text=True)
    assert verified.returncode == 0
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))  # the key matches the certificate
    assert "BEGIN CERTIFICATE" in ca.read_text() and "PRIVATE KEY" not in ca.read_text()


def test_p13_counts_only_refreshes_with_the_expired_token_before_the_first_answer(reprobe):
    raced = ["1.00 TOKEN-IN rt-gen-0", "1.20 TOKEN-IN rt-gen-0", "1.30 TOKEN-OUT rt-gen-1", "1.31 TOKEN-OUT rt-gen-2"]
    late = ["1.00 TOKEN-IN rt-gen-0", "1.10 TOKEN-OUT rt-gen-1", "3.00 TOKEN-IN rt-gen-0", "3.10 TOKEN-OUT rt-gen-2"]
    reread = ["1.00 TOKEN-IN rt-gen-0", "1.20 TOKEN-IN rt-gen-1", "1.30 TOKEN-OUT rt-gen-1"]
    assert reprobe.competing_refreshes(raced) == 2
    assert reprobe.competing_refreshes(late) == 1
    assert reprobe.competing_refreshes(reread) == 1
    assert reprobe.competing_refreshes([]) == 0


def test_the_standin_holds_the_first_refresh_until_a_second_arrives(reprobe, standin):
    import threading
    port, log = standin
    answers = []
    def refresh():
        answers.append(_call(port, "POST", "/oauth/token", json.dumps({"refresh_token": "rt-gen-0"}).encode(),
                             {"content-type": "application/json"})[0])
    first = threading.Thread(target=refresh)
    first.start()
    first.join(1.5)
    assert first.is_alive() and " TOKEN-OUT " not in log.read_text()
    refresh()
    first.join(10)
    lines = log.read_text().splitlines()
    assert answers == [200, 200]
    assert reprobe.competing_refreshes(lines) == 2



def _fake_p13(made, *, extra=None, broken=False, swap=False):
    """A stand-in for the row body: makes `<parent>/L` and the files the real row makes there, as it does."""
    def run(self, base, parent):
        login = parent / "L"
        login.mkdir(mode=0o700)
        info = login.lstat()
        self.p13_login = (login, (info.st_dev, info.st_ino))
        for name in ("config.toml", "auth.json", "auth.lock", "auth.json.lock"):
            (login / name).write_text("dummy")
        if swap:
            login.rename(parent / "moved-away")
            (parent / "other-login").mkdir()
            for name in ("config.toml", "auth.json", "auth.lock"):
                (parent / "other-login" / name).write_text("someone else's")
            login.symlink_to(parent / "other-login")
        if extra:
            (login / extra).write_text("not the row's")
        made.append(login)
        if broken:
            raise RuntimeError("the stand-in did not start")
        return False, "stub"
    return run


@pytest.mark.parametrize("broken", [False, True])
def test_p13_removes_the_login_folder_it_made_pass_or_fail(reprobe, tmp_path, monkeypatch, broken):
    parent = tmp_path / "other-volume"
    parent.mkdir()
    (parent / "keep.txt").write_text("yours")
    made = []
    monkeypatch.setattr(reprobe.Probe, "_p13", _fake_p13(made, broken=broken))
    probe = reprobe.Probe("codex", tmp_path / "work", parent)
    (tmp_path / "work").mkdir()
    if broken:
        with pytest.raises(RuntimeError):
            probe.p13()
    else:
        assert probe.p13() == (False, "stub")
    assert made == [parent / "L"]
    assert not (parent / "L").exists()
    assert sorted(p.name for p in parent.iterdir()) == ["keep.txt"]
    assert (parent / "keep.txt").read_text() == "yours"


def test_p13_keeps_a_login_folder_holding_files_it_did_not_make_and_says_so(reprobe, tmp_path, monkeypatch):
    parent = tmp_path / "other-volume"
    parent.mkdir()
    monkeypatch.setattr(reprobe.Probe, "_p13", _fake_p13([], extra="stray.tmp"))
    (tmp_path / "work").mkdir()
    ok, detail = reprobe.Probe("codex", tmp_path / "work", parent).p13()
    assert ok is False
    assert sorted(p.name for p in (parent / "L").iterdir()) == ["stray.tmp"]
    assert str(parent / "L") + " was kept: it holds stray.tmp" in detail


def test_p13_never_removes_a_login_folder_it_did_not_make(reprobe, tmp_path):
    parent = tmp_path / "other-volume"
    (parent / "L").mkdir(parents=True)
    (parent / "L" / "auth.json").write_text("yours")
    (tmp_path / "work").mkdir()
    with pytest.raises(FileExistsError):
        reprobe.Probe("codex", tmp_path / "work", parent).p13()
    assert (parent / "L" / "auth.json").read_text() == "yours"


def test_an_existing_login_folder_under_the_login_parent_is_refused_up_front(reprobe, codex, tmp_path, monkeypatch,
                                                                             capsys):
    ran = []
    monkeypatch.setattr(reprobe.Probe, "p13", lambda self: (ran.append("P-13"), (True, "stub"))[1])
    parent = tmp_path / "other-volume"
    (parent / "L").mkdir(parents=True)
    work = tmp_path / "work"
    code = reprobe.main(["--work", str(work), "--codex", codex, "--rows", "P-13", "--login-parent", str(parent)])
    assert code == 2 and ran == []
    assert str(parent / "L") + " already exists" in capsys.readouterr().err
    assert not (work / "records").exists()
    assert reprobe.main(["--work", str(tmp_path / "work2"), "--codex", codex, "--rows", "CB-16",
                         "--login-parent", str(parent)]) == 1  # only the link probe uses the folder


def test_p13_cleanup_never_follows_a_login_folder_swapped_for_a_link(reprobe, tmp_path, monkeypatch):
    parent = tmp_path / "other-volume"
    parent.mkdir()
    made = []
    monkeypatch.setattr(reprobe.Probe, "_p13", _fake_p13(made, swap=True))
    (tmp_path / "work").mkdir()
    probe = reprobe.Probe("codex", tmp_path / "work", parent)
    ok, detail = probe.p13()
    assert not ok and "was kept: it is no longer the folder this row made" in detail
    assert sorted(os.listdir(parent / "other-login")) == ["auth.json", "auth.lock", "config.toml"]
    assert (parent / "other-login" / "auth.json").read_text() == "someone else's"


def test_p13_keeps_its_own_parent_when_something_else_is_inside(reprobe, tmp_path, monkeypatch):
    made = []
    monkeypatch.setattr(reprobe.Probe, "_p13", _fake_p13(made, extra="notes.txt"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    real_stat = os.stat
    monkeypatch.setattr(reprobe.os, "stat", lambda p, *a, **k: type("S", (), {"st_dev": 1 if str(p) == str(home) else 2})()
                        if str(p) in (str(home), str(work)) else real_stat(p, *a, **k))
    probe = reprobe.Probe("codex", work, None)
    ok, detail = probe.p13()
    assert "was kept" in detail
    assert (made[0] / "notes.txt").read_text() == "not the row's"
