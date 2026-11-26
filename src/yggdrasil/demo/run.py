"""`ygg demo` and `ygg demo --cleanup <folder>`: the driver of the live demo.

The driver plays the user and the main session. Every Yggdrasil action runs in a child process in the demo's
own environment, built from scratch in a fresh temp folder; the driver itself reads nothing of the user's (no
config, profile, plugin, state or tracker function is called here). Teardown stops every process the demo
recorded, by exact start identity only, and runs whatever happened before it.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .. import release, util
from . import script

SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
BLOCKED_PROGRAMS = ("osascript", "open", "security", "gh", "t3")
SHORT_TIMEOUT = 120
DASHBOARD_START_SECONDS = 30  # how long the dashboard server may take to print its address
AGENTS_END_SECONDS = 30  # how long the driver waits, after the last line, for every agent to have ended
STEP4_SECONDS = 20  # teardown step 4: how long to wait until no recorded process is alive
STOP_SECONDS = 90  # teardown step 2: how long `ygg stop` may take
NARRATION = {1: "brief written", 2: "plan approved", 3: "WP-1 ready for review", 4: "WP-2 ready for review",
             5: "reviews approved", 6: "merged into main", 7: "project closed"}
WAITING_FOR = {2: "the plan to be ready", 3: "WP-1's review", 4: "WP-2's review", 5: "both reviews to be approved",
               6: "the project to be ready to merge", 7: "the project to close"}
GITCONFIG = "[user]\n\tname = Yggdrasil Demo\n\temail = demo@example.invalid\n"
# The demo's Codex sessions run the scripted stand-in, which calls no model; this provider gives them a sign-in
# route (a provider with no key) so they start like a signed-in user's, and nothing ever connects to it.
CODEX_CONFIG = ('model_provider = "ygg-demo"\n\n[model_providers.ygg-demo]\nname = "Yggdrasil demo stand-in"\n'
                'base_url = "http://127.0.0.1:9/v1"\n')
# What `ygg demo --cleanup` checks and reads inside a folder: every folder below these is walked.
CHECKED_FOLDERS = ("state", "state.safety", "tracker", "userhome", "home")
TOP_FILES = ("demo-marker.json", "conductor.pid", "dashboard.pid")
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


class Failure(Exception):
    def __init__(self, where: str, reason: str):
        super().__init__(reason)
        self.where, self.reason = where, reason


class Interrupted(BaseException):
    """Raised from a signal handler to unwind the flow into teardown."""


class Unsafe(Exception):
    """A folder `ygg demo --cleanup` refuses: the reason follows `is not a safe demo folder:`."""


def config_text(*, fast: bool) -> str:
    return "\n".join([
        "tracker: files",
        "launcher: headless",
        "launcher_cli: claude-code",
        "alert_forwarder: jsonl",
        'alert_macos: "off"',
        'branch_protection: "off"',
        "commit_identity: {name: Yggdrasil Demo, email: demo@example.invalid, trailers: none}",
        # The demo shortens the conductor's waits between ticks and between launches, because those waits, not the
        # scripted agents, dominate its run time.
        "tick_seconds: 0.3",
        "settle_delay_seconds: 0.2",
        # The confirm window is set per mode so that it never ends near the moment a scripted agent ends. (An agent
        # that exits just as the window closes can be held as uncertain; that handling in the core is tracked
        # separately.) With --fast an agent ends within about 2 s, well inside 5 s, and is judged by its exit
        # record. At the default pace every activation waits 3 s before its first check, so its agent is always
        # still running, by seconds, when a 0.5 s window ends.
        "launch_confirm_seconds: " + ("5" if fast else "0.5"),
        "load_threshold: 100",
        "stop_grace_seconds: 2",
        "dashboard_refresh_seconds: 2",
    ]) + "\n"


def profile_text(root: str) -> str:
    import yaml
    return yaml.safe_dump({"launch_env": {"GIT_CONFIG_NOSYSTEM": "1",
                                          "GIT_CONFIG_GLOBAL": os.path.join(root, "userhome", ".gitconfig")}},
                          sort_keys=False)


def _emit(stream_name: str, line: str) -> None:
    """Write one line; a closed or broken stdout or stderr never stops the demo or its teardown."""
    stream = getattr(sys, stream_name)
    try:
        stream.write(line + "\n")
        stream.flush()
    except (OSError, ValueError):
        try:
            setattr(sys, stream_name, open(os.devnull, "w"))  # noqa: SIM115 (replaces the broken stream)
        except OSError:
            pass


def _out(line: str) -> None:
    _emit("stdout", line)


def _err(line: str) -> None:
    _emit("stderr", line)


def _test_mode() -> bool:
    return os.environ.get("YGG_TEST_MODE") == "1"


def _test(name: str) -> str | None:
    """A test-mode control, honoured only with YGG_TEST_MODE=1."""
    if not _test_mode():
        return None
    value = os.environ.get(name)
    return value if value not in (None, "") else None


def _hold_points() -> list[str]:
    return [n.strip() for n in (_test("YGG_TEST_DEMO_HOLD_POINTS") or "").split(",") if n.strip()]


def _describe(error: BaseException) -> str:
    text = str(error)
    return type(error).__name__ + (": " + text if text else "")


# ---------------------------------------------------------------- process identity

GONE, EXACT, DIFFERENT, UNKNOWN = "gone", "exact", "different", "unknown"


def _alive(pid: int) -> bool | None:
    """Whether `pid` is a live (non-zombie) process; None when that can't be told."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return None  # OverflowError: a pid outside the system's pid range
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False  # this process's own child, just reaped
    except ChildProcessError:
        pass
    except (OSError, OverflowError):
        return None
    status = None
    try:
        text = Path("/proc/" + str(pid) + "/stat").read_text()
        status = text[text.rindex(")") + 2:].split()[0]
    except (OSError, ValueError, IndexError):
        try:
            result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                                    timeout=10)
            status = result.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            status = None
    if not status:
        # Its state can't be read: ask the kernel again, since a process that just ended reads like this too.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        return None
    return not status.startswith("Z")


