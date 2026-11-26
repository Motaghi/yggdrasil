"""Yggdrasil's own Codex sign-in (v1.1.1): one login folder per user, shared by every Codex session.

The login folder `L` is `agent_codex_login_dir` with `~` expanded and made absolute, never resolved: that text is
what the sessions' `auth.json` and `auth.lock` links name. `ygg codex login`, `status` and `logout` run the
installed `codex` with `CODEX_HOME=L`; Yggdrasil never reads, copies, prints or sends the login itself.

Trusted code opens `L` with `O_DIRECTORY | O_NOFOLLOW`, checks it with `fstat` and works through that descriptor,
so the folder cannot be swapped between the check and the use. A shared `flock` on `L/.ygg-sign-in.lock` (the
sign-in lease) is held by the supervisor of every Codex session that got the links; `ygg codex logout` takes it
exclusively without waiting, so a logout never ends the sign-in of a running session.

`route` names how a Codex session would sign in, in Codex's own order (the provider's key, then `CODEX_API_KEY`,
then the Yggdrasil login). `ygg doctor` shows it, and a Codex launch whose route is `none` raises one log-class
`ygg-codex-sign-in` alert per next step.
"""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import util

DEFAULT_LOGIN_DIR = "~/.config/yggdrasil/agent-codex"
LOGIN_FILE = "auth.json"
LOCK_FILE = "auth.lock"
LEASE_FILE = ".ygg-sign-in.lock"
CONFIG_FILE = "config.toml"
# The whole of `L/config.toml`: Codex keeps the login in `L/auth.json`, never in the keychain.
CONFIG_TEXT = 'cli_auth_credentials_store = "file"\n'
# Folders a login folder may never lie in, besides the roots read at use. Tests may replace this tuple.
FIXED_ROOTS = ("/tmp", "/private/tmp")
LAUNCH = "launch precondition: "
LEASE_WAIT = 10.0
LOGOUT_LIMIT = 60
LINK_HOPS = 40

NOT_PROBED_WARNING = "ygg: this Codex version is not probed for the shared sign-in"
SESSIONS_RUNNING = "refused: Codex sessions are running; stop them or let them end, then log out"
NO_LOGIN = "no Yggdrasil Codex login"
NEXT_LOGIN = "run ygg codex login, set CODEX_API_KEY, or use a model provider with its own key"
# Variables the `ygg codex` verbs never pass to Codex: the login it manages must be the one it uses.
WITHHELD = ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN")

PROVIDER_KEY, PROVIDER, API_KEY, LOGIN, NONE = "provider key", "provider", "api key", "yggdrasil login", "none"
RECORD = "codex-sign-in.json"
ALERT_KIND = "ygg-codex-sign-in"


# ---------------------------------------------------------------- the login folder

def absolute(text: str) -> str:
    """`text` with `~` expanded and made absolute, never resolved and never normalised: a `..` stays where it is
    written, because `hop/..` names the parent of wherever the link `hop` lands, not the folder holding `hop`."""
    text = os.path.expanduser(str(text))
    return text if os.path.isabs(text) else os.path.join(os.getcwd(), text)


def login_dir() -> Path:
    """`agent_codex_login_dir` with `~` expanded and made absolute, not resolved and with every `..` kept (the
    text written into links). An empty setting means the default folder."""
    from . import config
    value = config.get("agent_codex_login_dir")
    text = str(value) if isinstance(value, str) and value else DEFAULT_LOGIN_DIR
    return Path(absolute(text))


def is_default(path: Path) -> bool:
    return str(path) == str(Path(absolute(DEFAULT_LOGIN_DIR)))


class LocationRefused(util.YggError):
    """The login folder lies in a forbidden location: every Codex launch refuses it, a reviewer's included."""


def _location_refused(text: str) -> LocationRefused:
    return LocationRefused(util.EXIT_REFUSED, text)


def _refused(text: str) -> util.YggError:
    """A refusal whose signed text already starts with `refused: `, shown exactly once (exit 3)."""
    return util.YggError(util.EXIT_REFUSED, text)


