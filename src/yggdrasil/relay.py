"""The relay for a Codex agent's `ygg` calls (v1.1.1).

A Codex agent's sandbox cannot write the state root, so its `ygg` (the client, when YGG_RELAY is set) writes a
request into `<state root>/relay/<activation>/in`, and the supervisor (the server, outside the sandbox) runs it as
`ygg` with the activation's own fixed identity and a verb allowlist, one request at a time, and publishes the
response create-only into `out`. A request is run only after it was moved into `taken`; an id that is in `taken`,
`out` or `withdrawn` is never run again (the used-id rule). Only the supervisor's own durable record in `out` or
`withdrawn` ever says a call was not run.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import state, util

ID_RE = re.compile(r"^[0-9a-f]{32}$")
REQUEST_RE = re.compile(r"^([0-9a-f]{32})\.json$")
WITHDRAW_RE = re.compile(r"^([0-9a-f]{32})\.withdraw$")
FOLDERS = ("in", "taken", "out", "withdrawn", "files", "tmp")
REQUEST_LIMIT = 1024 * 1024
OUTPUT_LIMIT = 1024 * 1024
# Verbs only Yggdrasil itself runs: never relayable, whatever else says.
INTERNAL_VERBS = ("hook", "push-guard", "lane-check", "_supervise", "_tend")
# Verbs the client runs itself, never through the relay.
LOCAL_VERBS = ("lane-check", "_tend")
# The verbs a managed node of any role may run; each verb's own identity and role checks still apply.
MANAGED_VERBS = (
    "send", "inbox", "tracker", "routemap", "pid", "stage", "wp", "review", "ruling", "acceptance", "issue",
    "exception", "blocker", "decision", "decide", "hardstop", "status", "pending", "merge", "lane",
    "resolve-model", "alerts", "summary", "doctor", "config", "node", "project",
)
# Options whose value names a file the verb reads: the client reads it under the work-area rule and the server
# writes its text into the relay's own `files` folder.
FILE_OPTIONS = ("--body-file", "--file", "--report")
ALIVE_EVERY = 2.0
ALIVE_STALE = 15.0
WITHDRAW_WAIT = 5.0
TMP_GRACE = 5.0
TURN = 0.1
KILL_GRACE = 10.0
DEFAULT_LIMIT = 600
EXIT_UNKNOWN = 75

PAUSED = "Yggdrasil is paused for this scope; stop work now."
STOPPED = "Yggdrasil emergency stop for this node; stop work now."
NOT_RUN = "ygg: the relay did not take this call in time; it was not run, so you may repeat it"
UNKNOWN = "ygg: the relay stopped answering; the outcome of this call is unknown, check ygg status before you repeat it"


# ---------------------------------------------------------------- shared

def relay_root(activation: str) -> Path:
    return state.root() / "relay" / activation


UPLOAD_RE = re.compile(r"^([0-9a-f]{32})-([0-9]+)$")
NOT_UPLOADED = "a relayed call reads only the files its request uploaded"


def relayed_file_refusal(path) -> str | None:
    """Under YGG_VIA_RELAY, the refusal for a file path that is not one of this request's own uploads
    (`<relay root>/files/<request id>-<position>`), decided on the text alone, before anything is opened,
    stat'ed or resolved; None outside the relay or for an upload of this request."""
    via = util.env("YGG_VIA_RELAY")
    if not via:
        return None
    request = util.env("YGG_RELAY_REQUEST") or ""
    text = os.path.normpath(str(path))
    match = UPLOAD_RE.match(os.path.basename(text))
    if not ID_RE.match(request) or match is None or match.group(1) != request or \
            os.path.dirname(text) != os.path.normpath(str(relay_root(via) / "files")):
        return NOT_UPLOADED
    return None


def verb_limit(argv: list[str]) -> int:
    """The relayed child's limit in seconds for a request's argument list."""
    verb = argv[0] if argv else ""
    action = argv[1] if len(argv) > 1 else ""
    if verb == "merge" and action == "check":
        from . import config
        try:
            minutes = float(config.get("merge_check_timeout_minutes", 30) or 30)
        except (TypeError, ValueError):
            minutes = 30.0
        return int(2 * minutes * 60 + 600)
    if verb == "wp" and action == "cancel":
        return 600
    if (verb == "wp" and action == "ready") or (verb == "lane" and action == "sync"):
        return 1800
    return DEFAULT_LIMIT


