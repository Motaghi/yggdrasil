"""Shared helpers: errors, paths, clock, locking, atomic files, ids, redaction."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

STANDARD_VERSION = 1
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RESERVED_IDS = {"l1", "user"}
# Known credential shapes (best effort): Linear, Anthropic, OpenAI project, service and admin keys, GitHub
# tokens of every kind, AWS access key ids and Slack tokens.
SECRET_RE = re.compile(r"(?:lin_api_|lin_oauth_|sk-(?:ant|proj|svcacct|admin)-|gh[opsur]_|github_pat_)[A-Za-z0-9_\-]*"
                       r"|(?:AKIA|ASIA)[A-Z0-9]{16}|xox[abposr]-[A-Za-z0-9-]+")

EXIT_USAGE = 1
EXIT_HOOK_DENY = 2
EXIT_REFUSED = 3
EXIT_GUARD = 4
EXIT_LOCK = 5
EXIT_ADMISSION = 6
EXIT_UNCERTAIN = 7
EXIT_TRACKER = 8
EXIT_PAUSED = 64


class YggError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class LockTimeout(YggError):
    """A lock that stayed busy past the caller's timeout."""


def refuse(reason: str) -> YggError:
    return YggError(EXIT_REFUSED, "REFUSED: " + reason)


def usage(message: str) -> YggError:
    return YggError(EXIT_USAGE, message)


# ---------------------------------------------------------------- environment

def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def current_node() -> str | None:
    """The managed node id of the caller, or None for the user's shell or L1."""
    node = env("YGG_NODE")
    if node in (None, "l1", "user"):
        return None
    return node


def overrides_allowed() -> bool:
    return env("YGG_NODE") is None or env("YGG_TEST_MODE") == "1"


def test_mode() -> bool:
    return env("YGG_TEST_MODE") == "1"


def _configured(key: str):
    from . import config
    value = config.get(key)
    return value if isinstance(value, str) and value else None


def state_root() -> Path:
    root = Path(env("YGG_STATE_ROOT") or _configured("state_root") or
                Path.home() / ".local" / "state" / "yggdrasil")
    root.mkdir(parents=True, exist_ok=True)
    _owner_only(root, 0o700)
    return root


def _owner_only(path: Path, mode: int) -> None:
    """Set a folder or file Yggdrasil keeps private to `mode`; refuse when that cannot be done."""
    try:
        if os.stat(path).st_mode & 0o777 != mode:
            os.chmod(path, mode)
    except OSError as error:
        raise refuse(str(path) + " must have mode " + oct(mode)[2:] + " and cannot be changed: " +
                     (error.strerror or str(error)))


def files_root() -> Path:
    return Path(env("YGG_FILES_ROOT") or _configured("files_root") or Path.cwd() / "ygg-tracker")


# ---------------------------------------------------------------- clock

def parse_time(text: str) -> datetime:
    value = str(text).strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def clock_source() -> str:
    return "override" if env("YGG_NOW") and overrides_allowed() else "system"


def now() -> datetime:
    if clock_source() == "override":
        return parse_time(env("YGG_NOW"))
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    moment = (moment or now()).astimezone(timezone.utc)
    return moment.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def compact(moment: datetime | None = None) -> str:
    return (moment or now()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------- files

def fsync_dir(path: Path) -> None:
    """Flush a directory entry, so a rename or creation inside it survives an OS crash (I13). Failures
    propagate: a write is only complete once its directory is synced."""
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_dir(path: Path) -> Path:
    """Create a directory and any missing parents, flushing each parent entry that gained a child."""
    path = Path(path)
    missing = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        fsync_dir(directory.parent)
    return path


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Durable replace: the file (with `mode`, if given) is flushed before the rename and the directory after
    it; the write counts as done only once both are synced (I13)."""
    path = Path(path)
    ensure_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            # Every durable write passes through redaction, whichever command produced the text.
            handle.write(redact(text))
            handle.flush()
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    from . import faults
    faults.after_write(path)  # A7.2: counted (and possibly faulted) once the write is done


def read_text(path: Path, default: str = "") -> str:
    try:
        return Path(path).read_text()
    except (FileNotFoundError, NotADirectoryError):
        return default


def _store_file(path: Path) -> bool:
    """Whether a path lies in a store whose files must never be read as empty when torn (state root or
    safety folder); tracker records have their own corruption handling."""
    try:
        resolved = Path(path).resolve()
        roots = [state_root().resolve()]
        from . import safety
        roots.append(safety.root().resolve())
    except OSError:
        return False
    return any(resolved == base or resolved.is_relative_to(base) for base in roots)


def corrupt_store(path: Path, detail: str) -> YggError:
    return YggError(EXIT_USAGE, "corrupt store file " + str(path) + ": " + detail)


def read_json(path: Path, default=None):
    """A JSON file, or `default` when it does not exist. An existing store file that is empty or does not
    parse is reported, never read as empty (I13)."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        text = read_text(path)
    except (UnicodeDecodeError, OSError) as error:
        if _store_file(path):
            raise corrupt_store(path, "unreadable: " + str(error))
        return default
    if not text.strip():
        if _store_file(path):
            raise corrupt_store(path, "empty")
        return default
    try:
        return json.loads(text)
    except ValueError as error:
        if _store_file(path):
            raise corrupt_store(path, str(error))
        return default


def write_json(path: Path, data) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=False) + "\n")