def _message(error: util.YggError) -> str:
    """A refusal's text without the `REFUSED: ` marker, for doctor's detail and the alert."""
    text = error.message
    return text[len("REFUSED: "):] if text.startswith("REFUSED: ") else text


def _roots() -> list[tuple[str, set]]:
    """(shown, spellings) of every folder a login folder may not lie in: the state root, the safety root, the
    tracker, every programme home, every box, /tmp, /private/tmp and $TMPDIR. Each is matched by its own text
    and by its resolved path."""
    from . import quarantine, safety, tracker
    found = [str(util.state_root()), str(safety.root()), str(tracker.root())]
    for programme in tracker.programmes():
        home = tracker.programme_home(programme)
        if home:
            found.append(home)
    found += sorted(quarantine._box_roots())
    found += list(FIXED_ROOTS)
    if os.environ.get("TMPDIR"):
        found.append(os.environ["TMPDIR"])
    roots = []
    for shown in found:
        spellings = {os.path.abspath(shown), os.path.realpath(shown)} - {"/"}
        if spellings:
            roots.append((os.path.realpath(shown), spellings))
    return roots


def check_location(path: Path) -> None:
    """Refuse a login folder whose text, resolved path, or any resolved prefix on the way lies inside a folder an
    agent or Yggdrasil's own state can reach (`_roots`). `path` is walked one component at a time from `/`: a
    `..` steps to the real parent, and a link's target is walked in its place, so every folder the kernel would
    pass through is compared, by text and by device and inode."""
    roots = _roots()
    identities = {}
    for shown, spellings in roots:
        for spelling in spellings:
            try:
                info = os.stat(spelling)
            except OSError:
                continue
            identities.setdefault((info.st_dev, info.st_ino), shown)
    text = absolute(str(path))

    def inside(candidate: str) -> str | None:
        for shown, spellings in roots:
            if any(candidate == root or candidate.startswith(root.rstrip("/") + "/") for root in spellings):
                return shown
        try:
            info = os.stat(candidate)
        except OSError:
            return None
        return identities.get((info.st_dev, info.st_ino))

    def refuse_if_inside(candidate: str) -> None:
        root = inside(candidate)
        if root is not None:
            raise _location_refused("refused: agent_codex_login_dir " + str(path) + " lies inside " + root)

    refuse_if_inside(os.path.normpath(text))
    current = "/"
    pending = [part for part in text.split("/") if part not in ("", ".")]
    hops = 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            current = os.path.dirname(current) or "/"
            refuse_if_inside(current)
            continue
        candidate = os.path.join(current, part)
        refuse_if_inside(candidate)
        if os.path.islink(candidate):
            hops += 1
            if hops > LINK_HOPS:
                raise _location_refused("refused: agent_codex_login_dir " + str(path) + " cannot be resolved (a link loop)")
            try:
                target = os.readlink(candidate)
            except OSError:
                current = candidate
                continue
            if target.startswith("/"):
                current = "/"
            pending = [p for p in target.split("/") if p not in ("", ".")] + pending
            continue
        current = candidate
    refuse_if_inside(os.path.realpath(text))


def _own_folder(info: os.stat_result) -> bool:
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()