def normalise(argv: list[str]) -> list[str]:
    """`--option=value` for a file option becomes `--option value`, so a file is always at a value position."""
    out = []
    for value in argv:
        name, sep, rest = value.partition("=")
        if sep and name in FILE_OPTIONS:
            out += [name, rest]
        else:
            out.append(value)
    return out


def file_positions(argv: list[str]) -> list[int]:
    return [i + 1 for i, value in enumerate(argv[:-1]) if value in FILE_OPTIONS and i >= 1]


def is_relayable(argv: list[str]) -> bool:
    return bool(argv) and argv[0] in MANAGED_VERBS and argv[0] not in INTERNAL_VERBS and \
        argv[0] not in state.USER_VERBS


# ---------------------------------------------------------------- client

def client_wanted(argv: list[str]) -> bool:
    """Whether this `ygg` runs as a relay client: YGG_RELAY is set and the verb is neither a lane hook nor the
    tender (nor a bare `--version` or `--help`, which read nothing)."""
    if not os.environ.get("YGG_RELAY") or os.environ.get("YGG_VIA_RELAY"):
        return False
    if not argv or argv[0] in LOCAL_VERBS or argv[0] in ("--version", "-h", "--help"):
        return False
    return True


def _create(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
    finally:
        os.close(descriptor)


def _read_response(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _alive_mark(path: Path):
    try:
        found = os.stat(path)
    except OSError:
        return None
    return (found.st_mtime_ns, found.st_ino, found.st_size)


def _print_response(response: dict) -> int:
    sys.stdout.write(str(response.get("stdout") or ""))
    sys.stdout.flush()
    sys.stderr.write(str(response.get("stderr") or ""))
    sys.stderr.flush()
    try:
        return int(response.get("exit"))
    except (TypeError, ValueError):
        return 1


def client_main(argv: list[str]) -> int:
    """Send one call through the relay and print its response."""
    relay = Path(os.environ["YGG_RELAY"])
    argv = normalise(list(argv))
    files = {}
    try:
        for position in file_positions(argv):
            from . import commands
            files[str(position)] = commands.read_body_file(argv[position])
    except util.YggError as error:
        print(error.message, file=sys.stderr)
        return error.code
    request_id = secrets.token_hex(16)
    body = json.dumps({"argv": argv, "stdin": "", "files": files}).encode()
    incoming = relay / "in"
    temporary = incoming / (".tmp-" + request_id)
    try:
        _create(temporary, body)
        os.link(temporary, incoming / (request_id + ".json"))
    except OSError as error:
        print("ygg: the relay request could not be written: " + str(error.strerror or error), file=sys.stderr)
        return EXIT_UNKNOWN
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    out = relay / "out" / (request_id + ".json")
    withdrawn = relay / "withdrawn" / (request_id + ".json")
    alive = relay / "alive"
    deadline = time.monotonic() + verb_limit(argv) + 60
    mark, changed = _alive_mark(alive), time.monotonic()
    while True:
        if out.exists():
            response = _read_response(out)
            if response is not None:
                return _print_response(response)
        now = time.monotonic()
        current = _alive_mark(alive)
        if current != mark:
            mark, changed = current, now
        if now - changed > ALIVE_STALE or now > deadline:
            break
        time.sleep(TURN)
    # Ask to withdraw; only the supervisor's own record can say the call was not run.
    try:
        _create(incoming / (request_id + ".withdraw"), b"")
    except OSError:
        pass
    until = time.monotonic() + WITHDRAW_WAIT
    while time.monotonic() < until:
        if out.exists():
            response = _read_response(out)
            if response is not None:
                return _print_response(response)
        if withdrawn.exists():
            print(NOT_RUN, file=sys.stderr)
            return EXIT_UNKNOWN
        time.sleep(TURN)
    print(UNKNOWN, file=sys.stderr)
    return EXIT_UNKNOWN


def relayed_hold() -> None:
    """In a relayed child under YGG_TEST_MODE=1: the test-only delay and hold, before the verb runs."""
    if not util.test_mode() or not os.environ.get("YGG_VIA_RELAY"):
        return
    delay = os.environ.get("YGG_TEST_RELAY_DELAY")
    if delay:
        try:
            time.sleep(max(float(delay), 0.0))
        except ValueError:
            pass
    folder = os.environ.get("YGG_TEST_RELAY_HOLD")
    sequence = os.environ.get("YGG_TEST_RELAY_SEQ")
    if not folder or not sequence:
        return
    base = Path(folder)
    if not (base / ("hold-" + sequence)).exists():
        return
    try:
        _create(base / ("entered-" + sequence),
                (str(os.environ.get("YGG_RELAY_REQUEST") or "") + " " + str(os.getpid()) + "\n").encode())
    except OSError:
        pass
    while not (base / ("release-" + sequence)).exists():
        time.sleep(TURN)


# ---------------------------------------------------------------- server

def make_folder(activation: str) -> dict:
    """Made by the launcher before the spawn: the relay folder (0700) with the agent's `in` and the
    supervisor's `taken`, `out`, `withdrawn`, `files` and `tmp`. Returns the device and inode of `in`."""
    root = relay_root(activation)
    util.ensure_dir(root.parent)
    os.chmod(root.parent, 0o700)
    root.mkdir(mode=0o700)
    for name in FOLDERS:
        (root / name).mkdir(mode=0o700)
    found = os.stat(root / "in", follow_symlinks=False)
    return {"relay_in_device": found.st_dev, "relay_in_inode": found.st_ino}


def _lstat(name: str, folder: int):
    try:
        return os.stat(name, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _exists(name: str, folder: int) -> bool:
    return _lstat(name, folder) is not None


def _publish(folder: int, name: str, data: bytes) -> bool:
    """Create-only publication: `.tmp-<id>`, then `link` to `<id>.json`, which fails rather than replaces."""
    temporary = ".tmp-" + name
    try:
        os.unlink(temporary, dir_fd=folder)
    except OSError:
        pass
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
        published = True
    except FileExistsError:
        published = False
    finally:
        try:
            os.unlink(temporary, dir_fd=folder)
        except OSError:
            pass
    try:
        os.fsync(folder)
    except OSError:
        pass
    return published


def _cut(text: str) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= OUTPUT_LIMIT:
        return text
    return data[:OUTPUT_LIMIT].decode("utf-8", errors="ignore") + "\n(cut at 1 MiB)\n"


class Refused(Exception):
    """A request that is answered with a fixed refusal reason."""


def _remove_tree(path: Path) -> bool:
    """Link-safe removal (invariant T rule 3); a host whose rmtree may follow links keeps the folder."""
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        return False
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


class Server:
    """The supervisor's side of the relay for one activation."""

    def __init__(self, activation: str, data: dict, node: dict):
        self.activation = activation
        self.data = data
        self.node = node
        self.root = relay_root(activation)
        self.base = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.fds = {}
        for name in ("taken", "out", "withdrawn", "files", "tmp"):
            self.fds[name] = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.base)
        self.in_fd = None
        self.replaced = False
        self.child = None  # (id, process, deadline, files, started)
        self.child_note = None
        self.sequence = 0
        self.remembered = {}
        self.stopping = False
        self.last_alive = 0.0
        self.ygg = None
        self._open_in()

    # -- descriptors

    def _open_in(self) -> None:
        try:
            fd = os.open("in", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.base)
        except OSError:
            self._mark_replaced()
            return
        found = os.fstat(fd)
        expected = (self.data.get("relay_in_device"), self.data.get("relay_in_inode"))
        if expected != (None, None) and (found.st_dev, found.st_ino) != expected:
            os.close(fd)
            self._mark_replaced()
            return
        self.in_fd = fd

    def _check_in(self) -> bool:
        if self.replaced or self.in_fd is None:
            return False
        now = _lstat("in", self.base)
        held = os.fstat(self.in_fd)
        if now is None or not stat.S_ISDIR(now.st_mode) or (now.st_dev, now.st_ino) != (held.st_dev, held.st_ino):
            self._mark_replaced()
            return False
        return True

    def _mark_replaced(self) -> None:
        if not self.replaced:
            self.replaced = True
            self._refused_line(None, "relay: the request folder was replaced")

    # -- records

    def _refused_line(self, request_id: str | None, reason: str) -> None:
        line = json.dumps({"id": request_id, "at": util.iso(), "reason": reason}) + "\n"
        descriptor = os.open("refused.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600,
                             dir_fd=self.base)
        try:
            os.write(descriptor, line.encode())
        finally:
            os.close(descriptor)

    def _used(self, request_id: str) -> bool:
        name = request_id + ".json"
        return any(_exists(name, self.fds[f]) for f in ("taken", "out", "withdrawn"))

    def _respond(self, request_id: str, code: int, stdout: str, stderr: str) -> None:
        body = json.dumps({"exit": code, "stdout": _cut(stdout), "stderr": _cut(stderr)}).encode()
        if not _publish(self.fds["out"], request_id + ".json", body):
            self._refused_line(request_id, "response already written")

    def _remember(self, name: str, found) -> None:
        self.remembered[name] = (found.st_dev, found.st_ino) if found is not None else None

    def _is_remembered(self, name: str, found) -> bool:
        if name not in self.remembered:
            return False
        key = (found.st_dev, found.st_ino) if found is not None else None
        return self.remembered[name] == key

    def _unlink_in(self, name: str) -> bool:
        try:
            os.unlink(name, dir_fd=self.in_fd)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    # -- listing

    def _entries(self) -> list[tuple[str, os.stat_result]]:
        if not self._check_in():
            return []
        try:
            names = os.listdir(self.in_fd)
        except OSError:
            return []
        found = []
        for name in names:
            info = _lstat(name, self.in_fd)
            if info is not None:
                found.append((name, info))
        found.sort(key=lambda item: (item[1].st_ctime_ns, item[0]))
        return found

    # -- withdrawals

    def withdrawals(self) -> None:
        for name, info in self._entries():
            match = WITHDRAW_RE.match(name)
            if not match or self._is_remembered(name, info):
                continue
            request_id = match.group(1)
            if self._used(request_id):
                if not self._unlink_in(name):
                    self._remember(name, info)
                self._refused_line(request_id, "withdrawal after take")
                if name in self.remembered:
                    continue
                continue
            record = json.dumps({"id": request_id, "at": util.iso()}).encode()
            _publish(self.fds["withdrawn"], request_id + ".json", record)
            self._unlink_in(request_id + ".json")
            if not self._unlink_in(name):
                self._remember(name, info)

    # -- one request

    def _next_request(self):
        """The first request entry in arrival order that may be served this turn."""
        now = time.time()
        for name, info in self._entries():
            match = REQUEST_RE.match(name)
            if not match or self._is_remembered(name, info):
                continue
            request_id = match.group(1)
            if _exists(".tmp-" + request_id, self.in_fd) and now - info.st_ctime < TMP_GRACE:
                continue
            return request_id, name, info
        return None

    def _used_id_rule(self, request_id: str, name: str, info) -> bool:
        """True when the id was already used: the entry is removed, never run, never answered."""
        if not self._used(request_id):
            return False
        if not self._unlink_in(name):
            self._remember(name, info)
        self._refused_line(request_id, "id already used")
        return True

    def _take(self, request_id: str, name: str, info) -> bool:
        try:
            os.rename(name, name, src_dir_fd=self.in_fd, dst_dir_fd=self.fds["taken"])
        except OSError:
            self._remember(name, info)
            self._refused_line(request_id, "could not be moved")
            return False
        try:
            os.fsync(self.fds["taken"])
        except OSError:
            pass
        return True

    def _read_taken(self, request_id: str) -> dict:
        try:
            descriptor = os.open(request_id + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=self.fds["taken"])
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.EMLINK):
                raise Refused("not a regular file") from None
            raise Refused("not a regular file") from None
        try:
            found = os.fstat(descriptor)
            if not stat.S_ISREG(found.st_mode):
                raise Refused("not a regular file")
            if found.st_nlink != 1:
                raise Refused("more than one link")
            if found.st_size > REQUEST_LIMIT:
                raise Refused("larger than 1 MiB")
            chunks, total = [], 0
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > REQUEST_LIMIT:
                    raise Refused("larger than 1 MiB")
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        try:
            value = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise Refused("not one JSON object") from None
        if not isinstance(value, dict):
            raise Refused("not one JSON object")
        for key in value:
            if key not in ("argv", "stdin", "files"):
                raise Refused("unknown key " + str(key))
        for key in ("argv", "stdin", "files"):
            if key not in value:
                raise Refused("missing key " + key)
        argv = value["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and "\x00" not in a for a in argv):
            raise Refused("argv must be a non-empty list of strings")
        if not isinstance(value["stdin"], str):
            raise Refused("stdin must be a string")
        files = value["files"]
        if not isinstance(files, dict):
            raise Refused("files must be an object")
        allowed = {str(p) for p in file_positions(argv)}
        for key, text in files.items():
            if key not in allowed or not isinstance(text, str) or "\x00" in text:
                raise Refused("files must name the positions of options the verb reads as files")
        if set(files) != allowed:  # every option the verb reads as a file is uploaded, never read from its path
            raise Refused("files must name the positions of options the verb reads as files")
        if not is_relayable(argv):
            raise Refused(argv[0] + " is not available to managed sessions")
        return value

    def _environment(self, request_id: str, temporary: Path) -> dict:
        from . import launcher
        supervisor = os.environ
        environment = {}
        for key, value in supervisor.items():
            if (key in launcher.AGENT_ENV_NAMES or key.startswith("LC_")) and not launcher.TOKEN_ENV.match(key):
                environment[key] = value
        for key in launcher.RELAY_CONFIG_ENV:
            if supervisor.get(key) is not None:
                environment[key] = supervisor[key]
        if util.test_mode():
            for key in launcher.RELAY_TEST_ENV:
                if supervisor.get(key) is not None:
                    environment[key] = supervisor[key]
            if supervisor.get("YGG_TEST_RELAY_HOLD") is not None:
                environment["YGG_TEST_RELAY_SEQ"] = str(self.sequence)
        environment.update({k: str(v) for k, v in (self.data.get("pinned") or {}).items()})
        data = self.data
        environment["YGG_NODE"] = data["node"]
        environment["YGG_ACTIVATION"] = data["activation"]
        environment["YGG_PROGRAMME"] = data.get("programme") or ""
        environment["YGG_PROJECT"] = data.get("project") or ""
        environment["YGG_MODEL"] = str(data.get("model") or "")
        environment["YGG_EFFORT"] = str(data.get("effort") or "")
        if self.data.get("agent_nonce"):
            environment["YGG_ACTIVATION_NONCE"] = str(self.data["agent_nonce"])
        environment["YGG_VIA_RELAY"] = self.activation
        environment["YGG_RELAY_REQUEST"] = request_id
        environment["GIT_TERMINAL_PROMPT"] = "0"
        for key in ("TMPDIR", "TMP", "TEMP"):
            environment[key] = str(temporary)
        environment["XDG_CACHE_HOME"] = str(temporary / "cache")
        for key in ("YGG_RELAY", "CODEX_HOME", "YGG_SUPERVISOR_FOR", launcher.CODEX_API_KEY):
            environment.pop(key, None)
        return environment

    def _start(self, request_id: str, value: dict) -> None:
        from . import launcher
        argv = list(value["argv"])
        written = []
        for key, text in value["files"].items():
            name = request_id + "-" + key
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                                 dir_fd=self.fds["files"])
            try:
                os.write(descriptor, text.encode())
            finally:
                os.close(descriptor)
            path = self.root / "files" / name
            argv[int(key)] = str(path)
            written.append(path)
        temporary = self.root / "tmp" / request_id
        temporary.mkdir(mode=0o700)
        (temporary / "cache").mkdir(mode=0o700)
        capture = self.root / "tmp" / (request_id + ".run")
        capture.mkdir(mode=0o700)
        if self.ygg is None:
            self.ygg = launcher.ygg_executable()
        self.sequence += 1
        environment = self._environment(request_id, temporary)
        stdin = open(capture / "stdin", "w+")
        stdin.write(value["stdin"])
        stdin.flush()
        stdin.seek(0)
        stdout = open(capture / "stdout", "w+b")
        stderr = open(capture / "stderr", "w+b")
        try:
            process = subprocess.Popen([self.ygg] + argv, cwd=str(self.root), env=environment, stdin=stdin,
                                       stdout=stdout, stderr=stderr, start_new_session=True)
        except OSError as error:
            for handle in (stdin, stdout, stderr):
                handle.close()
            self._finish_files(request_id, written, temporary, capture)
            self._respond(request_id, 1, "", "relay: request " + request_id + " refused: " + str(error) + "\n")
            return
        for handle in (stdin, stdout, stderr):
            handle.close()
        self.child = {"id": request_id, "process": process, "deadline": time.monotonic() + verb_limit(argv),
                      "files": written, "tmp": temporary, "capture": capture, "killed_at": None}
        self.child_note = None
        self._record_child(self.child)

    def _finish_files(self, request_id, written, temporary, capture) -> None:
        for path in written:
            try:
                os.unlink(path.name, dir_fd=self.fds["files"])
            except OSError:
                pass
        _remove_tree(temporary)
        _remove_tree(capture)

    def _refuse_when_halted(self) -> None:
        """A paused programme or a stopped node gets no new relayed call, with the texts the Claude Code hook
        gives in the same case. A call that started before a pause runs to its end; an emergency stop cancels a
        running call as it always has, and that call is answered as stopping with its outcome unknown."""
        from . import launcher
        programme = self.data.get("programme") or self.node.get("programme")
        if state.paused(programme):
            raise Refused(PAUSED)
        if launcher.stopped({"id": self.data["node"], "programme": programme}):
            raise Refused(STOPPED)

    def serve_one(self) -> None:
        """Start at most one new request (only when no relayed child runs)."""
        if self.child is not None or self.replaced:
            return
        found = self._next_request()
        if found is None:
            return
        request_id, name, info = found
        if self._used_id_rule(request_id, name, info):
            return
        if not self._take(request_id, name, info):
            return
        from . import faults
        faults.boundary("relay-after-take")
        try:
            self._refuse_when_halted()
            value = self._read_taken(request_id)
            self._start(request_id, value)
        except Refused as reason:
            self._respond(request_id, 1, "", "relay: request " + request_id + " refused: " + str(reason) + "\n")
        except Exception as error:  # noqa: BLE001  (any failure is that request's response)
            self._respond(request_id, 1, "", "relay: request " + request_id + " refused: " +
                          type(error).__name__ + ": " + str(error) + "\n")

    def _signal_child(self, number: int) -> None:
        if self.child is None:
            return
        try:
            os.killpg(self.child["process"].pid, number)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _record_child(self, child: dict) -> None:
        """The relay child's identity (pid and start time), appended to `children.jsonl` in the relay folder
        (trusted) before anything else happens with it, so recovery after a supervisor crash can tell whether
        it still runs."""
        pid = child["process"].pid
        self._append_child({"id": child["id"], "pid": pid, "started_at": util.process_start(pid), "at": util.iso()})

    def _record_child_end(self, child: dict) -> None:
        self._append_child({"id": child["id"], "pid": child["process"].pid, "ended": True, "at": util.iso()})

    def _append_child(self, value: dict) -> None:
        descriptor = os.open("children.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600,
                             dir_fd=self.base)
        try:
            os.write(descriptor, (json.dumps(value) + "\n").encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def reap(self) -> None:
        """Enforce the relayed request's limit, and once its whole process group is gone (its leader and every
        process the leader left behind), write its response. Until then the request stays tracked, no other
        request starts, and the limit (SIGTERM, then SIGKILL after KILL_GRACE) applies to the whole group."""
        if self.child is None:
            return
        child = self.child
        process = child["process"]
        now = time.monotonic()
        running = process.poll() is None
        if not running:
            from . import interlock
            running = interlock.group_alive(process.pid)
        if running:
            if child["killed_at"] is None and now > child["deadline"]:
                self.child_note = "relay: request " + child["id"] + " ran out of time; its outcome is unknown"
                self._signal_child(signal.SIGTERM)
                child["killed_at"] = now
            elif child["killed_at"] is not None and now - child["killed_at"] > KILL_GRACE:
                self._signal_child(signal.SIGKILL)
                child["killed_at"] = now
            return
        from . import faults
        faults.boundary("relay-after-run")
        capture = child["capture"]
        stdout = (capture / "stdout").read_bytes().decode("utf-8", errors="replace")
        stderr = (capture / "stderr").read_bytes().decode("utf-8", errors="replace")
        if self.child_note:
            code, stderr = EXIT_UNKNOWN, stderr + self.child_note + "\n"
        else:
            code = process.returncode if process.returncode is not None and process.returncode >= 0 else EXIT_UNKNOWN
        self._respond(child["id"], code, stdout, stderr)
        self._record_child_end(child)
        self._finish_files(child["id"], child["files"], child["tmp"], capture)
        self.child = None
        self.child_note = None

    def stop(self) -> None:
        """A stop or reset: the relayed child in flight is signalled and reported unknown."""
        if self.stopping:
            return
        self.stopping = True
        if self.child is not None:
            self.child_note = ("relay: the session was stopped while request " + self.child["id"] +
                               " ran; its outcome is unknown")
            self._signal_child(signal.SIGTERM)
            self.child["killed_at"] = time.monotonic()

    def answer_waiting(self, text: str) -> None:
        """Every entry still in `in`: the used-id rule, then moved into `taken` and answered `text`."""
        self.withdrawals()
        for name, info in self._entries():
            match = REQUEST_RE.match(name)
            if not match or self._is_remembered(name, info):
                continue
            request_id = match.group(1)
            if self._used_id_rule(request_id, name, info):
                continue
            if not self._take(request_id, name, info):
                continue
            self._respond(request_id, EXIT_UNKNOWN, "", text.format(id=request_id) + "\n")

    def beat(self) -> None:
        now = time.monotonic()
        if now - self.last_alive < ALIVE_EVERY and self.last_alive:
            return
        self.last_alive = now
        try:
            descriptor = os.open(".alive-new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600,
                                 dir_fd=self.base)
            try:
                os.write(descriptor, (util.iso() + " " + str(time.time_ns()) + "\n").encode())
            finally:
                os.close(descriptor)
            os.rename(".alive-new", "alive", src_dir_fd=self.base, dst_dir_fd=self.base)
        except OSError:
            pass

    def turn(self, agent_alive: bool) -> None:
        self.beat()
        self.reap()
        self.withdrawals()
        if self.stopping:
            self.answer_waiting("relay: the session is stopping; request {id} was not run")
        elif agent_alive:
            self.serve_one()

    def drain(self) -> None:
        """The agent's group is gone and no relayed child runs."""
        self.answer_waiting("relay: the session is stopping; request {id} was not run" if self.stopping
                            else "relay: the session ended before request {id} ran")

    def close_descriptors(self) -> None:
        for fd in list(self.fds.values()) + [self.in_fd, self.base]:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.fds = {}
        self.in_fd = None


def archive(activation: str) -> None:
    """Move `in` into `<state root>/relay/_removing/` and remove it link-safely, remove the per-request temporary
    folders, and keep the rest of the relay folder (`taken`, `out`, `withdrawn`, `refused.jsonl`) in
    `<state root>/output/<activation>/relay/`. Repeating it is harmless."""
    root = relay_root(activation)
    if not root.is_dir() or root.is_symlink():
        return
    removing = util.ensure_dir(state.root() / "relay" / "_removing")
    target = removing / (activation + "-in")
    if (root / "in").exists() or (root / "in").is_symlink():
        try:
            os.rename(root / "in", target)
        except OSError:
            pass
    if target.exists() or target.is_symlink():
        _remove_tree(target) if target.is_dir() and not target.is_symlink() else target.unlink(missing_ok=True)
    for name in ("tmp", "files"):
        path = root / name
        if path.is_dir() and not path.is_symlink():
            _remove_tree(path)
    kept = util.ensure_dir(state.root() / "output" / activation) / "relay"
    util.ensure_dir(kept)
    for name in ("taken", "out", "withdrawn", "refused.jsonl", "children.jsonl"):
        source = root / name
        if source.exists() or source.is_symlink():
            destination = kept / name
            if destination.exists():
                continue
            os.rename(source, destination)
    for name in ("alive", ".alive-new"):
        (root / name).unlink(missing_ok=True)
    try:
        os.rmdir(root)
    except OSError:
        pass


def children_running(activation: str) -> bool:
    """Whether a relay child the supervisor recorded in `children.jsonl` may still run: its pid with its
    recorded start time is not proven dead, or its process group still has a member. An unreadable record
    counts as running (fail closed)."""
    from . import interlock, lifecycle
    path = relay_root(activation) / "children.jsonl"
    if path.is_symlink():
        return True
    try:
        text = path.read_text()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    started, ended = {}, set()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            return True
        if not isinstance(value, dict) or not isinstance(value.get("pid"), int):
            return True
        if value.get("ended"):
            ended.add((value.get("id"), value["pid"]))
        else:
            started[(value.get("id"), value["pid"])] = value.get("started_at")
    for (request_id, pid), start in started.items():
        if (request_id, pid) in ended:
            continue
        if lifecycle.evidence(pid, start) != "dead" or interlock.group_alive(pid):
            return True
    return False


def recover() -> list[str]:
    """Recovery of relay folders whose supervisor is gone (the crash-recovery suite's fault boundaries): once the
    supervisor is dead and the agent's process group has no process left, the relay folder is archived as the supervisor would
    have archived it, and the receipt gets the time the exit was found, with no exit code. Nothing is replayed and
    nothing is answered: `taken` without `out` stays the durable record of an unknown outcome, and an exit with no
    code is never a valid exit record, so the activation stays held as uncertain until the user releases it."""
    base = state.root() / "relay"
    if not base.is_dir():
        return []
    from . import interlock, launcher, lifecycle
    done = []
    for entry in sorted(os.listdir(base)):
        if entry.startswith("_") or entry.startswith("."):
            continue
        data = launcher.receipt(entry)
        if not data or data.get("exited_at") is not None:
            if data and data.get("exited_at") is not None and (base / entry).is_dir():
                archive(entry)  # the supervisor recorded the exit but stopped before its archive
                done.append(entry)
            continue
        aborted = data.get("phase") == "aborted" or data.get("outcome") == "aborted"
        if data.get("supervisor_pid") is None and not aborted:
            continue  # a launch still starting its supervisor; a launcher that died is aborted first
        supervisor = lifecycle.evidence(data.get("supervisor_pid"), data.get("supervisor_started_at"))
        if supervisor != "dead" and not (aborted and data.get("pid") is None):
            continue
        pid = data.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and interlock.group_alive(pid):
            continue
        if children_running(entry):
            continue  # a relay child runs on in its own group: nothing of its request is touched yet
        if data.get("agent_nonce") and data.get("supervisor_started_at"):
            # Every process carrying this activation's identity (the agent's, and relay children, which carry
            # it too), also one the supervisor started but had not recorded when it died.
            status, pids = lifecycle.orphan_agents(entry, data["agent_nonce"], data["supervisor_started_at"])
            if status != "ok" or pids:
                continue
        archive(entry)
        launcher.remove_scratch(entry, data)
        if pid is not None:
            with util.locked():
                record = launcher.receipt(entry)
                if record.get("exited_at") is None:
                    record.update(exited_at=util.iso(), exit_code=None, relay_recovered=True)
                    launcher.save_receipt(entry, record)
        done.append(entry)
    return done