def append_line(path: Path, data) -> None:
    """Durable append: the line is flushed, and so is the directory when the file is new (I13)."""
    path = Path(path)
    ensure_dir(path.parent)
    created = not path.exists()
    # Logs are owner-only (created 0600, tightened if older), and redacted like every durable write.
    descriptor = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        if os.fstat(handle.fileno()).st_mode & 0o777 != 0o600:
            os.fchmod(handle.fileno(), 0o600)
        handle.write(redact(json.dumps(data)) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if created:
        fsync_dir(path.parent)
    from . import faults
    faults.after_write(path)


def read_lines(path: Path) -> list:
    """JSON lines. A line that does not parse, a torn final record included, is reported (I13)."""
    lines = []
    try:
        text = read_text(path)
    except (UnicodeDecodeError, OSError) as error:
        raise corrupt_store(path, "unreadable: " + str(error))
    for index, line in enumerate(text.split("\n")):
        if not line.strip():
            continue
        try:
            lines.append(json.loads(line))
        except ValueError as error:
            raise corrupt_store(path, "line " + str(index + 1) + ": " + str(error))
    return lines


# Lock depth and handles are per thread: a lock held by one thread is never re-entered by another, which
# opens its own handle and waits on the flock like any other holder.
_LOCKS = threading.local()


def _held() -> tuple[dict, dict]:
    if not hasattr(_LOCKS, "depth"):
        _LOCKS.depth, _LOCKS.files = {}, {}
    return _LOCKS.depth, _LOCKS.files


CREATION_LOCK = "creation"
RECEIPT_LOCK = "receipt"  # a leaf: it guards one activation-record read-modify-write and nothing else


def lock_held(name: str) -> bool:
    """Whether this thread holds the named lock now."""
    return bool(_held()[0].get(name))


@contextlib.contextmanager
def locked(name: str = "journal", timeout: float | None = None):
    """Exclusive lock across processes and threads, re-entrant within one thread, kept in the safety folder so a state-root wipe can never
    split it. `journal` (the default) serialises transitions and journal writes and may be held across
    tracker I/O; `creation` guards only local safety-folder work and agent creation, and no other lock may
    be acquired while it is held. Lock files are created once and never truncated or removed, so the flock
    inode stays stable. With `timeout`, a lock still held by another process after that many seconds raises
    `LockTimeout` instead of waiting on."""
    depths, files = _held()
    if depths.get(RECEIPT_LOCK) and name != RECEIPT_LOCK:
        raise RuntimeError("lock order: '" + name + "' acquired while the receipt lock is held")
    if name not in (CREATION_LOCK, RECEIPT_LOCK) and depths.get(CREATION_LOCK):
        raise RuntimeError("lock order: '" + name + "' acquired while the creation lock is held")
    depth = depths.get(name, 0)
    if depth:
        depths[name] = depth + 1
        try:
            yield
        finally:
            depths[name] -= 1
        return
    from . import safety
    try:
        path = safety.layout() / "locks" / (name + ".lock")
        handle = open(path, "a+")
    except OSError as error:
        raise YggError(EXIT_REFUSED, "safety folder " + str(safety.root()) + " missing or unreadable: " + str(error))
    if timeout is None:
        fcntl.flock(handle, fcntl.LOCK_EX)
    else:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise LockTimeout(EXIT_TRACKER, "the " + name + " lock is busy")
                time.sleep(0.05)
    depths[name] = 1
    files[name] = handle
    try:
        if name == "journal":
            safety.bootstrap()  # first use: the folder's establishment fact, outside the creation lock
        yield
    finally:
        depths[name] = 0
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
        files.pop(name, None)


# ---------------------------------------------------------------- ids and text

def check_id(value: str, what: str = "identifier", *, allow_reserved: bool = False) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise usage("invalid " + what + " '" + str(value) + "': must match ^[a-z0-9][a-z0-9-]{0,62}$")
    if value in RESERVED_IDS and not allow_reserved:
        raise usage("invalid " + what + " '" + value + "': reserved")
    return value


def redact(text: str) -> str:
    return SECRET_RE.sub("[REDACTED]", text or "")


SECRET_KEY_RE = re.compile(r"(secret|token|password|passwd|credential|api_key|apikey|private_key)", re.I)


SECRET_KEY_PARTS = frozenset(("key", "auth", "authorization", "cookie", "session", "bearer", "signature", "pat"))
# Names with a secret-looking part that hold a non-secret (a short team prefix), shown as they are. The exemption
# applies only to the second match: whatever the old substring rule hides stays hidden.
SHOWN_KEY_NAMES = frozenset(("linear_team_key",))


def secret_looking_key(key: str) -> bool:
    """Today's substring match, plus a second one: the key name split on `_`, `-` and `.` has a part, in any
    case, that is a whole secret word (`STRIPE_KEY`, `SESSION_COOKIE`). The second match only adds names."""
    if SECRET_KEY_RE.search(key):
        return True
    if key.lower() in SHOWN_KEY_NAMES:
        return False
    return any(part in SECRET_KEY_PARTS for part in re.split(r"[_.\-]", key.lower()))


def redact_profile(value, key: str = ""):
    """A copy of profile data for display, with every value under a secret-looking key replaced."""
    # A secret-looking key hides its whole value, nested containers included.
    if key and secret_looking_key(key) and value not in (None, "", {}, []):
        return "[redacted]"
    if isinstance(value, dict):
        return {(redact(k) if isinstance(k, str) else k): redact_profile(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_profile(v) for v in value]
    if isinstance(value, str):
        return redact(value)
    return value


def split_frontmatter(text: str) -> tuple[dict | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---", 4)
    if end < 0:
        return None, text
    header_text = text[4:end]
    rest = text[end + 4:]
    if rest.startswith("\n"):
        rest = rest[1:]
    try:
        header = yaml.safe_load(header_text)
    except yaml.YAMLError:
        return None, text
    if header is None:
        header = {}
    if not isinstance(header, dict):
        return None, text
    return header, rest


SECTION_LINE = re.compile(r"^## (Records|Comments)$", re.M)


def normalise_newlines(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def escape_sections(text: str) -> str:
    """Neutralise body lines that would parse as a Records or Comments section (same grammar)."""
    return SECTION_LINE.sub(lambda match: "\\" + match[0], normalise_newlines(text))


def check_stage(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,4}", text) or not 1 <= int(text) <= 9999:
        raise usage("invalid --stage '" + str(value) + "': must be a positive integer")
    return int(text)


def check_record_id(value, what: str = "package") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(WP-[0-9]{1,9}|(D|E|MG|EN|I|T)-[0-9]{4,9})", value):
        raise usage("invalid " + what + " id '" + str(value) + "'")
    return value


def dump_yaml(data) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def render_frontmatter(header: dict, body: str) -> str:
    return "---\n" + dump_yaml(header) + "---\n" + body


def bullets(text: str, heading: str) -> list[str]:
    match = re.search(r"(?ms)^## " + re.escape(heading) + r"[ \t]*\n(.*?)(?=^## |\Z)", text)
    if not match:
        return []
    return [line[2:].strip() for line in match[1].splitlines() if line.startswith("- ")]


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        pass
    import subprocess
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    status = result.stdout.strip()
    return bool(status) and not status.startswith("Z")


# A process's start identity is its start time as an epoch from the kernel, written `s:<seconds>.<micro>`
# (`s:<seconds>` when only whole seconds are known), so no reader's timezone or locale can change it. Older
# receipts hold `ps -o lstart` text, which is read as UTC (the conductor's zone) or as local time, whole
# seconds only: a live agent is never taken for a dead one because of the zone its record was written in.
LSTART = "%a %b %d %H:%M:%S %Y"


def _kernel_start(pid: int) -> tuple[int, int] | None:
    """(seconds, microseconds) since the epoch at which `pid` started, from the kernel, or None."""
    import struct
    import sys
    if sys.platform == "darwin":
        import ctypes
        import ctypes.util
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
            size = ctypes.c_size_t(1024)
            buffer = ctypes.create_string_buffer(size.value)
            if libc.sysctl(mib, 4, buffer, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0 or size.value < 16:
                return None
            # struct kinfo_proc begins with extern_proc, whose first member holds p_starttime (a timeval).
            seconds, micro = struct.unpack_from("=qi", buffer.raw, 0)
        except (OSError, AttributeError, struct.error):
            return None
        return (seconds, micro) if seconds > 0 and 0 <= micro < 1000000 else None
    if sys.platform.startswith("linux"):
        try:
            stat = Path("/proc/" + str(pid) + "/stat").read_text()
            ticks = int(stat[stat.rindex(")") + 2:].split()[19])  # field 22, after the command in parentheses
            boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                        if line.startswith("btime "))
            hertz = os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError, StopIteration):
            return None
        whole, rest = divmod(ticks, hertz)
        return boot + whole, rest * 1000000 // hertz
    return None


def process_start(pid) -> str | None:
    """The start identity of a live `pid` (see LSTART above), or None when it can't be read."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    found = _kernel_start(pid)
    if found is not None:
        return "s:%d.%06d" % found
    import subprocess
    try:  # no kernel interface: ps, always in UTC and the C locale
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                                env=dict(os.environ, TZ="UTC", LC_ALL="C", LANG="C"), timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    epochs = start_epochs(" ".join(result.stdout.split()), zones=("UTC",))
    return "s:%d" % min(epochs) if epochs else None


def _system_zone():
    """The machine's configured timezone (from /etc/localtime), whatever TZ this process runs under."""
    try:
        from zoneinfo import ZoneInfo
        name = os.readlink("/etc/localtime").split("zoneinfo/", 1)[-1]
        return ZoneInfo(name)
    except (OSError, ValueError, ImportError, KeyError):
        return None


def start_epochs(text: str, zones=("UTC", "local", "system")) -> set:
    """Whole-second epochs a start time may denote: a `s:` identity has one; `ps -o lstart` text has one per
    zone it may have been written in (UTC, this process's local zone, the machine's zone)."""
    import calendar
    import time
    text = " ".join(str(text or "").split())
    if text.startswith("s:"):
        try:
            return {int(float(text[2:]))}
        except ValueError:
            return set()
    try:
        parsed = time.strptime(text, LSTART)
    except ValueError:
        return set()
    from datetime import datetime
    found = set()
    if "UTC" in zones:
        found.add(calendar.timegm(parsed))
    if "local" in zones:
        for fold in (0, 1):  # both readings of an hour that repeats when the clocks go back, and only then
            try:
                found.add(int(datetime(*parsed[:6], fold=fold).timestamp()))
            except (OverflowError, ValueError, OSError):
                pass
    zone = _system_zone() if "system" in zones else None
    if zone is not None:
        for fold in (0, 1):
            found.add(int(datetime(*parsed[:6], tzinfo=zone, fold=fold).timestamp()))
    return found


def start_epoch(text: str):
    """The epoch of a start identity (seconds, with microseconds when known), or of lstart text read as UTC."""
    text = " ".join(str(text or "").split())
    if text.startswith("s:"):
        try:
            return float(text[2:])
        except ValueError:
            return None
    epochs = start_epochs(text, zones=("UTC",))
    return min(epochs) if epochs else None


def start_match(recorded, actual) -> str | None:
    """How a recorded start time names the process whose current identity is `actual`: `exact` (a `s:`
    identity, unambiguous in every timezone), `ambiguous` (older lstart text that matches in one of the
    zones it may have been written in, which alone never identifies a process well enough to signal it),
    or None."""
    if not recorded or not actual:
        return None
    recorded, actual = " ".join(str(recorded).split()), " ".join(str(actual).split())
    if not actual.startswith("s:"):
        return "exact" if recorded == actual else None
    if recorded.startswith("s:"):
        mine, theirs = recorded[2:], actual[2:]
        if mine == theirs:
            return "exact"
        if "." in mine and "." in theirs:
            return None  # both exact and different
        try:
            return "exact" if int(float(mine)) == int(float(theirs)) else None
        except ValueError:
            return None
    try:
        return "ambiguous" if int(float(actual[2:])) in start_epochs(recorded) else None
    except ValueError:
        return None


def start_matches(recorded, actual) -> bool:
    """Whether a recorded start time may name the process whose current identity is `actual`."""
    return start_match(recorded, actual) is not None


def same_start(pid, recorded) -> bool:
    """The live process `pid` is the one recorded (its start time matches, whatever zone it was written in)."""
    return start_matches(recorded, process_start(pid))


# ---------------------------------------------------------------- trusted git

# Variables that would make git read a different repository, object store, ref view or configuration than
# the path Yggdrasil names explicitly with -C.
GIT_SELECTION = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_REPLACE_REF_BASE",
                 "GIT_CONFIG", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM")
# Yggdrasil's own git calls never run a program the repository's config names: hooks, fsmonitor, signing,
# editors, pagers, ssh, credential helpers and network transports are all forced off.
GIT_FORCED = {"core.hooksPath": "/dev/null", "core.fsmonitor": "false", "commit.gpgSign": "false",
              "tag.gpgSign": "false", "gpg.program": "false", "gpg.ssh.program": "false",
              "gpg.x509.program": "false", "core.editor": "false", "sequence.editor": "false",
              "core.pager": "cat", "core.sshCommand": "false", "core.askPass": "false", "credential.helper": "",
              "protocol.allow": "never"}
GIT_SAFETY = tuple(item for key, value in GIT_FORCED.items() for item in ("-c", key + "=" + value))


# Variables that name a program git would run, or point git at other programs or templates.
GIT_PROGRAMS = ("GIT_EXTERNAL_DIFF", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_PAGER", "GIT_SSH", "GIT_SSH_COMMAND",
                "GIT_ASKPASS", "GIT_PROXY_COMMAND", "GIT_EXEC_PATH", "GIT_TEMPLATE_DIR")
_GLOBAL_CONFIG: dict = {}


def trusted_global_config() -> str:
    """A global git config Yggdrasil controls: only the user's identity, copied once per process from the
    real global config (read without includes). Git never reads the real global or system config for
    Yggdrasil's own calls."""
    target = str(state_root() / "git" / "global.gitconfig")
    if _GLOBAL_CONFIG.get("path") == target:
        return target
    import subprocess
    environment = {k: v for k, v in os.environ.items()
                   if k not in GIT_SELECTION and k not in GIT_PROGRAMS and k != "GIT_CONFIG_GLOBAL"}
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    lines = []
    for key in ("name", "email"):
        found = subprocess.run(["git", "config", "--global", "--no-includes", "--get", "user." + key],
                               capture_output=True, text=True, env=environment).stdout.strip()
        if found and "\n" not in found:
            lines.append("\t" + key + " = " + json.dumps(found, ensure_ascii=False))
    atomic_write(Path(target), ("[user]\n" + "\n".join(lines) + "\n") if lines else "", mode=0o600)
    _GLOBAL_CONFIG["path"] = target
    return target


def git_env() -> dict:
    environment = {k: v for k, v in os.environ.items()
                   if k not in GIT_SELECTION and k not in GIT_PROGRAMS
                   and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    # Replacement refs could make a commit look like another; Yggdrasil always reads the real objects.
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = trusted_global_config()
    # No transport of any kind, no lazy fetch, no user-chosen protocol.
    environment["GIT_ALLOW_PROTOCOL"] = ""
    environment["GIT_PROTOCOL_FROM_USER"] = "0"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    return environment


def trusted_git(*args, check: bool = False):
    """Every git call Yggdrasil makes itself: programs the config could name are forced off, no repository
    selection variables, no replace refs, no system config and only Yggdrasil's own global config."""
    import subprocess
    return subprocess.run(["git", *GIT_SAFETY, *map(str, args)], capture_output=True, text=True, check=check,
                          env=git_env())


# The only repository, worktree and global config keys Yggdrasil's own git calls accept. Anything else,
# including every include, refuses the operation, so no repository setting can name a program for git to run.
# core.hooksPath and core.fsmonitor are allowed because GIT_SAFETY forces both off for Yggdrasil's own calls
# (A10 AC-13), and agent worktrees use Yggdrasil's own hooks folder. At a launch, an agent's own git would run an
# fsmonitor program, so there core.fsmonitor is held to AGENT_EXACT_VALUES (AC-41). Yggdrasil's own agent git keys
# are allowed only with the values in EXACT_VALUES.
ALLOWED_KEY = re.compile(r"^(user\.(name|email)"
                         r"|core\.(bare|repositoryformatversion|filemode|ignorecase|precomposeunicode|logallrefupdates"
                         r"|hookspath|fsmonitor|untrackedcache)"
                         r"|remote\.[^\n]+\.(url|pushurl|fetch)"
                         r"|branch\.[^\n]+\.(remote|merge)"
                         # Inert merge-base bookkeeping that gh, T3 and VS Code write per branch; git never runs them.
                         r"|branch\.[^\n]+\.(gh-merge-base|t3-merge-base|t3-merge-base-ref|vscode-merge-base)"
                         r"|core\.(autocrlf|eol|safecrlf|whitespace)"
                         r"|pull\.(rebase|ff)|merge\.ff|fetch\.prune|push\.default|init\.defaultbranch"
                         r"|color\.[^\n]+"
                         r"|submodule\.[^\n]+\.(url|path|branch)"
                         r"|extensions\.worktreeconfig"
                         r"|ygg\.[a-z0-9]+"
                         r"|lfs\.repositoryformatversion)$")
# Keys allowed only with these values (AC-33, AC-41): what Yggdrasil writes into a lane's worktree
# config so agent git never waits on GPG or an editor, and an fsmonitor that is off.
EXACT_VALUES = {"commit.gpgsign": ("false",), "core.editor": ("true",), "sequence.editor": ("true",)}
AGENT_EXACT_VALUES = dict(EXACT_VALUES, **{"core.fsmonitor": ("false", "no", "off", "0")})
# The standard Git LFS filter is the one helper allowed, by exact value.
LFS_FILTER = {"filter.lfs.clean": "git-lfs clean -- %f", "filter.lfs.smudge": "git-lfs smudge -- %f",
              "filter.lfs.process": "git-lfs filter-process", "filter.lfs.required": "true"}
_SCANNED_SCOPES = ("local", "worktree", "global", "system")
_GIT_VERSION: dict = {}


def require_git_scopes() -> None:
    """Refuse when git is older than 2.26, which lacks `git config --show-scope` (the allowlist needs it)."""
    if "ok" not in _GIT_VERSION:
        import subprocess
        text = subprocess.run(["git", "--version"], capture_output=True, text=True, env=git_env()).stdout
        match = re.search(r"(\d+)\.(\d+)", text)
        _GIT_VERSION["ok"] = bool(match) and (int(match[1]), int(match[2])) >= (2, 26)
        _GIT_VERSION["text"] = text.strip() or "no git"
    if not _GIT_VERSION["ok"]:
        raise refuse("launch precondition: git 2.26 or later is required (found " + _GIT_VERSION["text"] + ")")


def repository_helpers(path, agents: bool = False) -> list[str]:
    """Every config key git would load at `path` (repository, worktree, and the trusted global config; includes
    followed) that is not on the allowlist, or is on it with a value it doesn't accept, in the order git lists
    them. Yggdrasil refuses git operations at a path that has any (AC-04 row 1 names each one). With `agents`, for a
    launch, the checkout an agent's own git will use is checked, and core.fsmonitor must be off too (AC-41)."""
    require_git_scopes()
    listing = trusted_git("-C", path, "config", "--list", "--show-scope", "-z")
    if listing.returncode != 0:
        # A config git cannot read is never treated as clean.
        return ["config (unreadable: " + (listing.stderr.strip().splitlines() or ["no detail"])[0] + ")"]
    found = []
    exact = AGENT_EXACT_VALUES if agents else EXACT_VALUES
    fields = listing.stdout.split("\0")
    for index in range(0, len(fields) - 1, 2):
        scope, (key, _, value) = fields[index], fields[index + 1].partition("\n")
        key = key.strip().lower()
        if scope not in _SCANNED_SCOPES or not key:
            continue
        bad = False
        if key in LFS_FILTER:
            bad = LFS_FILTER[key] != value.strip()
        elif key in exact:
            bad = value.strip().lower() not in exact[key]
        elif key.startswith("alias."):
            # An alias that runs a shell command ("!...") is a helper; one that names a git command is not,
            # and Yggdrasil's own calls never use aliases.
            bad = value.lstrip().startswith("!")
        else:
            bad = not ALLOWED_KEY.match(key)
        if bad and key not in found:
            found.append(key)
    return found


def repository_helper(path, agents: bool = False) -> str | None:
    """The first key `repository_helpers` finds, or None."""
    found = repository_helpers(path, agents)
    return found[0] if found else None


def helper_refusal(path, label: str, agents: bool = False) -> None:
    """Refuse (exit 3) when `path` defines a repository git helper; `label` is the refusal kind. Every such key
    is named. `agents`: the path is a checkout an agent's own git will use (a launch)."""
    keys = repository_helpers(path, agents)
    if keys:
        raise refuse(label + ": " + str(path) + " defines " + ", ".join(keys) + "; Yggdrasil does not run "
                     "repository-defined git helpers")