def _own_file(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1


def _open_folder(path: Path, prefix: str, *, private: bool) -> int:
    """`path` opened with `O_RDONLY | O_DIRECTORY | O_NOFOLLOW`; a real folder of the user's (and, when `private`,
    with no group or other access), or the refusal."""
    refusal = util.refuse(prefix + str(path) + " is not a real folder of yours")
    try:
        descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise refusal from None
    try:
        info = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise refusal from None
    if not _own_folder(info) or (private and info.st_mode & 0o077):
        os.close(descriptor)
        raise refusal
    return descriptor


def open_dir(path: Path, prefix: str) -> int:
    """`L` opened with `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` and checked with `fstat`: a real folder owned by the
    user with no group or other access. The caller closes the descriptor. `prefix` is `launch precondition: ` at
    launch."""
    return _open_folder(path, prefix, private=True)


def check_login_file(dirfd: int, path: Path, prefix: str) -> bool:
    """Whether `auth.json` in the open login folder is there: True when it is a regular file owned by the user
    with one link, False when absent, and the refusal `<prefix><L>/auth.json is not a regular file of yours`
    when it is there but anything else (a link, a folder, a FIFO, a hard-linked file, another owner)."""
    try:
        info = os.stat(LOGIN_FILE, dir_fd=dirfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        info = None
    if info is None or not _own_file(info):
        raise util.refuse(prefix + str(path) + "/" + LOGIN_FILE + " is not a regular file of yours")
    return True


def login_file_ok(dirfd: int) -> bool:
    """True only when `auth.json` in the open login folder is a regular file owned by the user with one link
    (`fstatat` with `AT_SYMLINK_NOFOLLOW`). False when it is absent, and also when it is unsafe: a caller that
    must refuse an unsafe login uses `check_login_file`."""
    try:
        return check_login_file(dirfd, Path(""), "")
    except util.YggError:
        return False


def _open_file(dirfd: int, name: str, path: Path, prefix: str) -> int:
    """`name` in the open login folder, created or opened with `O_WRONLY | O_CREAT | O_NOFOLLOW | O_NONBLOCK`
    and mode 0600 (no `O_EXCL`, so concurrent callers are safe; no `O_TRUNC`; a FIFO cannot block), checked with
    `fstat` (regular, owned, one link) and set to 0600 with `fchmod`. The descriptor is not inherited."""
    refusal = util.refuse(prefix + str(path) + "/" + name + " is not a regular file of yours")
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=dirfd)
    except OSError:
        raise refusal from None
    try:
        info = os.fstat(descriptor)
        if not _own_file(info):
            raise refusal
        os.fchmod(descriptor, 0o600)
        os.set_inheritable(descriptor, False)
    except OSError:
        os.close(descriptor)
        raise refusal from None
    except util.YggError:
        os.close(descriptor)
        raise
    return descriptor


def ensure_file(dirfd: int, name: str, path: Path, prefix: str) -> None:
    """Create or open `auth.lock` or `.ygg-sign-in.lock` in the open login folder safely (`_open_file`), then close
    it. Refuses with `<prefix><L>/<name> is not a regular file of yours`."""
    os.close(_open_file(dirfd, name, path, prefix))


# ---------------------------------------------------------------- the sign-in lease

def take_lease_shared(timeout: float = LEASE_WAIT) -> int | None:
    """A shared `flock` on `L/.ygg-sign-in.lock` through a descriptor no child inherits, waiting at most `timeout`
    seconds. None on a time-out, and when `L` holds no login or cannot be used (the session then starts anyway).
    Closing the descriptor releases the lease."""
    path = login_dir()
    try:
        folder = open_dir(path, "")
    except util.YggError:
        return None
    try:
        if not login_file_ok(folder):
            return None
        descriptor = _open_file(folder, LEASE_FILE, path, "")
    except util.YggError:
        return None
    finally:
        os.close(folder)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                return None
            time.sleep(0.05)
        except OSError:
            os.close(descriptor)
            return None


def take_lease_exclusive_nb() -> int | None:
    """An exclusive `flock` on `L/.ygg-sign-in.lock` without waiting, for `ygg codex logout`: the descriptor, or
    None while any session holds the lease. Refuses when `L` or the lease file is not safe to use."""
    path = login_dir()
    folder = open_dir(path, "")
    try:
        descriptor = _open_file(folder, LEASE_FILE, path, "")
    finally:
        os.close(folder)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


# ---------------------------------------------------------------- the route

def platform_step(platform: str) -> str:
    """The next step for a Codex version that is probed, but not on this platform."""
    return "Codex's shared sign-in is not probed on " + platform + "; use a model provider with its own key"


def not_probed_step(version: str | None) -> str:
    """The next step for a Codex version not probed here. On a platform where no version is probed, installing
    another version cannot help, so the step names the platform whatever the version."""
    from . import codexlaunch
    current = codexlaunch.current_platform()
    if current is not None and not any(p == current for p, _ in codexlaunch.CODEX_PROBED_VERSIONS):
        return platform_step(current)
    platform = codexlaunch.unprobed_platform(version, current)
    if platform is not None:
        return platform_step(platform)
    text = str(version or "")
    if text.startswith("codex-cli "):
        who = "Codex " + text[len("codex-cli "):]
    else:
        who = "This Codex version"
    return (who + " is not probed for the shared sign-in; use a model provider with its own key, or install " +
            codexlaunch._probed_line(None))


def _unsafe_entry(folder: int, path: Path, name: str) -> str | None:
    """`<L>/<name> is not a regular file of yours` when that entry exists and is anything but a regular file of
    the user's with one link (a link, a FIFO, a folder, a hard-linked file); None when it is absent or safe. An
    `lstat` through the folder's descriptor: nothing is created, opened or changed."""
    try:
        info = os.stat(name, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        info = None
    if info is not None and _own_file(info):
        return None
    return str(path) + "/" + name + " is not a regular file of yours"


def login_state() -> tuple[bool, str]:
    """(usable, detail) of the Yggdrasil login: usable when `L` passes the location and folder checks,
    `L/auth.json` is a regular file of the user's, and an existing `L/auth.lock` is one too (a launch would refuse
    or skip the links otherwise). Otherwise the detail is the next step. An unsafe sign-in lease file leaves the
    login usable and is named in the detail. Reads only."""
    path = login_dir()
    try:
        check_location(path)
    except util.YggError as error:
        return False, _message(error)
    if not os.path.lexists(str(path)):
        return False, NEXT_LOGIN if is_default(path) else str(path) + " does not exist (unmounted or removed?)"
    try:
        folder = open_dir(path, "")
    except util.YggError as error:
        return False, _message(error)
    try:
        try:
            present = check_login_file(folder, path, "")
        except util.YggError as error:
            return False, _message(error)
        if present:
            lock = _unsafe_entry(folder, path, LOCK_FILE)
            if lock is not None:
                return False, lock
            lease = _unsafe_entry(folder, path, LEASE_FILE)
            if lease is not None:
                return True, str(path) + "; " + lease + ", so sessions run without the sign-in lease and a " \
                    "logout is refused; remove it"
            return True, str(path)
        try:
            os.stat(CONFIG_FILE, dir_fd=folder, follow_symlinks=False)
            configured = True
        except OSError:
            configured = False
    finally:
        os.close(folder)
    if configured:
        return False, "the login is not in " + str(path) + "/" + LOGIN_FILE + "; run ygg codex login again"
    return False, NEXT_LOGIN


def _value(environment: dict, name: str) -> bool:
    return bool(str(environment.get(name) or "").strip())


def _passthrough_names() -> list[str]:
    from . import launcher
    return launcher.passthrough_names()


def check_present_location(path: Path) -> None:
    """The location check a Codex launch makes: only when something exists at `path` (a folder, a file or a link).
    With nothing there a launch makes no links and leaves `path` alone, so there is nothing to refuse; the three
    `ygg codex` verbs check whether or not it exists."""
    if os.path.lexists(str(path)):
        check_location(path)


def location_refusal() -> str | None:
    """The location refusal of the configured login folder, as text, or None when it may be used (or when nothing
    exists there, as at a launch)."""
    try:
        check_present_location(login_dir())
    except util.YggError as error:
        return _message(error)
    return None


def route(session_env: dict, provider: dict | None, version_probed: bool, *,
          version: str | None = None, granted: dict | None = None) -> tuple[str, str]:
    """The sign-in route of one Codex session and its detail, in Codex's own order, from the environment that
    session gets and the provider in its hermetic config (None for Codex's default OpenAI provider).

    A provider with an `env_key` uses that key alone: `provider key` when it reaches the session with a value that
    is not blank, else `none`. A provider without one is `provider`. Only the default provider looks further:
    `api key` for a non-blank `CODEX_API_KEY`, else `yggdrasil login` when the login folder passes its checks,
    else `none`. On a version not probed for the shared sign-in the default provider has no route. A detail names
    only the provider's `name`, never a key, a token or any other provider field.

    A login folder that exists in a forbidden location is `none` with its refusal whatever else is set, as every
    Codex launch refuses it. `granted` is what a launched session actually got (`links`, and the launch's `sign_in` text for a
    reviewer that got none): with it the login route holds only when the links were made and the folder still holds a usable login; without it (doctor)
    the login folder is judged by its read-only checks."""
    refusal = location_refusal()
    if refusal is not None:
        return NONE, refusal
    if provider is not None:
        name = str(provider.get("name") or "")
        key = provider.get("env_key")
        if isinstance(key, str) and key:
            if _value(session_env, key):
                return PROVIDER_KEY, name
            if _value(os.environ, key) and key not in _passthrough_names():
                # The conductor has it, but the agents' environment drops it.
                return NONE, "list " + key + " under agent_env_passthrough so agents get it"
            return NONE, "set " + key + " for the conductor"
        return PROVIDER, name
    if not version_probed:
        return NONE, not_probed_step(version)
    if _value(session_env, "CODEX_API_KEY"):
        return API_KEY, ""
    usable, detail = login_state()
    if granted is not None and granted.get("links"):
        # Links alone are not a sign-in: a logout may have removed the login since they were made.
        return (LOGIN, str(login_dir())) if usable else (NONE, detail)
    if granted is not None:
        # No links: the launch's own reason when it gave one, else why the folder holds no usable login.
        given = str(granted.get("sign_in") or "").strip()
        return NONE, given or (NEXT_LOGIN if usable else detail)
    return (LOGIN, detail) if usable else (NONE, detail)


def next_step(route_name: str, detail: str) -> str:
    """The alert text: what the user does next when the route is `none`; empty for any other route."""
    return str(detail or NEXT_LOGIN) if route_name == NONE else ""


def session_provider() -> dict | None:
    """The model provider a hermetic Codex home names: None for Codex's default OpenAI provider, else that
    provider's allowed table with its `name` (the provider's id when the table has none)."""
    from . import codexlaunch
    reader = getattr(codexlaunch, "user_provider", None) or codexlaunch._user_provider
    name, table = reader()
    if not name or name == "openai":
        return None
    provider = dict(table or {})
    provider["name"] = str(provider.get("name") or name)
    return provider


def session_env(data: dict | None = None) -> dict:
    """The environment the Codex session described by `data` (an activation record) would get; with no `data`, a
    Codex builder's. The launcher's own builder (`launcher.codex_session_env`) is used when it has one, so doctor
    and the alert judge what a session really gets; the rest of this function stands in for it until then."""
    from . import launcher
    builder = getattr(launcher, "codex_session_env", None)
    if builder is not None:
        return builder(data)
    data = dict({"node": "", "activation": "", "role": "tl", "pinned": {}}, **(data or {}))
    data["cli"] = "codex"
    if data.get("role") == "reviewer":
        data["activation"] = ""  # asking about a reviewer's environment makes no review folder
    environment = launcher.child_env(data)
    reviewer_unprobed = data.get("role") == "reviewer" and not (data.get("codex_pin") or {}).get("probed")
    if "CODEX_API_KEY" not in environment and os.environ.get("CODEX_API_KEY") is not None:
        environment["CODEX_API_KEY"] = os.environ["CODEX_API_KEY"]
    if reviewer_unprobed:
        environment.pop("CODEX_API_KEY", None)
    return environment


# ---------------------------------------------------------------- doctor, the alert and status

def doctor_report() -> dict:
    """`{"route", "detail"}` for `ygg doctor`: the route of a Codex session started now, with a note when the
    installed Codex version is not probed. Never a key or a token."""
    from . import codexlaunch
    try:
        found = codexlaunch.probe(strict=False)
        probed = bool(found.get("probed"))
        name, detail = route(session_env(), session_provider(), probed, version=found.get("version"))
        if not probed:
            note = not_probed_step(found.get("version"))
            if note not in detail:
                detail = (detail + "; " if detail else "") + note
    except util.YggError as error:
        name, detail = NONE, _message(error)
    return {"route": name, "detail": util.redact(detail)}


def _record_path() -> Path:
    from . import state
    return state.root() / RECORD


def note_route(route_name: str, detail: str) -> None:
    """Called for every Codex launch: a route of `none` raises one log-class `ygg-codex-sign-in` alert, again only
    when its next step changes (`<state root>/codex-sign-in.json` keeps the last one, so separate tick processes
    raise it once); any other route clears the record."""
    from . import state
    path = _record_path()
    step = next_step(route_name, detail)
    with util.locked():
        known = util.read_json(path, {}) or {}
        if route_name != NONE:
            if path.exists():
                path.unlink()
                util.fsync_dir(path.parent)
            return
        if isinstance(known, dict) and known.get("next_step") == step:
            return
        util.write_json(path, {"next_step": step, "at": util.iso()})
    state.alert(ALERT_KIND, "A Codex session is starting with no sign-in: " + step, next_step=step)


def note_launch(data: dict) -> None:
    """The sign-in route of the Codex session `data` describes (its activation record with `codex_pin`), recorded
    for the alert. Best effort: it never stops a launch."""
    try:
        pin = data.get("codex_pin") or {}
        probed = data.get("role") != "reviewer" or bool(pin.get("probed"))
        granted = {"links": bool(data.get("codex_login_links")), "sign_in": data.get("codex_sign_in")}
        name, detail = route(session_env(data), session_provider(), probed, version=pin.get("version"),
                             granted=granted)
        note_route(name, detail)
    except Exception:  # noqa: BLE001  (a reporting step: a failure here must never abort the launch)
        return


def status_lines() -> list[str]:
    """`codex sign-in: none (<next step>)` while the last Codex launch had no route."""
    try:
        known = util.read_json(_record_path(), {}) or {}
    except util.YggError:
        return []
    if not isinstance(known, dict) or not known.get("next_step"):
        return []
    return ["codex sign-in: none (" + str(known["next_step"]) + ")"]


# ---------------------------------------------------------------- the `ygg codex` verbs

def _codex() -> tuple[str, dict]:
    """The installed Codex binary and its version check (never run for its version in test mode)."""
    from . import codexlaunch
    found = codexlaunch.probe(strict=False)
    if not found.get("path"):
        raise util.refuse("codex was not found; install codex-cli and run this again")
    return str(found["path"]), found


def _child_env(path: Path) -> dict:
    environment = {k: v for k, v in os.environ.items() if k not in WITHHELD}
    environment["CODEX_HOME"] = str(path)
    return environment


def _warn(found: dict) -> None:
    from . import codexlaunch
    if not found.get("probed"):
        platform = codexlaunch.unprobed_platform(found.get("version"), codexlaunch.current_platform())
        text = "ygg: " + platform_step(platform) if platform is not None else NOT_PROBED_WARNING
        print(text, file=sys.stderr, flush=True)


def _exit_code(code: int) -> int:
    return code if code >= 0 else 128 - code


def _run(binary: str, args: list[str], path: Path, limit: float | None = None) -> int:
    """Codex with `CODEX_HOME=L`, standard input, output and error passed through, and its exit code."""
    sys.stdout.flush()
    process = subprocess.Popen([binary] + args, env=_child_env(path))
    try:
        return _exit_code(process.wait(timeout=limit))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise util.YggError(util.EXIT_UNCERTAIN, "codex " + " ".join(args) + " did not finish within " +
                            str(int(limit or 0)) + " seconds; check with ygg codex status") from None
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise


def _write_config(folder: int, path: Path) -> None:
    """`L/config.toml` holding only the file-storage line, written under a new name and linked into place without
    replacing anything: when something appeared at `config.toml` since the folder was found empty, the login is
    refused and that entry is left alone."""
    temporary = ".config.toml." + uuid.uuid4().hex
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
    try:
        data = memoryview(CONFIG_TEXT.encode())
        while data:
            data = data[os.write(descriptor, data):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, CONFIG_FILE, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
    except OSError:
        raise _refused("refused: agent_codex_login_dir " + str(path) + " is not empty and is not a Yggdrasil "
                       "Codex login folder; choose a new folder") from None
    finally:
        os.unlink(temporary, dir_fd=folder)
    os.fsync(folder)


def _set_up_before(folder: int) -> str | None:
    """`empty` when the open folder is empty, `ours` when it holds a `config.toml` that is a regular file whose
    whole text is Yggdrasil's (a folder an earlier `ygg codex login` set up), else None. Reads only, through the
    folder's descriptor."""
    if not os.listdir(folder):
        return "empty"
    try:
        descriptor = os.open(CONFIG_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        wanted = CONFIG_TEXT.encode()
        data = b""
        while len(data) <= len(wanted):
            chunk = os.read(descriptor, len(wanted) + 1 - len(data))
            if not chunk:
                break
            data += chunk
        return "ours" if data == wanted else None
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _prepare(path: Path) -> None:
    """Create `L` when it is missing (0700; its missing parents only for the default path), refuse anything but a
    real folder of the user's, refuse an existing folder that is neither empty nor one an earlier login set up
    (so a Codex home of the user's own is never changed), then set it to 0700 through its descriptor and, in an
    empty folder, create `L/config.toml` without replacing anything."""
    if not os.path.lexists(str(path)):
        if not os.path.isdir(str(path.parent)):
            if not is_default(path):
                raise util.refuse("the folder that holds " + str(path) + " does not exist (unmounted or removed?); "
                                  "create it or change agent_codex_login_dir")
            os.makedirs(str(path.parent), mode=0o700, exist_ok=True)
        try:
            os.mkdir(str(path), 0o700)
        except FileExistsError:
            pass
    folder = _open_folder(path, "", private=False)
    try:
        found = _set_up_before(folder)
        if found is None:
            raise _refused("refused: agent_codex_login_dir " + str(path) + " is not empty and is not a Yggdrasil "
                           "Codex login folder; choose a new folder")
        if found == "empty":
            _write_config(folder, path)
        os.fchmod(folder, 0o700)
    finally:
        os.close(folder)


def login(flag: str | None) -> int:
    """`ygg codex login [--device-auth | --with-api-key]`."""
    path = login_dir()
    check_location(path)
    binary, found = _codex()
    _prepare(path)
    _warn(found)
    return _run(binary, ["login"] + ([flag] if flag else []), path)


def status() -> int:
    """`ygg codex status`: `codex login status` in the login folder."""
    path = login_dir()
    check_location(path)
    binary, found = _codex()
    if not os.path.lexists(str(path)):
        print(NO_LOGIN)
        return 1
    os.close(open_dir(path, ""))
    _warn(found)
    return _run(binary, ["login", "status"], path)


def logout() -> int:
    """`ygg codex logout`: `codex logout` under the exclusive sign-in lease, within 60 seconds; refused while any
    Codex session holds the lease."""
    path = login_dir()
    check_location(path)
    if not os.path.lexists(str(path)):
        print(NO_LOGIN)
        return 0
    binary, found = _codex()
    lease = take_lease_exclusive_nb()
    if lease is None:
        raise _refused(SESSIONS_RUNNING)
    try:
        _warn(found)
        return _run(binary, ["logout"], path, LOGOUT_LIMIT)
    finally:
        os.close(lease)


def verb(action: str | None, flag: str | None = None) -> int:
    if action == "login":
        return login(flag)
    if action == "status":
        return status()
    if action == "logout":
        return logout()
    raise util.usage("ygg codex login [--device-auth | --with-api-key] | status | logout")
