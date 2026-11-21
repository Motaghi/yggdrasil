"""Client-credentials token renewer (spec Linear auth paragraph, A4.3).

One renewer holds a lock, requests a new 30-day app token before the current one expires or after a 401,
saves it atomically before using it, and stores credentials in the macOS Keychain (default, service
`yggdrasil-linear`, accounts `client_id`, `client_secret`, `access_token`; the access-token label carries
`(expires <epoch>)`) or in a 0600 JSON file under the profile. There is no refresh token. A renewal
failure raises one `ygg-tracker-auth` alert per failure episode and exit 8. No token is ever placed in an
environment variable, so managed sessions never see it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .. import config, state, util
from ..util import YggError
from .client import LinearError, Transport, classify

SCOPES = "read,write,initiative:read,initiative:write"
LABEL = "Yggdrasil Linear app token (expires {expiry})"
RENEW_MARGIN = 3 * 24 * 3600


SERVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_LIFETIME = 400 * 24 * 3600  # far above the 30-day app token; anything longer is not a real lifetime


def _security(args: list, **kwargs):
    """Run the macOS credential tool; a missing tool or a hang is an actionable auth error, not a crash."""
    try:
        return subprocess.run(["security", *args], capture_output=True, text=True, timeout=10, **kwargs)
    except FileNotFoundError:
        raise _fail("the macOS `security` tool is not available; set linear_token_store: file")
    except (subprocess.TimeoutExpired, OSError) as error:
        raise _fail("the macOS Keychain did not answer (" + type(error).__name__ + "); unlock it and retry")


class KeychainStore:
    def __init__(self, service: str):
        if not SERVICE_RE.fullmatch(str(service or "")):
            # The name goes into the input of `security -i`: only plain names, never quoting or spaces.
            raise YggError(util.EXIT_USAGE, "linear_keychain_service must be 1 to 64 letters, digits, '.', '_' or "
                           "'-', starting with a letter or digit")
        self.service = service

    def _read(self, account: str, secret: bool) -> str | None:
        args = ["find-generic-password", "-s", self.service, "-a", account] + (["-w"] if secret else [])
        result = _security(args)
        if result.returncode != 0:
            return None
        return result.stdout.strip() if secret else result.stdout + result.stderr

    def get(self, account: str) -> str | None:
        return self._read(account, True)

    def expiry(self) -> float | None:
        attributes = self._read("access_token", False) or ""
        match = re.search(r"\(expires (\d+)\)", attributes)
        return float(match[1]) if match else None

    def save_token(self, token: str, expiry: float) -> None:
        """Save via `security -i`, which reads the command from stdin, so the token never appears on argv."""
        if not re.fullmatch(r"[A-Za-z0-9_\-.]+", token or ""):
            raise YggError(util.EXIT_TRACKER, "linear auth: the renewed token has an unexpected shape")
        command = ('add-generic-password -U -s "' + self.service + '" -a access_token -l "' +
                   LABEL.format(expiry=int(expiry)) + '" -w "' + token + '"\n')
        result = _security(["-i"], input=command)
        if result.returncode != 0 or "error" in (result.stderr or "").lower():
            raise _fail("could not save the renewed token in the Keychain; unlock it and retry")


class FileStore:
    """A 0600 JSON file {client_id, client_secret, access_token, expires_at} under the profile."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _check_folder(self) -> None:
        """The token file's folder must be the user's own and not writable by anyone else, so nobody else can
        place a file or a link where the renewed token is written."""
        try:
            info = os.lstat(self.path.parent)
        except OSError as error:
            raise _fail("token folder " + str(self.path.parent) + " is not readable (" + str(error) + ")")
        import stat
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise _fail("token folder " + str(self.path.parent) + " must be a folder owned by you and writable "
                        "by you only")

    def _load(self) -> dict:
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise _fail("token file " + str(self.path) + " is not readable (" + str(error) + ")")
        import stat
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise _fail("token file " + str(self.path) + " must be a regular file, not a link")
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise YggError(util.EXIT_TRACKER, "linear auth: token file " + str(self.path) + " must be mode 0600")
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, account: str) -> str | None:
        return self._load().get(account)

    def expiry(self) -> float | None:
        """`expires_at` as epoch seconds; the store may hold epoch seconds or an ISO 8601 time (A5.8)."""
        value = self._load().get("expires_at")
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            try:
                return util.parse_time(str(value)).timestamp()
            except ValueError:
                return 0.0  # unreadable: treated as expired, so the token is renewed

    def save_token(self, token: str, expiry: float) -> None:
        data = self._load()
        from datetime import datetime, timezone
        data.update(access_token=token,
                    expires_at=datetime.fromtimestamp(int(expiry), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self._check_folder()
        import tempfile
        # An unpredictable name, created exclusively with mode 0600 (never an existing file or a link), then
        # flushed and renamed over the token file in one step.
        descriptor, temporary = tempfile.mkstemp(prefix="." + self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(json.dumps(data))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            util.fsync_dir(self.path.parent)  # the rename is durable only once the folder is flushed (I13)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def store():
    from .client import fake_dir
    directory = fake_dir()
    if directory is not None and (directory / "url").is_file():
        return FileStore(directory / "token-store.json")  # A5.8: the suite's token store
    if config.get("linear_token_store", "keychain") == "file":
        path = config.get("linear_token_file")
        if not path:
            raise YggError(util.EXIT_USAGE, "linear_token_store is file but linear_token_file is not set")
        return FileStore(Path(str(path)).expanduser())
    return KeychainStore(str(config.get("linear_keychain_service") or "yggdrasil-linear"))


def _marker() -> Path:
    return state.root() / "linear-auth-failed"


def _fail(reason: str) -> YggError:
    """One ygg-tracker-auth alert per failure episode, then exit 8 (transitions hold)."""
    with util.locked("linear-auth"):
        if not _marker().exists():
            state.alert("ygg-tracker-auth", "Linear authentication failed: " + reason + "; transitions are held")
            util.atomic_write(_marker(), util.iso() + "\n")
    return YggError(util.EXIT_TRACKER, "linear auth: " + reason)


def _clear_failure() -> None:
    _marker().unlink(missing_ok=True)


class Renewer:
    def __init__(self, token_store=None, transport: Transport | None = None, now=time.time):
        self.store = token_store or store()
        self.transport = transport or Transport()
        self.now = now
        self._token: str | None = None

    def token(self) -> str:
        if self._token:
            return self._token
        with util.locked("linear-token"):
            token = self.store.get("access_token")
            expiry = self.store.expiry()
            if not token or (expiry is not None and expiry - self.now() < RENEW_MARGIN):
                token = self._renew_locked()
            self._token = token
        return token

    def renew(self) -> None:
        with util.locked("linear-token"):
            self._token = self._renew_locked()

    def _renew_locked(self) -> str:
        client_id, secret = self.store.get("client_id"), self.store.get("client_secret")
        if not client_id or not secret:
            raise _fail("no client credentials in the token store")
        try:
            response = self.transport.post("/oauth/token", {"grant_type": "client_credentials", "client_id": client_id,
                                                            "client_secret": secret, "scope": SCOPES}, form=True)
        except LinearError as error:
            raise _fail("token endpoint unreachable (" + error.kind + ")")
        body = response.body if isinstance(response.body, dict) else {}
        token = body.get("access_token")
        if response.status != 200 or not token or classify(response):
            raise _fail("token request refused (HTTP " + str(response.status) + ")")
        lifetime = body.get("expires_in")
        import math
        try:
            usable = not isinstance(lifetime, bool) and isinstance(lifetime, (int, float)) and \
                math.isfinite(float(lifetime)) and 0 < lifetime <= MAX_LIFETIME
        except OverflowError:  # an integer too large for a float
            usable = False
        if not usable:
            # A token without a usable lifetime would be saved as already expired and renewed on every call.
            raise _fail("token response has no valid expires_in")
        expiry = self.now() + float(lifetime)
        self.store.save_token(token, expiry)  # saved before it is used
        _clear_failure()
        return token


def client():
    from .client import Client
    renewer = Renewer()

    def token_source():
        try:
            return renewer.token()
        except YggError:
            raise

    def on_auth_failure():
        try:
            renewer.renew()
        except YggError:
            raise
    return Client(token_source, on_auth_failure)