def classify(pid, started) -> str:
    """The evidence for a recorded (pid, start identity) pair: `gone` (no live process has the pid), `exact` (the
    live process is the recorded one), `different` (another process has the pid now) or `unknown` (a live pid
    whose start can't be read, a pair with no identity, or a match that is only ambiguous). Unknown is never
    taken for gone."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return GONE
    alive = _alive(pid)
    if alive is False:
        return GONE
    if alive is None or not started:
        return UNKNOWN
    actual = util.process_start(pid)
    if actual is None:
        # No start time: gone if it has ended meanwhile; a live process whose start can't be read stays unknown.
        return GONE if _alive(pid) is False else UNKNOWN
    match = util.start_match(started, actual)
    if match == "exact":
        return EXACT
    return DIFFERENT if match is None else UNKNOWN


def same(pid, started) -> bool:
    """The identity rule: `pid` is alive and its current start identity matches `started` exactly."""
    return classify(pid, started) == EXACT


def send(pid: int, number: int) -> bool:
    """Signal the process group when `pid` leads one, otherwise the process alone (as interlock._signal)."""
    try:
        own_group = os.getpgid(pid) == pid
    except OSError:
        return False
    try:
        if own_group:
            os.killpg(pid, number)
        else:
            os.kill(pid, number)
    except OSError:
        return False
    return True


def _unknown(pid: int, what: str) -> "Unsafe":
    return Unsafe("recorded " + what + " process " + str(pid) + " can't be identified exactly")


GROUPS_FILE = "state/demo-groups.json"


class SignalLog:
    """What a teardown has signalled: whether anything yet (it decides how a recovery refusal is worded), and every
    process group a signalled leader heads (pgid -> the leader's identity). A group is written to
    state/demo-groups.json before the first signal goes to it, so a later recovery can check it for members even
    after this run has ended."""

    def __init__(self, folder):
        self.folder = folder
        self.signalled = False
        self.groups: dict[int, str] = {}
        self.changed: list[int] = []  # pids whose identity changed between selection and the signal
        self.write_error: str | None = None  # a failed write of the group file, reported when teardown ends

    def load(self) -> None:
        """Take in the groups earlier runs listed. A file that exists but is malformed refuses."""
        data = _json(self.folder, GROUPS_FILE)
        if data is MISSING:
            return
        if not isinstance(data, dict):
            raise Unsafe(GROUPS_FILE + " is malformed")
        for key, started in data.items():
            try:
                pgid = int(key)
            except ValueError:
                raise Unsafe(GROUPS_FILE + " is malformed") from None
            if pgid < 2 or not isinstance(started, str):
                raise Unsafe(GROUPS_FILE + " is malformed")
            self.groups.setdefault(pgid, started)

    def note_group(self, pgid: int, started: str) -> None:
        if pgid in self.groups:
            return
        self.groups[pgid] = started
        text = json.dumps({str(key): value for key, value in sorted(self.groups.items())}) + "\n"
        try:
            self.folder.write_state("demo-groups.json", text)
        except OSError as error:
            # The group stays in memory; the caller decides whether to go on (the driver, for its own unreaped
            # children) or refuse (anything else), and the failure is reported when teardown ends.
            self.write_error = GROUPS_FILE + " can't be written (" + _describe(error) + ")"
            raise Unsafe(self.write_error) from None


def signal_exact(pid: int, started, number: int, log: "SignalLog | None" = None) -> bool:
    """Send `number` only while `pid` is still the recorded process, checked right before. Anything but an exact
    match (gone, another process, or unknown) is never signalled; the final check reports what is left. A target
    that leads its own group is signalled as a group, after the group is written down in `log`."""
    if classify(pid, started) != EXACT:
        return False
    try:
        leader = os.getpgid(pid) == pid
    except OSError:
        return False
    if leader and log is not None:
        log.note_group(pid, started)  # written down first
    if classify(pid, started) != EXACT:
        # Checked again right before the signal: a process that changed meanwhile is never signalled, and the
        # final check counts it as unknown.
        if log is not None:
            log.changed.append(pid)
        return False
    sent = send(pid, number)
    if sent and log is not None:
        log.signalled = True
    return sent


def wait_gone(pid: int, started, seconds: float, process=None) -> bool:
    """Wait until the recorded process is gone (or its pid is another process's). False at the deadline, for a
    live process or one that can't be identified; the final check reports it."""
    deadline = time.monotonic() + seconds
    while True:
        if process is not None:
            process.poll()
        if classify(pid, started) in (GONE, DIFFERENT):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def group_members(pgid: int) -> bool | None:
    """Whether process group `pgid` has any member (None when that can't be told)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def busy_groups(groups: dict) -> tuple[list[int], list[int], list[int]]:
    """(groups whose leader is still alive, groups whose leader is gone but members remain, groups whose state
    can't be told). A group whose leader pid now belongs to another process emptied before that pid could be
    reused, so it is dropped. Nothing here ever signals a group."""
    leaders, busy, unknown = [], [], []
    for pgid, started in list(groups.items()):
        state = classify(pgid, started)
        if state == EXACT:
            leaders.append(pgid)
            continue
        if state == DIFFERENT:
            groups.pop(pgid, None)
            continue
        members = group_members(pgid)
        if members is None or state == UNKNOWN:
            unknown.append(pgid)
        elif members:
            busy.append(pgid)
        else:
            groups.pop(pgid, None)
    return leaders, busy, unknown


def _identity_epoch(started) -> float | None:
    text = str(started or "")
    if not text.startswith("s:"):
        return None
    try:
        value = float(text[2:])
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ---------------------------------------------------------------- reading a demo folder

class PathFolder:
    """A demo folder the driver made itself, read by path."""

    def __init__(self, root: Path):
        self.root = root

    def read(self, relative: str) -> str | None:
        """The file's text; None when it does not exist. A file that exists but can't be read refuses."""
        try:
            return (self.root / relative).read_text()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as error:
            raise Unsafe(relative + " can't be read (" + _describe(error) + ")") from None

    def listing(self, relative: str) -> list[str]:
        try:
            return sorted(os.listdir(self.root / relative))
        except OSError:
            return []

    def has(self, relative: str) -> bool:
        return (self.root / relative).is_dir()

    def mtime(self, relative: str) -> float | None:
        try:
            return os.stat(self.root / relative, follow_symlinks=False).st_mtime
        except OSError:
            return None

    def write_state(self, name: str, text: str) -> None:
        target = self.root / "state" / name
        temporary = target.with_name("." + name + "." + str(os.getpid()) + ".tmp")
        _write(temporary, text)
        os.replace(temporary, target)


class BoundFolder:
    """A demo folder opened once and read only through directory descriptors: no path is resolved again after
    the check, and no symlink is ever followed."""

    def __init__(self, folder: str):
        self.folder = folder
        self.fds: dict[str, int] = {}
        self.identities: dict[str, tuple[int, int]] = {}
        self.parent_fd: int | None = None
        self.name = ""

    def close(self) -> None:
        for fd in list(self.fds.values()) + ([self.parent_fd] if self.parent_fd is not None else []):
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds, self.parent_fd = {}, None

    def adopt(self, other: "BoundFolder") -> None:
        """Take over a later check of the same folder (its identities were compared by the caller)."""
        self.close()
        self.fds, self.identities, self.parent_fd, self.name = other.fds, other.identities, other.parent_fd, \
            other.name
        other.fds, other.parent_fd = {}, None

    @property
    def root_fd(self) -> int:
        return self.fds[""]

    def _split(self, relative: str) -> tuple[int | None, str]:
        folder, _, name = relative.rpartition("/")
        return self.fds.get(folder), name

    def read(self, relative: str) -> str | None:
        """The file's text; None when it does not exist. A file that exists but can't be read, or is not a regular
        file, refuses."""
        folder_fd, name = self._split(relative)
        if folder_fd is None:
            return None
        try:
            # Non-blocking, so a FIFO put here after the check can't hang the read; fstat refuses it.
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=folder_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise Unsafe(relative + " can't be read (" + _describe(error) + ")") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Unsafe(relative + " is not a regular file")
            with os.fdopen(fd, "r", closefd=False) as handle:
                return handle.read()
        except (OSError, UnicodeDecodeError) as error:
            raise Unsafe(relative + " can't be read (" + _describe(error) + ")") from None
        finally:
            os.close(fd)

    def listing(self, relative: str) -> list[str]:
        fd = self.fds.get(relative)
        if fd is None:
            return []
        try:
            return sorted(os.listdir(fd))
        except OSError:
            return []

    def has(self, relative: str) -> bool:
        return relative in self.fds

    def mtime(self, relative: str) -> float | None:
        folder_fd, name = self._split(relative)
        if folder_fd is None:
            return None
        try:
            return os.stat(name, dir_fd=folder_fd, follow_symlinks=False).st_mtime
        except OSError:
            return None

    def create(self, name: str, text: str, folder: str = "") -> None:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
                     dir_fd=self.fds[folder])
        with os.fdopen(fd, "w") as handle:
            handle.write(text)

    def write_state(self, name: str, text: str) -> None:
        if "state" not in self.fds:
            raise OSError("the folder has no state folder")
        temporary = "." + name + "." + str(os.getpid()) + ".tmp"
        self.create(temporary, text, "state")
        os.replace(temporary, name, src_dir_fd=self.fds["state"], dst_dir_fd=self.fds["state"])

    def replace(self, name: str, text: str) -> None:
        temporary = "." + name + "." + str(os.getpid()) + ".tmp"
        self.create(temporary, text)
        os.replace(temporary, name, src_dir_fd=self.root_fd, dst_dir_fd=self.root_fd)

    def remove(self) -> list[str]:
        """Remove the checked folder itself, by its entry in the temp folder, only while that entry is still it.
        Returns the removal errors (empty when the folder is gone)."""
        found = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        if (found.st_dev, found.st_ino) != self.identities[""]:
            raise Unsafe("it changed while it was cleaned up")
        return _rmtree(self.name, dir_fd=self.parent_fd)


def bind(folder: str) -> BoundFolder:
    """Check `folder` and open it: a real folder, directly inside the user's temp folder, owned by the user with
    mode 700; and every folder recovery reads or writes (state, state.safety, tracker, userhome and everything
    below them) a real folder on the same file system, with no symlink, device or hard-linked file anywhere."""
    # Check the folder's own entry, not what a trailing slash or a "." would resolve it to: "link/" or "link/."
    # makes lstat follow a symlink, so the spelling is normalised first.
    path = os.path.normpath(folder)
    if os.path.basename(path) in ("", ".", ".."):
        raise Unsafe("it is not a folder name")
    try:
        info = os.lstat(path)
    except OSError:
        raise Unsafe("it does not exist") from None
    if stat.S_ISLNK(info.st_mode):
        raise Unsafe("it is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise Unsafe("it is not a folder")
    real = os.path.realpath(path)
    tmp = os.path.realpath(tempfile.gettempdir())
    if os.path.dirname(real) != tmp:
        raise Unsafe("it is not directly inside " + tmp)
    bound = BoundFolder(folder)
    bound.name = os.path.basename(real)
    try:
        bound.parent_fd = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY)
        root_fd = os.open(bound.name, DIRECTORY, dir_fd=bound.parent_fd)
        bound.fds[""] = root_fd
        root = os.fstat(root_fd)
        if (root.st_dev, root.st_ino) != (info.st_dev, info.st_ino):
            raise Unsafe("it changed while it was checked")
        bound.identities[""] = (root.st_dev, root.st_ino)
        if root.st_uid != os.getuid():
            raise Unsafe("it is not owned by you")
        if stat.S_IMODE(root.st_mode) != 0o700:
            raise Unsafe("its mode is " + oct(stat.S_IMODE(root.st_mode))[2:] + ", not 700")
        for name in TOP_FILES:
            try:
                found = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            _check_file(name, found)
        for name in CHECKED_FOLDERS:
            try:
                os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue  # not made yet (the driver died first): nothing of it is read
            _walk(bound, root_fd, name, name, root.st_dev)
    except Unsafe:
        bound.close()
        raise
    except OSError as error:
        bound.close()
        raise Unsafe("it could not be checked (" + _describe(error) + ")") from None
    return bound


def _check_file(relative: str, found: os.stat_result) -> None:
    if stat.S_ISLNK(found.st_mode):
        raise Unsafe(relative + " is a symlink")
    if not stat.S_ISREG(found.st_mode):
        raise Unsafe(relative + " is not a regular file")  # a FIFO, socket or device
    if found.st_nlink > 1:
        raise Unsafe(relative + " has other hard links")


def _walk(bound: BoundFolder, parent_fd: int, name: str, relative: str, device: int) -> None:
    found = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(found.st_mode):
        raise Unsafe(relative + " is a symlink")
    if not stat.S_ISDIR(found.st_mode):
        raise Unsafe(relative + " is not a folder")
    if found.st_dev != device:
        raise Unsafe(relative + " is on another file system")
    fd = os.open(name, DIRECTORY, dir_fd=parent_fd)
    bound.fds[relative] = fd
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (found.st_dev, found.st_ino):
        raise Unsafe(relative + " changed while it was checked")
    bound.identities[relative] = (opened.st_dev, opened.st_ino)
    for entry in sorted(os.listdir(fd)):
        child = os.stat(entry, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(child.st_mode):
            _walk(bound, fd, entry, relative + "/" + entry, device)
        else:
            _check_file(relative + "/" + entry, child)


# ---------------------------------------------------------------- recorded processes

MISSING = object()  # what _json returns for a file that does not exist (JSON `null` reads as None)


def _json(folder, relative: str):
    """A JSON file's value; MISSING when the file does not exist. Unreadable or malformed refuses."""
    text = folder.read(relative)
    if text is None:
        return MISSING
    try:
        return json.loads(text)
    except ValueError:
        raise Unsafe(relative + " is malformed") from None


def _pid_file(folder, relative: str):
    """(pid, identity) from `<pid> <identity>`; None when the file does not exist. Anything else refuses."""
    text = folder.read(relative)
    if text is None:
        return None
    words = text.split()
    if len(words) != 2:
        raise Unsafe(relative + " is malformed")
    try:
        pid = int(words[0])
    except ValueError:
        raise Unsafe(relative + " is malformed") from None
    if pid < 2:
        raise Unsafe(relative + " is malformed")
    return pid, words[1]


def _record_pid(data: dict, key: str, relative: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if _int(value) is None or value < 2:
        raise Unsafe(relative + " has a " + key + " that is not a process id")
    return value


def recorded_processes(folder) -> list[tuple[int, str | None, str]]:
    """Every (pid, start identity, what) a demo folder recorded. Strict: a record that exists but is unreadable
    or malformed refuses rather than being skipped."""
    if isinstance(folder, Path):
        folder = PathFolder(folder)
    found = []
    for name, what in (("conductor.pid", "conductor"), ("dashboard.pid", "dashboard server")):
        pair = _pid_file(folder, name)
        if pair:
            found.append((pair[0], pair[1], what))
    for name in folder.listing("state/activations"):
        if not name.endswith(".json"):
            continue
        relative = "state/activations/" + name
        data = _json(folder, relative)
        if data is MISSING:
            continue  # gone since the listing
        if not isinstance(data, dict):
            raise Unsafe(relative + " is malformed")
        for pid_key, start_keys, what in (("launcher_pid", ("launcher_started_at",), "launcher"),
                                          ("supervisor_pid", ("supervisor_started_at",), "supervisor"),
                                          ("pid", ("process_start_identity", "process_started_at"), "agent")):
            pid = _record_pid(data, pid_key, relative)
            if pid is None:
                continue
            started = next((data.get(key) for key in start_keys if data.get(key)), None)
            found.append((pid, started, what))
    for relative, data in _merge_checks(folder):
        if data.get("state") != "running":
            continue
        pid = _record_pid(data, "pid", relative)
        if pid is None:
            raise Unsafe(relative + " is running with no pid")
        found.append((pid, data.get("process_started_at") or None, "merge check"))
    return found


def _merge_checks(folder) -> list[tuple[str, dict]]:
    records = []
    for name in folder.listing("state/merge-checks"):
        if not name.endswith(".json") or name.endswith(".result.json"):
            continue
        relative = "state/merge-checks/" + name
        data = _json(folder, relative)
        if data is MISSING:
            continue
        if not isinstance(data, dict):
            raise Unsafe(relative + " is malformed")
        if data.get("state") == "starting" and data.get("pid") is not None:
            raise Unsafe(relative + " is starting but holds a pid")
        if data.get("state") == "running" and data.get("pid") is None:
            raise Unsafe(relative + " is running with no pid")
        records.append((relative, data))
    return records


def starting_checks(folder) -> list[str]:
    """Merge checks whose record is claimed but whose job is not recorded yet: teardown can't vouch for them, so
    every one of them makes teardown and recovery refuse."""
    if isinstance(folder, Path):
        folder = PathFolder(folder)
    return [str(data.get("id") or "?") for _, data in _merge_checks(folder) if data.get("state") == "starting"]


def unfinished_without_agent(folder) -> list[str]:
    """Activations that have not ended and record no agent pid: what they may have started can't be accounted
    for. A pid that is present but not a process id refuses when the records are read."""
    if isinstance(folder, Path):
        folder = PathFolder(folder)
    found = []
    for name in folder.listing("state/activations"):
        if not name.endswith(".json"):
            continue
        relative = "state/activations/" + name
        data = _json(folder, relative)
        if data is MISSING:
            continue
        if not isinstance(data, dict):
            raise Unsafe(relative + " is malformed")
        if data.get("exited_at") is not None or data.get("terminal"):
            continue
        if _record_pid(data, "pid", relative) is None:
            found.append(str(data.get("activation") or name[:-5]))
    return found


# ---------------------------------------------------------------- folders

def _rmtree(path, dir_fd: int | None = None) -> list[str]:
    """Remove a demo folder with shutil.rmtree, which removes through directory descriptors and never follows a
    symlink: by path for the driver's own folder, through `dir_fd` for a folder `--cleanup` checked. Nothing is
    chmodded and nothing is retried. Returns every error, plus one when the folder is still there afterwards; an
    empty list means the folder is gone."""
    errors: list[str] = []

    def note(_function, name, error) -> None:
        errors.append(str(name) + ": " + _describe(error))
    try:
        if dir_fd is not None:
            shutil.rmtree(path, dir_fd=dir_fd, onexc=note)
        else:
            shutil.rmtree(path, onexc=note)
    except OSError as error:
        errors.append(str(path) + ": " + _describe(error))
    try:
        os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if not errors:
            errors.append(str(path) + ": still there after removal")
    except FileNotFoundError:
        pass
    except OSError as error:
        errors.append(str(path) + ": " + _describe(error))
    return errors


def removal_failed(shown: str, errors: list[str]) -> str:
    """The failure message for a folder that could not be fully removed. Removal has usually deleted most of it
    by then (its marker too, so `--cleanup` can't take it over): it names the folder, says it was only partly
    removed and must be removed by hand, and lists every error, each on its own line."""
    return shown + " was only partly removed; remove it by hand. Errors:\n  " + "\n  ".join(errors)


def partly_removed(shown: str) -> str:
    """The last stdout line after a failed removal (instead of `demo: kept`, which would promise a whole folder)."""
    return "demo: partly removed " + shown + "; remove it by hand"


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)


MARKER_LIMIT = 64 * 1024  # bytes; a real marker is a few hundred
PID_MAX = 2 ** 31 - 1  # the largest pid any supported system hands out
DEMO_NAME = re.compile(r"ygg-demo-[A-Za-z0-9_]+")  # the names tempfile.mkdtemp makes with that prefix


def _marker_text(folder_fd: int) -> str | None:
    """demo-marker.json's text, opened through the folder's descriptor without following a symlink and without
    blocking on a FIFO or device. None when it is missing, not a regular file owned by this user, larger than
    MARKER_LIMIT or not text."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open("demo-marker.json", flags, dir_fd=folder_fd)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        data = b""
        while len(data) <= MARKER_LIMIT:
            chunk = os.read(descriptor, MARKER_LIMIT + 1 - len(data))
            if not chunk:
                break
            data += chunk
        return data.decode() if len(data) <= MARKER_LIMIT else None
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(descriptor)


def _marker(root: Path) -> dict | None:
    """The folder's marker; None when the folder is not a real directory owned by this user, or the marker can't
    be read safely or is not a JSON object: not a demo this run can judge."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        folder_fd = os.open(root, flags)
    except OSError:
        return None
    try:
        info = os.fstat(folder_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return None
        text = _marker_text(folder_fd)
    except OSError:
        return None
    finally:
        os.close(folder_fd)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def left_behind(tmp: str) -> list[str]:
    """`ygg-demo-*` folders in `tmp` whose driver is gone without finishing teardown. Only real directories owned
    by this user with mode 700, with a name tempfile.mkdtemp could have made and a marker naming a valid driver
    pid, count."""
    found = []
    try:
        names = sorted(os.listdir(tmp))
    except OSError:
        return found
    for name in names:
        if not DEMO_NAME.fullmatch(name):
            continue
        folder = os.path.join(tmp, name)
        try:
            info = os.lstat(folder)
        except OSError:
            continue
        # The same top-level checks `ygg demo --cleanup` makes on the folder itself (a real directory, this user's,
        # mode 700). Cleanup also walks the folder's contents and checks the recorded processes' ages, so it can
        # still refuse a folder this scan lists.
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            continue
        marker = _marker(Path(folder))
        if not marker or marker.get("root") != os.path.realpath(folder) or marker.get("ended") is not False:
            continue
        driver = _int(marker.get("driver_pid"))
        if driver is None or not 2 <= driver <= PID_MAX:
            continue  # no pid a real driver could have had: not a demo this scan can judge
        try:
            if same(driver, marker.get("driver_started")):
                continue
        except (OSError, OverflowError, ValueError, TypeError):
            continue
        found.append(folder)
    return found


def can_open_browser() -> bool:
    """Whether the dashboard may open in a browser: stdout is a terminal, and on Linux there is a display. In an ssh
    session without one, a console browser would take the terminal and stall the demo, so only the line printed
    above names the dashboard."""
    try:
        if not sys.stdout.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return True


def _sh(text: str) -> str:
    return shlex.quote(text)


def shim_text(python: str, module: str) -> str:
    return "#!/bin/sh\nexec " + _sh(python) + " -I -m " + module + ' "$@"\n'


def blocked_text(python: str, record: str, name: str) -> str:
    code = ("import json, sys; open(sys.argv[1], 'a').write(json.dumps({'program': sys.argv[2], "
            "'argv': sys.argv[3:]}) + '\\n')")
    return "#!/bin/sh\n" + _sh(python) + " -I -c " + _sh(code) + " " + _sh(record) + " " + _sh(name) + \
        ' "$@"\nexit 1\n'


# ---------------------------------------------------------------- the driver

class Driver:
    def __init__(self, fast: bool, keep: bool, no_open: bool):
        self.fast, self.keep, self.no_open = fast, keep, no_open
        self.root: Path | None = None
        self.shown_root = ""
        self.env: dict = {}
        self.signal_number: int | None = None
        self.deferring = False
        self.tearing = False
        self.in_flow = False
        self.children: list = []  # every child's handle, kept as soon as Popen returns
        self.signals: SignalLog | None = None  # made at teardown: what it signalled, its groups
        self.short = None  # (Popen, start identity) of the running short command
        self.conductor = None  # (Popen, pid, start identity)
        self.dashboard = None
        self.started = time.monotonic()
        self.log = None
        self.lines_printed = 0
        self.agents_checked = False  # set once the check at the seventh line found nothing
        self.dashboard_line: str | None = None
        self.dashboard_target: str | None = None
        self.dashboard_printed = False
        self.ready: dict[int, bool] = {k: False for k in NARRATION}
        self.waiting_since: dict[int, float] = {}  # when each narration step's wait began
        self.fail_at = None
        fault = _test("YGG_TEST_DEMO_FAIL_AT")
        if fault is not None:
            try:
                self.fail_at = int(fault)
            except ValueError:
                self.fail_at = None
        self.deadline = script.DEADLINE_FAST if fast else script.DEADLINE_DEFAULT

    def phase(self) -> str:
        return "step " + str(self.lines_printed + 1) if self.in_flow else "setup"

    # -------------------------------------------------- signals

    def _on_signal(self, signum, _frame):
        if self.signal_number is None:
            self.signal_number = signum
        if self.deferring or self.tearing:
            return
        raise Interrupted()

    def _after_deferral(self) -> None:
        if self.signal_number is not None and not self.tearing:
            raise Interrupted()

    def _enter_teardown(self) -> None:
        """From here a signal is only noted; the loop makes sure a signal landing on the assignment itself
        cannot skip it."""
        while True:
            try:
                self.tearing = True
                return
            except Interrupted:
                continue

    # -------------------------------------------------- children

    def _logged(self, text: str) -> None:
        if self.log is not None and text:
            try:
                self.log.write(text if text.endswith("\n") else text + "\n")
                self.log.flush()
            except (OSError, ValueError):
                pass

    def start(self, argv: list[str], *, cwd: Path, stdout=None, stderr=None):
        """Start a child in its own session with the demo environment; its handle is kept at once. Returns
        (process, start identity)."""
        process = subprocess.Popen(argv, cwd=str(cwd), env=self.env, stdin=subprocess.DEVNULL,
                                   stdout=stdout if stdout is not None else subprocess.PIPE,
                                   stderr=stderr if stderr is not None else subprocess.PIPE,
                                   start_new_session=True, text=True)
        self.children.append(process)
        return process, util.process_start(process.pid)

    def short_command(self, argv: list[str], *, cwd: Path | None = None, timeout: int = SHORT_TIMEOUT,
                      where: str = "setup") -> subprocess.CompletedProcess:
        # Signals are deferred from just before the start until the command is recorded.
        self.deferring = True
        try:
            process, started = self.start(argv, cwd=cwd or self.root)
            self.short = (process, started)
        finally:
            self.deferring = False
        self._after_deferral()
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._end_short()
            raise Failure(where, " ".join(argv[1:3]) + " did not finish in " + str(timeout) + " s") from None
        self.short = None
        self._logged("$ " + " ".join(argv) + " -> " + str(process.returncode) + "\n" + (out or "") + (err or ""))
        return subprocess.CompletedProcess(argv, process.returncode, out, err)

    def ygg(self, *args: str, where: str = "setup", timeout: int = SHORT_TIMEOUT) -> subprocess.CompletedProcess:
        return self.short_command([str(self.root / "bin" / "ygg"), *args], where=where, timeout=timeout)

    def ygg_ok(self, *args: str, where: str) -> subprocess.CompletedProcess:
        result = self.ygg(*args, where=where)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise Failure(where, "ygg " + " ".join(args[:2]) + " exited " + str(result.returncode) +
                          (": " + detail[-1] if detail else ""))
        return result

    def _end_short(self) -> None:
        """Teardown step 0: end the running short command through its handle (an unreaped child: its pid can't
        have been reused), as a group, recorded for the final check."""
        if self.short is None:
            return
        process, started = self.short
        if process.poll() is None:
            self._note_own_group(process.pid, started)
            send(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                send(process.pid, signal.SIGKILL)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        try:
            process.communicate(timeout=1)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        self.short = None

    def _kill_own(self, process) -> None:
        """Stop a child of this process through its handle: while it is unreaped its pid can't be reused."""
        if process.poll() is not None:
            return
        self._note_own_group(process.pid, util.process_start(process.pid))
        try:
            send(process.pid, signal.SIGKILL)
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except (subprocess.TimeoutExpired, ChildProcessError):
            pass

    def _note_own_group(self, pid: int, started) -> None:
        """Write down the group of an unreaped child before it is signalled (its pid can't have been reused)."""
        if not started or self.root is None:
            return
        if self.signals is None:
            self.signals = SignalLog(PathFolder(self.root))
        try:
            if os.getpgid(pid) == pid:
                self.signals.note_group(pid, started)
        except (OSError, Unsafe):
            pass

    def start_long(self, argv: list[str], pid_file: str, *, stdout) -> tuple:
        """Start a long-lived child and record `<pid> <start identity>` before anything else; signals are
        deferred (noted, never masked) from just before the start until the pid file is written. If the
        identity can't be read or the pid file can't be written, the child is ended through its handle."""
        self.deferring = True
        try:
            process, started = self.start(argv, cwd=self.root, stdout=stdout, stderr=self.log)
            try:
                if not started:
                    raise Failure(self.phase(), "could not read the start identity of " + " ".join(argv[1:3]))
                _write(self.root / pid_file, str(process.pid) + " " + started + "\n")
            except BaseException:
                self._kill_own(process)
                raise
        finally:
            self.deferring = False
        return process, process.pid, started

    # -------------------------------------------------- setup

    def build_env(self, git_dir: str) -> dict:
        root = str(self.root)
        entries = []
        first = _test("YGG_TEST_DEMO_PATH_FIRST")
        if first:
            entries.append(first)
        entries += [os.path.join(root, "bin"), git_dir, *SYSTEM_PATH]
        env = {
            "PATH": os.pathsep.join(entries),
            "HOME": os.path.join(root, "userhome"),
            "TMPDIR": os.path.join(root, "tmp"),
            "LANG": "en_US.UTF-8",
            "YGG_CONFIG": os.path.join(root, "config.yaml"),
            "YGG_PROFILE": os.path.join(root, "profile.yaml"),
            "YGG_STATE_ROOT": os.path.join(root, "state"),
            "YGG_FILES_ROOT": os.path.join(root, "tracker"),
            "YGG_TRACKER": "files",
            "YGG_CLAUDE_BIN": os.path.join(root, "bin", "claude"),
            "YGG_CODEX_BIN": os.path.join(root, "bin", "codex"),
            "YGG_DEMO_ROOT": root,
            "YGG_DEMO_PACE": "0" if self.fast else str(script.PACE_SECONDS),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.path.join(root, "userhome", ".gitconfig"),
        }
        if _test_mode():
            env["YGG_TEST_MODE"] = "1"
            for name in ("YGG_RELEASE_FILE", "YGG_TEST_RELEASE_OFF"):
                value = _test(name)
                if value:
                    env[name] = value
            agent_points = [point for point in _hold_points() if point.startswith("agent-")]
            if agent_points and _test("YGG_TEST_DEMO_HOLD"):
                env["YGG_DEMO_HOLD"] = _test("YGG_TEST_DEMO_HOLD")
                env["YGG_DEMO_HOLD_POINTS"] = ",".join(agent_points)
        return env

    def write_shims(self) -> None:
        python = sys.executable
        bin_dir = self.root / "bin"
        for name, module in (("claude", "yggdrasil.demo.agent"), ("codex", "yggdrasil.demo.agent"),
                             ("ygg", "yggdrasil")):
            _write(bin_dir / name, shim_text(python, module), 0o700)
        record = str(self.root / "blocked-calls.jsonl")
        for name in BLOCKED_PROGRAMS:
            _write(bin_dir / name, blocked_text(python, record, name), 0o700)

    def check_shims(self) -> None:
        for name in ("claude", "codex", "ygg"):
            found = shutil.which(name, path=self.env["PATH"])
            if found != str(self.root / "bin" / name):
                raise Failure("setup", name + " resolves to " + str(found) + ", outside the demo's bin folder")

    def setup(self) -> None:
        git = shutil.which("git")
        if not git:
            raise Failure("setup-early", "git not found on PATH")
        for folder in left_behind(tempfile.gettempdir()):
            _out("demo: an earlier demo was left in " + _sh(folder) + "; to stop it and remove it, run: "
                 "ygg demo --cleanup " + _sh(folder))
        made = tempfile.mkdtemp(prefix="ygg-demo-")
        self.root = Path(made)
        self.shown_root = made
        _write(self.root / "demo-marker.json", json.dumps({
            "root": os.path.realpath(made), "driver_pid": os.getpid(),
            "driver_started": util.process_start(os.getpid()), "ended": False}) + "\n")
        for name in ("state", "tracker", "home", "userhome", "tmp", "bin"):
            (self.root / name).mkdir(mode=0o700)
        self.log = open(self.root / "demo.log", "a")  # noqa: SIM115 (open for the whole run)
        _write(self.root / "userhome" / ".gitconfig", GITCONFIG)
        (self.root / "userhome" / ".codex").mkdir(mode=0o700)
        _write(self.root / "userhome" / ".codex" / "config.toml", CODEX_CONFIG)
        _write(self.root / "config.yaml", config_text(fast=self.fast))
        _write(self.root / "profile.yaml", profile_text(made))
        _write(self.root / "brief.md", script.BRIEF)
        self.env = self.build_env(os.path.dirname(git))
        _write(self.root / "demo-env.json", json.dumps(self.env, indent=2, sort_keys=True) + "\n")
        self.write_shims()
        self.check_shims()
        home = self.root / "home"
        self.git(["git", "init", "-q", "-b", "main", "--template="], home)
        _write(home / "README.md", script.README, 0o644)
        self.git(["git", "add", "README.md"], home)
        self.git(["git", "commit", "-q", "-m", "Start the demo home"], home)

    def git(self, argv: list[str], cwd: Path) -> None:
        result = self.short_command(argv, cwd=cwd)
        if result.returncode != 0:
            raise Failure("setup", " ".join(argv[:2]) + " exited " + str(result.returncode) + ": " +
                          (result.stderr or "").strip()[-300:])

    # -------------------------------------------------- narration

    def _check_fault(self, step: int) -> None:
        if self.fail_at == step:
            raise Failure("step " + str(step), "test fault")

    def mark_ready(self, number: int) -> None:
        if not self.ready[number]:
            self.ready[number] = True
            # The next step's wait is timed from here, never reset by other changes.
            self.waiting_since.setdefault(number + 1, time.monotonic())

    def flush_lines(self) -> None:
        """Print every narration line whose condition holds, in order, no earlier than its pacing floor."""
        while self.lines_printed < 7:
            number = self.lines_printed + 1
            if not self.ready[number]:
                return
            if not self.fast and time.monotonic() - self.started < script.LINE_FLOOR_SECONDS * number:
                return
            _out("demo: " + str(number) + "/7 " + NARRATION[number])
            self.lines_printed = number
            if number == 1:
                self.show_dashboard()
            self._check_fault(number + 1)

    def check_deadline(self) -> None:
        """Each wait is timed from its own start: the first step whose condition does not hold yet fails once
        it has waited longer than the deadline."""
        pending = next((n for n in range(2, 8) if not self.ready[n]), None)
        if pending is None:
            return
        since = self.waiting_since.get(pending)
        if since is not None and time.monotonic() - since > self.deadline:
            raise Failure("step " + str(pending), "waited " + str(self.deadline) + " s for " + WAITING_FOR[pending])

    def show_dashboard(self) -> None:
        if self.dashboard_line is None or self.dashboard_printed:
            return
        _out(self.dashboard_line)
        self.dashboard_printed = True
        if not self.no_open and self.dashboard_target and can_open_browser():
            try:
                import webbrowser
                webbrowser.open(self.dashboard_target)
            except Exception:  # noqa: BLE001 (a browser that can't open never stops the demo)
                pass

    # -------------------------------------------------- flow

    def status(self, where: str) -> dict:
        result = self.ygg("status", "--json", where=where)
        try:
            data = json.loads(result.stdout)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def reviews(self) -> dict:
        try:
            data = _json(PathFolder(self.root), "tracker/_ygg/reviews.json")
        except Unsafe:
            return {}  # caught mid-write: the next poll reads it
        reviews = data.get("reviews") if isinstance(data, dict) else None
        return reviews if isinstance(reviews, dict) else {}

    def hold(self, point: str, where: str) -> None:
        folder = _test("YGG_TEST_DEMO_HOLD")
        if point not in _hold_points() or not folder:
            return
        if release.on("dashboard"):
            self.ygg("dashboard", script.PROGRAMME, "--no-open", where=where)
        hold = Path(folder)
        (hold / ("held-" + point)).write_text("")
        deadline = time.monotonic() + script.HOLD_SECONDS
        while not (hold / ("go-" + point)).exists():
            if time.monotonic() > deadline:
                raise Failure(where, "hold " + point + " was not released")
            if self.conductor is not None and self.conductor[0].poll() is not None:
                raise Failure(where, "the conductor stopped (exit " + str(self.conductor[0].returncode) + ")")
            time.sleep(0.1)

    @staticmethod
    def first_line(stream, seconds: float) -> str | None:
        """The first line of `stream`, or None when none comes within `seconds` (read on a helper thread)."""
        lines: queue.Queue = queue.Queue()

        def read() -> None:
            try:
                lines.put(stream.readline())
            except (OSError, ValueError):
                lines.put("")
        threading.Thread(target=read, daemon=True).start()
        try:
            return lines.get(timeout=seconds)
        except queue.Empty:
            return None

    def setup_dashboard(self) -> None:
        if not release.on("dashboard"):
            self.dashboard_line = "demo: dashboard not in this release"
            return
        self.ygg_ok("dashboard", script.PROGRAMME, "--no-open", where="step 2")
        page = self.root / "state" / "dashboard" / (script.PROGRAMME + ".html")
        if release.on("dashboard_serve"):
            process, pid, started = self.start_long(
                [str(self.root / "bin" / "ygg"), "dashboard", "serve", script.PROGRAMME, "--port", "0"],
                "dashboard.pid", stdout=subprocess.PIPE)
            self.dashboard = (process, pid, started)
            self._after_deferral()
            line = self.first_line(process.stdout, DASHBOARD_START_SECONDS)
            if line is None:
                raise Failure("step 2", "the dashboard server printed no address within " +
                              str(DASHBOARD_START_SECONDS) + " s")
            if not line.startswith("Serving http://127.0.0.1:"):
                raise Failure("step 2", "the dashboard server did not start")
            url = line.split()[1]
            self.dashboard_line = "demo: dashboard " + url + " (refreshes every 2 s)"
            self.dashboard_target = url
        else:
            self.dashboard_line = "demo: dashboard file " + str(page) + " (refreshes every 2 s)"
            self.dashboard_target = page.as_uri()

    def flow(self) -> None:
        self.in_flow = True
        self._check_fault(1)
        self.ygg_ok("programme", "create", script.PROGRAMME, "--domain", "demo", "--home", str(self.root / "home"),
                    "--kind", "git-repo", where="step 1")
        self.ygg_ok("project", "create", script.PROJECT, "--programme", script.PROGRAMME, "--pm", "pm1",
                    where="step 1")
        self.ygg_ok("node", "add", "pm1tl1", "--role", "tl", "--parent", "pm1", where="step 1")
        self.ygg_ok("node", "add", "pm1su1", "--role", "su", "--parent", "pm1", "--pairs-with", "pm1tl1",
                    where="step 1")
        self.ygg_ok("agents", "allow-minimal", "--yes", where="step 1")
        self.ygg_ok("tracker", "write", "--as", "pm1", "--project", script.PROJECT, "--kind", "brief",
                    "--body-file", str(self.root / "brief.md"), where="step 1")
        self.mark_ready(1)
        self.setup_dashboard()
        self.flush_lines()
        process, pid, started = self.start_long([str(self.root / "bin" / "ygg"), "conductor", "run"],
                                                "conductor.pid", stdout=self.log)
        self.conductor = (process, pid, started)
        self._after_deferral()
        self.waiting_since[2] = time.monotonic()  # the plan's wait starts with the conductor
        approved_stages: set = set()
        pid_approved = merged = False
        while True:
            where = self.phase()
            if process.poll() is not None:
                raise Failure(where, "the conductor stopped (exit " + str(process.returncode) + ")")
            current = self.status(where)
            projects = {p.get("id"): p for p in current.get("projects") or []}
            project = projects.get(script.PROJECT) or {}
            state_name = project.get("state")
            stage = project.get("stage") or {}
            reviews = self.reviews()
            # The driver acts only once the lines before its action have printed; until then it keeps polling, so
            # no wait here blocks the loop or escapes the deadline.
            if not pid_approved and state_name == "pid-ready" and self.lines_printed >= 1:
                self.hold("before-plan-approval", "step 2")
                self.ygg_ok("pid", "approve", script.PROJECT, where="step 2")
                pid_approved = True
                self.mark_ready(2)
            newest = {}
            for _review_id, record in sorted(reviews.items()):
                if isinstance(record, dict) and record.get("package") in ("WP-1", "WP-2"):
                    newest[record["package"]] = record
            if "WP-1" in newest:
                self.mark_ready(3)
            if "WP-2" in newest and self.ready[3]:
                self.mark_ready(4)
            if self.ready[4] and len(newest) == 2 and all(r.get("state") == "approved" for r in newest.values()):
                self.mark_ready(5)
            if stage.get("state") == "awaiting-approval" and stage.get("number") not in approved_stages:
                self.ygg_ok("stage", "approve", script.PROJECT, where=where)
                approved_stages.add(stage.get("number"))
            if not merged and state_name == "ready-to-merge" and self.lines_printed >= 5:
                self.hold("before-merge-main", "step 6")
                self.ygg_ok("merge", "main", script.PROJECT, where="step 6")
                merged = True
                self.mark_ready(6)
            if merged and state_name == "closed":
                self.mark_ready(7)
            self.flush_lines()
            if self.lines_printed == 7:
                self.check_agents()
                return
            self.check_deadline()
            time.sleep(1)

    def check_agents(self, seconds: float = AGENTS_END_SECONDS) -> None:
        """At the seventh line, before anything is stopped: wait, a bounded time, for every agent the demo ran to
        end (an owed message may still start one near the end), then fail at once on anything agent_findings
        reports. This is the early word; the demo's success also needs the second read after the stop."""
        deadline = time.monotonic() + seconds
        while True:
            problems, still_open = agent_findings(self.root, final=False)
            if not still_open or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        if problems:
            raise Failure("step 7", NOT_CLEAN + "; ".join(problems))
        self.agents_checked = True

    # -------------------------------------------------- the run

    def tidy_worktrees(self) -> None:
        """After a finished run, with every process stopped: remove the project's linked worktrees (only those
        under the home's .ygg-worktrees folder) with the driver's own git, so a kept home shows its branches as
        plain branches that `git branch` lists without a worktree mark."""
        home = self.root / "home"
        base = os.path.realpath(home) + "/.ygg-worktrees"
        try:
            if not stat.S_ISDIR(os.lstat(base).st_mode):
                return  # not a real folder (a symlink or a file): nothing is removed
        except OSError:
            return
        allowed = base + "/"
        try:
            listing = self.short_command(["git", "worktree", "list", "--porcelain"], cwd=home, where="cleanup")
            for line in listing.stdout.splitlines():
                path = line[len("worktree "):] if line.startswith("worktree ") else None
                if path and os.path.realpath(path).startswith(allowed):
                    self.short_command(["git", "worktree", "remove", "--force", "--force", path], cwd=home,
                                       where="cleanup")
            self.short_command(["git", "worktree", "prune"], cwd=home, where="cleanup")
        except (Failure, OSError):
            pass

    def finish_marker(self) -> None:
        marker = _marker(self.root) or {}
        marker["ended"] = True
        try:
            _write(self.root / "demo-marker.json", json.dumps(marker) + "\n")
        except OSError:
            pass

    def signal_code(self) -> int | None:
        return 128 + int(self.signal_number) if self.signal_number is not None else None

    def run(self) -> int:
        failure: Failure | None = None
        escaping: BaseException | None = None
        for number in SIGNALS:
            signal.signal(number, self._on_signal)
        try:
            try:
                self.setup()
                self.flow()
            except Failure as error:
                failure = error
            except Interrupted:
                pass
            except Exception as error:  # noqa: BLE001 (any fault still ends in teardown)
                failure = Failure(self.phase(), _describe(error))
            except BaseException as error:  # noqa: BLE001 (teardown first, then it goes on)
                escaping = error
                failure = Failure(self.phase(), _describe(error))
            finally:
                self._enter_teardown()
        except Interrupted:
            pass  # a signal that landed in the handlers above
        code = self.finish(failure)
        if escaping is not None:
            raise escaping
        return code

    def teardown(self) -> str | None:
        """Steps 0 to 4, then the driver's own children through their handles, then the final check over every
        group it signalled and every child it owns. Returns the failure reason."""
        folder = PathFolder(self.root)
        if self.signals is None:
            self.signals = SignalLog(folder)
        try:
            self._end_short()
        except Exception:  # noqa: BLE001, S110 (step 0 is best effort; steps 1 to 4 decide)
            pass
        began = time.monotonic()

        def trace(text: str) -> None:
            self._logged("teardown %.1f s: %s" % (time.monotonic() - began, text))
        try:
            reason = stop_everything(folder, self.env, conductor=self.conductor,
                                     ran_conductor=self.conductor is not None, log=self.signals,
                                     owned=self.children, trace=trace)
        except Unsafe as error:
            reason = str(error)
        except Exception as error:  # noqa: BLE001 (a teardown fault fails the run; it never hides)
            reason = "teardown stopped: " + _describe(error)
        for process in self.children:
            try:
                self._kill_own(process)
            except Exception:  # noqa: BLE001, S110
                pass
        trace("own children ended")
        if reason is None:
            try:
                reason = settle(None, self.signals.groups, owned=self.children, changed=self.signals.changed,
                                trace=trace)
            except Unsafe as error:
                reason = str(error)
        trace("final check done (" + (reason or "all gone") + ")")
        if reason is None and self.signals.write_error:
            reason = self.signals.write_error  # the folder is kept: a later recovery can't see these groups
        return reason

    def finish(self, failure: Failure | None) -> int:
        if failure is not None and (failure.where == "setup-early" or self.root is None):
            _err("demo: failed at setup: " + failure.reason)
            return self.signal_code() or 1
        if self.root is None:
            return self.signal_code() or 1
        if failure is not None:
            _err("demo: failed at " + failure.where + ": " + failure.reason)
        reason = self.teardown()
        if reason is not None:
            reason = plain_reason(reason)
            _err("demo: failed at cleanup: " + reason)
            failure = failure or Failure("cleanup", reason)
        elif failure is None and self.signal_number is None and self.agents_checked:
            # The verdict: read again once the conductor and every agent have stopped, so nothing recorded after
            # the first read (a late launch, its refusal or a stop hold) is missed.
            problems, _ = agent_findings(self.root, final=True)
            if problems:
                failure = Failure("step 7", NOT_CLEAN + "; ".join(problems))
                _err("demo: failed at step 7: " + failure.reason)
        if failure is None and self.signal_number is None and self.keep:
            self.tidy_worktrees()
        if self.dashboard is not None and self.dashboard[0].stdout is not None:
            try:
                self.dashboard[0].stdout.close()
            except OSError:
                pass
        if self.log is not None:
            try:
                self.log.close()
            except OSError:
                pass
        if reason is None:
            self.finish_marker()  # only a teardown that stopped everything ends the demo
        if failure is None and self.signal_number is None:
            _out("demo: finished in " + str(int(round(time.monotonic() - self.started))) + " s")
        if failure is None and not self.keep:
            errors = _rmtree(self.root)
            if errors:
                _err("demo: failed at cleanup: " + removal_failed(self.shown_root, errors))
                _out(partly_removed(self.shown_root))
                return self.signal_code() or 1
            _out("demo: removed " + self.shown_root)
        else:
            _out("demo: kept " + self.shown_root)
        return self.signal_code() or (1 if failure is not None else 0)


# ---------------------------------------------------------------- stopping

def stop_everything(folder, env: dict, *, conductor=None, ran_conductor: bool = True,
                    ygg_argv: list[str] | None = None, cwd: str | None = None, use_ygg_stop: bool = True,
                    before_signals=None, admit=None, log: SignalLog | None = None, owned=None,
                    trace=None) -> str | None:
    """Teardown steps 1 to 4 for a demo folder (PathFolder or BoundFolder). `conductor` is (process, pid,
    identity) when the caller started it; otherwise it comes from `conductor.pid`. `before_signals` runs after
    step 1 and may raise to refuse. `use_ygg_stop` is false for `--cleanup`, which signals only the pid and
    identity pairs the folder records. A pair is signalled only on an exact match; any other evidence is skipped
    (never signalled) and the final check reports it, so teardown fails and keeps the folder rather than
    succeed on incomplete evidence. A group whose leader is gone is never signalled. Returns the failure reason,
    or None."""
    if isinstance(folder, Path):
        folder = PathFolder(folder)
    log = log if log is not None else SignalLog(folder)
    owned = owned if owned is not None else []  # every child started here joins it, for the final check
    trace = trace or (lambda text: None)
    admit = admit or (lambda started: True)  # --cleanup passes the age rule: nothing older is ever signalled
    # Step 1: the conductor (it finishes its tick and exits), so no new launch or merge check starts.
    process = None
    if conductor is not None:
        process, pid, started = conductor
    else:
        pair = _pid_file(folder, "conductor.pid")
        pid, started = pair if pair else (None, None)
    if pid is not None and started and admit(started) and signal_exact(pid, started, signal.SIGTERM, log):
        if not wait_gone(pid, started, 15, process):
            signal_exact(pid, started, signal.SIGKILL, log)
            wait_gone(pid, started, 5, process)
    if process is not None:
        if process not in owned:
            owned.append(process)
        try:
            process.wait(timeout=1)
        except (subprocess.TimeoutExpired, ChildProcessError):
            pass
    trace("step 1 done (conductor)")
    if before_signals is not None:
        before_signals()
    # Step 2: `ygg stop`, the real safety path, on the demo's own state root (the driver's own teardown only).
    if use_ygg_stop and (ran_conductor or folder.has("state/activations")):
        root = cwd or str(getattr(folder, "root", ""))
        argv = (ygg_argv or [os.path.join(root, "bin", "ygg")]) + ["stop"]
        stopper = None
        try:
            stopper = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
        except (OSError, subprocess.SubprocessError):
            pass
        if stopper is not None:
            owned.append(stopper)
            started = util.process_start(stopper.pid)
            if started:
                log.note_group(stopper.pid, started)  # its own session: written down before any signal
            try:
                stopper.wait(timeout=STOP_SECONDS)
            except subprocess.TimeoutExpired:
                # An unreaped child: its pid can't have been reused, so its group is still its own.
                send(stopper.pid, signal.SIGKILL)
                try:
                    stopper.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                settle(folder, log.groups, owned=owned, changed=log.changed)
                return "ygg stop did not finish in " + str(STOP_SECONDS) + " s"
    trace("step 2 done (ygg stop)")
    # Step 3: every recorded pair that is still exactly the recorded process (merge-check jobs included, by the
    # identity in their record), checked again right before each signal. A pair that can't be identified is
    # skipped, and the others are still signalled.
    targets = [(p, s) for p, s, _ in recorded_processes(folder)
               if s and admit(s) and classify(p, s) == EXACT]
    for p, s in targets:
        signal_exact(p, s, signal.SIGTERM, log)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(classify(p, s) == EXACT for p, s in targets):
        time.sleep(0.1)
    for p, s in targets:
        signal_exact(p, s, signal.SIGKILL, log)
    trace("step 3 done (" + str(len(targets)) + " signalled)")
    # Step 4: the final check.
    reason = settle(folder, log.groups, owned=owned, changed=log.changed, trace=trace)
    trace("step 4 done (" + (reason or "all gone") + ")")
    return reason


def settle(folder, groups: dict, *, owned=(), seconds: float | None = None, changed=(), trace=None) -> str | None:
    """Step 4 and the final check: wait until no recorded process, no leader of a signalled group and no child
    this process owns is alive; no signalled group has members after its leader ended; nothing is left whose
    evidence is unknown; and no merge check is still starting. It never signals. Returns None, or at the deadline
    the reason teardown fails (the folder is kept)."""
    deadline = time.monotonic() + (STEP4_SECONDS if seconds is None else seconds)
    noted = False
    while True:
        alive, unknown, unidentified = [], [], []
        if folder is not None:
            for p, s, what in recorded_processes(folder):
                state = classify(p, s)
                if state == EXACT:
                    alive.append(p)
                elif state == UNKNOWN:
                    (unknown if s else unidentified).append((p, what))
        leaders, busy, unclear = busy_groups(groups)
        own = [process.pid for process in owned if process is not None and process.poll() is None]
        starting = starting_checks(folder) if folder is not None else []
        agentless = unfinished_without_agent(folder) if folder is not None else []
        if not (alive or unknown or unidentified or leaders or busy or unclear or own or starting or agentless
                or changed):
            return None
        if changed:
            return "process " + str(changed[0]) + " changed while it was being stopped, so it can't be accounted for"
        if trace is not None and not noted:
            noted = True
            trace("waiting: alive=%s unknown=%s unidentified=%s leaders=%s busy=%s unclear=%s own=%s starting=%s "
                  "agentless=%s" % (alive, unknown, unidentified, leaders, busy, unclear, own, starting, agentless))
        if time.monotonic() > deadline:
            if unknown:
                return str(_unknown(unknown[0][0], unknown[0][1]))
            if unclear:
                return "process group " + str(unclear[0]) + " can't be checked for members"
            if alive or leaders or own:
                return "process " + str((alive or leaders or own)[0]) + " still running"
            if unidentified:
                return ("process " + str(unidentified[0][0]) +
                        " has no recorded start identity and was not signalled")
            if busy:
                return "process group " + str(busy[0]) + " still has members"
            if agentless:
                return "activation " + agentless[0] + " has no recorded agent identity"
            return "merge check " + starting[0] + " is still starting, so its job can't be accounted for"
        time.sleep(0.2)


# ---------------------------------------------------------------- --cleanup

RECOVERY_SETTLE_SECONDS = 10


def check_ages(folder, not_before: float) -> None:
    """The age rule: every recorded process alive with its exact identity started no earlier than the demo's
    driver. Anything older is not the demo's, so nothing is signalled."""
    for pid, started, what in recorded_processes(folder):
        if not started:
            continue
        state = classify(pid, started)
        if state == UNKNOWN:
            raise _unknown(pid, what)
        if state != EXACT:
            continue
        epoch = _identity_epoch(started)
        if epoch is None or epoch < not_before:
            raise Unsafe("recorded " + what + " process " + str(pid) + " started before the demo")


NOT_CLEAN = "not every agent ended cleanly: "

# The alert kinds a clean demo run raises, each for a step the driver itself answers. Any other kind fails the
# demo, so a kind nobody listed fails closed.
#   ygg-pid-ready       the plan is ready; the driver approves it (step 2)
#   ygg-stage-approval  a stage report waits for approval; the driver approves it
#   ygg-stage-ready     a stage is ready to end; only without `escalated`, which means the PM's close
#                       activation ended without ending the stage
#   ygg-merge-ready     the project is ready to merge; the driver merges it (step 6)
#   ygg-project-closed  the project closed (step 7)
#   ygg-rehydrate-skipped  journal replay counts each `worktree-close` entry (written when the closed project's
#                       worktrees are removed) as one it can't restore; allowed only while its count is no more
#                       than the demo project's `worktree-close` entries journaled before the alert was raised
QUIET_ALERTS = frozenset({"ygg-pid-ready", "ygg-stage-approval", "ygg-merge-ready", "ygg-project-closed"})
ALERT_KIND_RE = re.compile(r"^ygg-[a-z0-9-]{1,60}$")
JOURNAL_END_FLAGS = ("spawn_failed", "capacity", "released_as", "reset_kill", "interrupted", "ambiguous", "crashed")


def _who(node) -> str:
    return node if isinstance(node, str) and util.ID_RE.match(node) else "an unnamed node"


_FENCE = re.compile(r"```yaml[ \t]*\n(.*?)^```[ \t]*(?:\n|\Z)", re.S | re.M)


def _strict_yaml(text: str):
    """YAML that may not repeat a key in any mapping: a repeated key would let a later value hide an earlier one
    (an exit code of 1 followed by 0). Raises yaml.YAMLError for that as for any other parse error."""
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        keys = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in keys:
                raise yaml.constructor.ConstructorError(None, None, "repeated key", key_node.start_mark)
            keys.add(key)
        return loader.construct_mapping(node, deep=deep)
    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    return yaml.load(text, Loader=Loader)


def _strict_entries(text: str) -> list[dict] | None:
    """A journal's entries, read strictly: its header and every block must parse to a mapping, and nothing but
    blank space may stand between or after the blocks (an unfinished block counts). None when any part fails;
    the core reader skips such a block, this check never does."""
    import yaml
    if not text.startswith("---\n") or text.find("\n---\n", 3) < 0:
        return None
    end = text.find("\n---\n", 3)
    try:
        if not isinstance(_strict_yaml(text[4:end]), dict):
            return None
    except yaml.YAMLError:
        return None
    entries: list[dict] = []
    position = end + 5
    while True:
        while position < len(text) and text[position] in " \t\n":
            position += 1
        if position == len(text):
            return entries
        match = _FENCE.match(text, position)
        if match is None:
            return None
        try:
            value = _strict_yaml(match.group(1))
        except yaml.YAMLError:
            return None
        if not isinstance(value, dict):
            return None
        entries.append(value)
        position = match.end()


def _moment(value) -> float | None:
    """An ISO time in seconds since the epoch; None when it isn't one."""
    import datetime
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def _journaled(root: Path, folder: PathFolder) -> tuple[dict, list[float], str | None]:
    """Every activation the journals record (key: node, the programme whose journal holds it, phases, every
    exit entry), the times of the `worktree-close` entries the app wrote for the demo's project, and why the
    journals can't be read, if they can't."""
    try:
        programmes = sorted(os.listdir(root / "tracker" / "initiatives"))
    except OSError:
        return {}, [], "the programme journal can't be listed"
    found: dict[str, dict] = {}
    closes: list[float] = []
    for programme, relative in [(None, "tracker/_ygg/journal.md")] + [
            (name, "tracker/initiatives/" + name + "/documents/journal.md") for name in programmes]:
        try:
            text = folder.read(relative)
        except Unsafe:
            return found, closes, "the programme journal can't be read"
        entries = _strict_entries(text) if text is not None else []
        if entries is None:
            return found, closes, "the programme journal has an entry that can't be read"
        for entry in entries:
            if entry.get("kind") == "worktree-close" and entry.get("actor") == "ygg-app" and \
                    entry.get("project") == script.PROJECT and _moment(entry.get("at")) is not None:
                closes.append(_moment(entry.get("at")))
            if entry.get("kind") != "activation":
                continue
            if not isinstance(entry.get("key"), str) or not entry.get("key"):
                return found, closes, "the programme journal has an activation entry without its key"
            item = found.setdefault(str(entry["key"]), {"node": None, "programmes": set(), "phases": set(),
                                                        "exits": []})
            item["node"] = item["node"] or entry.get("node")
            item["programmes"].add(programme)
            item["phases"].add(entry.get("phase"))
            if entry.get("phase") == "exited":
                item["exits"].append(entry)
    return found, closes, None


def _activation_problem(who: str, record: dict, journaled: dict | None) -> tuple[str | None, bool]:
    """(what is wrong with one activation, whether it is still running); (None, False) for a clean end."""
    phases = journaled["phases"] if journaled else set()
    exits = (journaled or {}).get("exits") or []
    outcome = record.get("outcome")
    code = record.get("exit_code")
    if outcome == "uncertain":
        return who + " was held because Yggdrasil couldn't confirm its agent started", False
    if outcome == "aborted" or record.get("phase") == "aborted" or "aborted" in phases:
        return who + "'s agent was stopped before it started", False
    if record.get("exited_at") is None or (journaled is not None and not phases & {"exited", "released"}):
        return who + "'s agent had not ended", True
    if any(record.get(flag) for flag in ("interrupted", "crashed", "ambiguous")) or "released" in phases or \
            any(end.get(flag) for end in exits
                for flag in ("interrupted", "ambiguous", "reset_kill", "crashed", "released_as")):
        return who + "'s agent was stopped while it ran, so its work is held for checking", False
    if any(end != exits[0] for end in exits[1:]):
        return who + "'s run has two different exit records in the programme journal", False
    if not (isinstance(code, int) and not isinstance(code, bool)) or code != 0 or \
            any(end.get("exit_code") != 0 for end in exits):
        shown = str(code) if isinstance(code, int) and not isinstance(code, bool) else "no exit code"
        return who + "'s agent ended with an error (exit " + shown + ")", False
    if any(end.get("spawn_failed") for end in exits):
        return "Yggdrasil couldn't start " + who + "'s agent", False
    if record.get("class") == "capacity" or any(end.get("capacity") for end in exits):
        return who + "'s agent ran out of model capacity", False
    if outcome != "confirmed":
        return who + "'s agent ended in a state the demo doesn't expect", False
    if journaled is None:
        return who + "'s agent is missing from the programme journal", False
    return None, False


def _alert_problem(row: dict, worktree_closes: list[float]) -> str | None:
    kind = row["kind"]
    who = _who(row.get("node"))
    if kind in QUIET_ALERTS:
        return None
    if kind == "ygg-stage-ready":
        return None if row.get("escalated") in (None, False) else "a stage was left open after its PM's last turn"
    if kind == "ygg-rehydrate-skipped":
        count, raised = row.get("count"), _moment(row.get("ts"))
        if isinstance(count, int) and not isinstance(count, bool) and raised is not None and \
                0 < count <= sum(1 for at in worktree_closes if at <= raised):
            return None
        return "Yggdrasil skipped journal entries it couldn't read"
    plain = {
        "ygg-uncertain": who + " was held because Yggdrasil couldn't confirm its agent started",
        "ygg-stall": who + "'s agent stalled",
        "ygg-launch-refused": "Yggdrasil refused to start " + who + "'s agent",
        "ygg-stop-held": who + "'s agent was stopped while it ran, so its work is held for checking",
    }
    if kind in plain:
        return plain[kind]
    return "Yggdrasil raised an alert the demo doesn't expect (" + \
        (kind if ALERT_KIND_RE.match(kind) else "of an unknown kind") + ")"


def agent_findings(root: Path, *, final: bool) -> tuple[list[str], bool]:
    """Everything that keeps the demo from having ended cleanly, in plain words, and whether an agent is still
    running. The activations checked are every one the programme journal records plus every record found; a
    journaled activation without a readable record, two records for one activation, an activation of another
    programme or project, a listing or read error, a journal entry that can't be read, an alert line that isn't
    an alert object, and any alert kind outside the list above all count. Before the stop (`final` false) a last
    alert line without its newline is still being written and waits for the final read."""
    folder = PathFolder(root)
    problems: list[str] = []

    def note(text: str) -> None:
        if text not in problems:
            problems.append(text)
    still_open = False
    records: dict[str, dict] = {}
    try:
        names = sorted(os.listdir(root / "state" / "activations"))
    except OSError:
        names = None
        note("the agents' records can't be listed")
    read: list[tuple[str, dict]] = []
    for name in names or []:
        if not name.endswith(".json"):
            continue
        try:
            data = _json(folder, "state/activations/" + name)
        except Unsafe:
            data = None
        if not isinstance(data, dict):
            note("an agent's record can't be read")
            continue
        read.append((name[:-5], data))
    claims: dict = {}
    for _, data in read:
        claims[data.get("activation")] = claims.get(data.get("activation"), 0) + 1
    for stem, data in read:
        key = data.get("activation")
        who = _who(data.get("node"))
        if claims[key] > 1:
            note(who + " has two records for one run")
            continue
        if key != stem:
            note(who + "'s record names a different run than its file")
            continue
        records[key] = data
        if data.get("programme") != script.PROGRAMME or data.get("project") != script.PROJECT:
            note(who + "'s run belongs to another programme or project than the demo's")
    journaled, worktree_closes, journal_error = _journaled(root, folder)
    if journal_error:
        note(journal_error)
    for item in journaled.values():
        if item["programmes"] != {script.PROGRAMME}:
            note(_who(item["node"]) + "'s run belongs to another programme or project than the demo's")
    if names is not None and not journal_error and not (
            any(item["programmes"] == {script.PROGRAMME} for item in journaled.values()) and
            any(data.get("programme") == script.PROGRAMME and data.get("project") == script.PROJECT
                for data in records.values())):
        note("no agent of the demo's project ran")
    for key in sorted(set(records) | set(journaled)):
        record, entry = records.get(key), journaled.get(key)
        who = _who((record or {}).get("node") or (entry or {}).get("node"))
        if record is None:
            if names is not None:
                note(who + "'s agent left no record")
            continue
        problem, running = _activation_problem(who, record, entry)
        still_open = still_open or running
        if problem:
            note(problem)
    try:
        text = folder.read("state/alerts.jsonl")
    except Unsafe:
        text = None
    if text is None:
        note("the alert log can't be read")
        text = ""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    elif lines and not final:
        lines.pop()  # still being written
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not (isinstance(row, dict) and all(isinstance(row.get(field), str) and row.get(field)
                                              for field in ("id", "ts", "kind", "message"))):
            note("the alert log has a line that isn't a valid alert")
            continue
        problem = _alert_problem(row, worktree_closes)
        if problem:
            note(problem)
    return problems, still_open


_PLAIN_REASONS = [
    # An error's own text can quote a full path: keep only its type.
    (re.compile(r" \(([A-Z][A-Za-z]*(?:Error|Exception|Interrupt|Exit))(?::[^()]*)?\)"), r" (\1)"),
    (re.compile(r"[^\s'\"(),]*state/activations/[^\s'\"(),]*"), "an agent's record"),
    (re.compile(r"[^\s'\"(),]*state/merge-checks/[^\s'\"(),]*"), "a merge check's record"),
    (re.compile(r"[^\s'\"(),]*" + re.escape(GROUPS_FILE)), "the list of process groups the demo signalled"),
    (re.compile(r"[^\s'\"(),]*state/[^\s'\"(),]*"), "a file in the demo's state folder"),
    (re.compile(r"\bactivation act-[0-9a-z-]+"), "an agent's run"),
    (re.compile(r"\bmerge check [a-z0-9?][a-z0-9-]*"), "a merge check"),
    (re.compile(r"\bact-[0-9a-z-]+"), "an agent's run"),
]


def plain_reason(reason: str) -> str:
    """A teardown reason for the driver's last line: files named by their role, never by their path inside the
    state folder, and no activation or merge-check ids."""
    for pattern, replacement in _PLAIN_REASONS:
        reason = pattern.sub(replacement, reason)
    return reason


def open_spawned(folder) -> set[str]:
    """Activations the programme journals (read raw) show spawned with no terminal fact."""
    from ..journal import _parse_blocks
    found: dict[str, set] = {}
    paths = ["tracker/_ygg/journal.md"] + ["tracker/initiatives/" + name + "/documents/journal.md"
                                           for name in folder.listing("tracker/initiatives")]
    for relative in paths:
        parent, _, name = relative.rpartition("/")
        if not folder.has(parent) or name not in folder.listing(parent):
            continue
        text = folder.read(relative)
        if text is None:
            raise Unsafe(relative + " can't be read")
        for entry in _parse_blocks(text):
            if entry.get("kind") == "activation" and entry.get("key") and not entry.get("cleared_at"):
                found.setdefault(str(entry["key"]), set()).add(entry.get("phase"))
    terminal = {"exited", "aborted", "released"}
    return {key for key, phases in found.items() if "spawned" in phases and not phases & terminal}


def check_recoverable(folder, real_root: str, shown: str, not_before: float) -> None:
    """Fail closed before recovery signals anything: it signals only the pid and identity pairs the folder records,
    so every process of the demo must have one. Refused: an unfinished activation without a recorded
    agent identity, a live agent whose identity can't be matched exactly, a merge check still `starting` or
    `running` without an identity, an activation the journal still shows open after its record ended, and a
    working folder outside the demo's worktrees."""
    worktrees = real_root + "/home/.ygg-worktrees/"
    records = {}
    for name in folder.listing("state/activations"):
        if not name.endswith(".json"):
            continue
        data = _json(folder, "state/activations/" + name)
        if data is MISSING:
            continue
        if not isinstance(data, dict):
            raise Unsafe("state/activations/" + name + " is malformed")
        key = str(data.get("activation") or name[:-5])
        records[key] = data
        cwd = data.get("cwd")
        if cwd and not os.path.realpath(str(cwd)).startswith(worktrees):
            raise Unsafe("activation " + key + " names a working folder outside " + worktrees)
        if data.get("exited_at") is not None or data.get("terminal"):
            continue
        pid = _int(data.get("pid"))
        recorded = data.get("process_start_identity") or data.get("process_started_at")
        if pid is None:
            raise Unsafe("activation " + key + " has no recorded agent identity")
        if _alive(pid) is False:
            continue  # nothing of it is running: with or without an identity it can't be signalled wrongly
        if not recorded:
            raise Unsafe("activation " + key + " has no recorded agent identity")
        actual = util.process_start(pid)
        if same(pid, recorded):
            epoch = _identity_epoch(recorded)
            if epoch is None or epoch < not_before:
                raise Unsafe("recorded agent process " + str(pid) + " started before the demo")
        elif actual is None or util.start_match(recorded, actual) is not None:
            raise Unsafe("activation " + key + "'s agent (pid " + str(pid) + ") can't be identified exactly")
    for check in starting_checks(folder):
        raise Unsafe("merge check " + check + " is still starting")
    for _, data in _merge_checks(folder):
        pid = _int(data.get("pid"))
        if data.get("state") == "running" and not data.get("process_started_at") and pid is not None and \
                _alive(pid) is not False:
            raise Unsafe("merge check " + str(data.get("id")) + " has no recorded identity")
    for key in sorted(open_spawned(folder)):
        data = records.get(key)
        if data is None:
            raise Unsafe("activation " + key + " is open in the journal with no recorded agent to check")
        if data.get("exited_at") is not None or data.get("terminal"):
            raise Unsafe("activation " + key + "'s record shows its agent ended but the journal doesn't, so its "
                         "other processes can't be ruled out; stop them, then remove " + shown + " by hand")


def unsafe_reason(folder: str) -> str | None:
    """Why `folder` is not a safe demo folder to clean up, or None."""
    try:
        bind(folder).close()
    except Unsafe as error:
        return str(error)
    return None


def cleanup(folder: str) -> int:
    first: list = []

    def note(signum, _frame):
        if not first:
            first.append(signum)
    for number in SIGNALS:
        signal.signal(number, note)
    if not os.path.basename(os.path.normpath(folder)).startswith("ygg-demo-"):
        _err("demo: " + folder + " is not a demo folder")
        return 1
    try:
        bound = bind(folder)
    except Unsafe as error:
        _err("demo: " + folder + " is not a safe demo folder: " + str(error))
        return 1
    try:
        return _cleanup_bound(folder, bound, first)
    except Unsafe as error:
        _err("demo: " + folder + " is not a safe demo folder: " + str(error))
        return 1
    finally:
        bound.close()


def _cleanup_bound(folder: str, bound: BoundFolder, first: list) -> int:
    """Recovery: teardown steps 1, 3, 4 and 5 for a left-behind folder. It never runs `ygg stop` or anything else
    from the folder; it signals only recorded pid and identity pairs that match exactly and pass the age rule.
    Whenever the evidence is incomplete it refuses and keeps the folder. A refusal before anything was signalled
    is worded as an untouched folder's; after the first signal, as a failed cleanup."""
    real = os.path.realpath(folder)
    marker = _json(bound, "demo-marker.json")
    if not isinstance(marker, dict) or marker.get("root") != real:
        _err("demo: " + folder + " is not a demo folder")
        return 1
    not_before = _identity_epoch(marker.get("driver_started"))
    if not_before is None:
        raise Unsafe("its marker has no readable driver start")
    driver = _int(marker.get("driver_pid"))
    if driver is None or driver < 2:
        raise Unsafe("its marker has no valid driver pid")
    state = classify(driver, marker.get("driver_started"))
    if state == UNKNOWN:
        raise _unknown(driver, "driver")
    if state == EXACT:
        _err("demo: the demo in " + folder + " is still running (pid " + str(driver) + ")")
        return 1
    check_ages(bound, not_before)
    log = SignalLog(bound)
    log.load()
    for pgid, started in sorted(log.groups.items()):
        state = classify(pgid, started)
        if state == EXACT:
            continue  # its recorded leader is alive: step 3 and the final check deal with it
        if state == DIFFERENT:
            log.groups.pop(pgid, None)  # the id belongs to another process now: the old group emptied first
            continue
        if group_members(pgid) is not False:
            # Probe only: a reused id that has members makes this refuse wrongly, never signal wrongly.
            raise Unsafe("process group " + str(pgid) + " listed in " + GROUPS_FILE + " still has members")

    def before_signals() -> None:
        # After the conductor stopped: the whole check again, which must find the very folders checked first,
        # then the age rule and the recovery check on records that can no longer change. A launch the conductor
        # left half done gets a few seconds to settle (its supervisor records the agent or gives up).
        again = bind(folder)
        try:
            for relative, identity in bound.identities.items():
                if again.identities.get(relative, identity) != identity:
                    raise Unsafe((relative or "the folder") + " changed while it was cleaned up")
        except BaseException:
            again.close()
            raise
        bound.adopt(again)  # read on through the fresh descriptors (they also see folders made since)
        deadline = time.monotonic() + RECOVERY_SETTLE_SECONDS
        while True:
            try:
                check_ages(bound, not_before)
                check_recoverable(bound, real, folder, not_before)
                return
            except Unsafe:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)

    def admit(started) -> bool:
        epoch = _identity_epoch(started)
        return epoch is not None and epoch >= not_before

    try:
        reason = stop_everything(bound, {}, use_ygg_stop=False, before_signals=before_signals, admit=admit,
                                 log=log)
    except Unsafe as error:
        reason = str(error)
    if reason is not None:
        if not log.signalled:
            raise Unsafe(reason)  # nothing was signalled: the folder is as it was found
        _err("demo: failed at cleanup: " + reason)
        _out("demo: kept " + folder)
        return 128 + first[0] if first else 1
    marker["ended"] = True
    try:
        bound.replace("demo-marker.json", json.dumps(marker) + "\n")
    except OSError:
        pass
    try:
        errors = bound.remove()
    except (OSError, Unsafe) as error:
        errors = [str(error)]
    if errors:
        _err("demo: failed at cleanup: " + removal_failed(folder, errors))
        _out(partly_removed(folder))
        return 128 + first[0] if first else 1
    _out("demo: removed " + folder)
    return 128 + first[0] if first else 0


def main(args) -> int:
    if getattr(args, "cleanup", None):
        return cleanup(args.cleanup)
    return Driver(bool(args.fast), bool(args.keep), bool(args.no_open)).run()
