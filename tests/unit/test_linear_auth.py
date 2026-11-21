"""Linear credentials: safe token writes (B27), approved origins and no redirects (B28), input checks (B29)."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from yggdrasil import util
from test_round2 import clear_caches, world  # noqa: F401  (shared fixture)


def token_store(folder, **values):
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / "token-store.json"
    path.write_text(json.dumps(dict({"client_id": "c", "client_secret": "s", "access_token": "old",
                                     "expires_at": "2099-01-01T00:00:00Z"}, **values)))
    path.chmod(0o600)
    return path


def test_the_renewed_token_never_follows_a_planted_link(world):
    from yggdrasil.linear.auth import FileStore
    path = token_store(world / "creds")
    victim = world / "victim.txt"
    victim.write_text("keep me")
    os.symlink(victim, path.with_name("." + path.name + ".tmp"))  # the old predictable temporary name
    FileStore(path).save_token("new-token", 4_000_000_000)
    assert victim.read_text() == "keep me"
    assert json.loads(path.read_text())["access_token"] == "new-token"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_a_linked_token_file_or_an_open_folder_is_refused(world):
    from yggdrasil.linear.auth import FileStore
    path = token_store(world / "creds")
    link = world / "creds" / "link.json"
    os.symlink(path, link)
    with pytest.raises(util.YggError):
        FileStore(link).get("access_token")
    (world / "creds").chmod(0o777)
    try:
        with pytest.raises(util.YggError) as error:
            FileStore(path).save_token("t", 4_000_000_000)
        assert "writable by you only" in error.value.message
    finally:
        (world / "creds").chmod(0o700)


def test_only_https_linear_origins_are_used(world, monkeypatch):
    from yggdrasil.linear import client
    for bad in ("http://api.linear.app", "https://linear.example.com", "https://user:pw@api.linear.app"):
        monkeypatch.setattr(client.config, "get", lambda key, default=None, bad=bad:
                            bad if key == "linear_api_url" else default)
        with pytest.raises(client.LinearError):
            client.api_url()
    monkeypatch.setattr(client.config, "get", lambda key, default=None: default)
    assert client.api_url() == "https://api.linear.app"


def test_the_fake_is_honoured_only_in_test_mode_and_on_loopback(world, monkeypatch):
    from yggdrasil.linear import client
    fake = world / "fake"
    fake.mkdir()
    (fake / "url").write_text("http://127.0.0.1:9\n")
    monkeypatch.setenv("YGG_LINEAR_FAKE", str(fake))
    assert client.api_url() == "http://127.0.0.1:9"
    (fake / "url").write_text("http://evil.example:80\n")
    with pytest.raises(client.LinearError):
        client.api_url()
    monkeypatch.delenv("YGG_TEST_MODE")
    assert client.fake_dir() is None  # outside test mode the fake directory is ignored entirely


def test_a_redirect_is_never_followed_with_credentials(world, monkeypatch):
    from yggdrasil.linear import client
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers.get("Authorization")))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:" + str(self.server.server_address[1]) + "/stolen")
            self.end_headers()

        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fake = world / "fake"
    fake.mkdir()
    (fake / "url").write_text("http://127.0.0.1:" + str(server.server_address[1]) + "\n")
    monkeypatch.setenv("YGG_LINEAR_FAKE", str(fake))
    try:
        with pytest.raises(client.LinearError) as error:
            client.Transport().post("/graphql", {"query": "{ viewer { id } }"}, headers={"Authorization": "Bearer t"})
        assert "redirect" in error.value.message
        assert [p for p, _ in seen] == ["/graphql"]  # nothing was sent to the redirect target
    finally:
        server.shutdown()


def test_an_unsafe_keychain_service_name_is_refused(world):
    from yggdrasil.linear.auth import KeychainStore
    with pytest.raises(util.YggError):
        KeychainStore('ygg" -w "x')
    assert KeychainStore("yggdrasil-linear").service == "yggdrasil-linear"


def test_a_token_without_a_lifetime_is_not_saved(world):
    from yggdrasil.linear import auth, client
    path = token_store(world / "creds", access_token="", expires_at="")

    class Transport:
        def post(self, path, body, form=False, headers=None):
            return client.Response(200, {}, {"access_token": "fresh", "token_type": "Bearer"})
    renewer = auth.Renewer(auth.FileStore(path), Transport())
    with pytest.raises(util.YggError) as error:
        renewer.token()
    assert "expires_in" in error.value.message
    assert json.loads(path.read_text())["access_token"] == ""


def test_a_missing_credential_tool_is_an_actionable_auth_error(world, monkeypatch):
    from yggdrasil.linear import auth

    def missing(*args, **kwargs):
        raise FileNotFoundError("security")
    monkeypatch.setattr(auth.subprocess, "run", missing)
    with pytest.raises(util.YggError) as error:
        auth.KeychainStore("yggdrasil-linear").get("access_token")
    assert error.value.code == util.EXIT_TRACKER and "linear_token_store: file" in error.value.message


def test_a_service_name_with_a_trailing_newline_is_refused(world):
    from yggdrasil.linear.auth import KeychainStore
    with pytest.raises(util.YggError):
        KeychainStore("yggdrasil-linear\n")


@pytest.mark.parametrize("lifetime", [float("nan"), float("inf"), 10 ** 12, -5, 0, "2591999"])
def test_an_unusable_token_lifetime_is_an_auth_error(world, lifetime):
    from yggdrasil.linear import auth, client
    path = token_store(world / "creds", access_token="", expires_at="")

    class Transport:
        def post(self, path, body, form=False, headers=None):
            return client.Response(200, {}, {"access_token": "fresh", "expires_in": lifetime})
    with pytest.raises(util.YggError) as error:
        auth.Renewer(auth.FileStore(path), Transport()).token()
    assert error.value.code == util.EXIT_TRACKER and "expires_in" in error.value.message


def test_the_token_replacement_flushes_its_folder(world, monkeypatch):
    from yggdrasil.linear import auth
    path = token_store(world / "creds")
    flushed = []
    monkeypatch.setattr(auth.util, "fsync_dir", lambda folder: flushed.append(str(folder)))
    auth.FileStore(path).save_token("new-token", 4_000_000_000)
    assert flushed == [str(path.parent)]


def test_an_integer_too_large_for_a_float_is_an_auth_error(world):
    from yggdrasil.linear import auth, client
    path = token_store(world / "creds", access_token="", expires_at="")

    class Transport:
        def post(self, path, body, form=False, headers=None):
            return client.Response(200, {}, {"access_token": "fresh", "expires_in": 10 ** 400})
    with pytest.raises(util.YggError) as error:
        auth.Renewer(auth.FileStore(path), Transport()).token()
    assert "expires_in" in error.value.message
