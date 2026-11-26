"""The hermetic Codex launch and the Codex version pin (v1.1.1).

Every Codex launch gets a Codex home that Yggdrasil writes whole (`<state root>/launch/<activation>/codex-home/`),
so nothing in the user's own Codex home (hooks, MCP servers, skills, trust entries, profiles) reaches an agent.
Codex builders (PM, team lead, worker, senior user) and every helper run start only on a probed platform and
Codex CLI version; the project reviewer's version is recorded and never refused.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
import shutil
import subprocess
import sys
from pathlib import Path

from . import state, util

# From `codex features list` on codex-cli 0.170.0 (macOS). Names may be added; removing one needs a new probe of that version.
CODEX_DISABLED_FEATURES = (
    "hooks", "apps", "plugins", "remote_plugin", "plugin_sharing", "browser_use", "browser_use_external",
    "computer_use", "in_app_browser", "in_app_local_automation", "multi_agent", "skill_mcp_dependency_install",
    "image_generation", "tool_suggest", "system_proxy_fallback", "respect_system_proxy",
)
# Pairs of `sys.platform` and the exact `codex --version` line probed with Yggdrasil (every row of
# `scripts/codex-reprobe`). Adding a pair is a code change made only after that script passes on it.
CODEX_PROBED_VERSIONS = (("darwin", "codex-cli 0.170.0"),)

# The provider table keys copied from the user's Codex config; nothing else of it reaches an agent.
PROVIDER_KEYS = ("name", "base_url", "env_key", "wire_api", "query_params", "http_headers", "env_http_headers")
# The sandbox keys of the command line, repeated in the hermetic config.
SANDBOX_SETTINGS = (("writable_roots", []), ("network_access", False), ("exclude_slash_tmp", True),
                    ("exclude_tmpdir_env_var", True))
UNKNOWN_VERSION = "an unknown Codex version"
# Keys Codex reads for its own sign-in; never visible to the commands an agent runs.
CODEX_KEY_NAMES = ("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN")
# The two names in a hermetic Codex home that link to the shared Yggdrasil login.
LOGIN_LINKS = ("auth.json", "auth.lock")
UNPROBED_SIGN_IN = "unprobed: no shared sign-in"
VERSION_TIMEOUT = 10
LAUNCH = "launch precondition: "
RECHECK = "re-check refused: "


def _probed_line(platform: str | None) -> str:
    versions = [v for p, v in CODEX_PROBED_VERSIONS if platform is None or p == platform]
    return " or ".join(versions or [v for _, v in CODEX_PROBED_VERSIONS])


def platform_text() -> str:
    pairs = [p + " with " + v for p, v in CODEX_PROBED_VERSIONS]
    return " or ".join(pairs)


def current_platform() -> str | None:
    """`sys.platform`, or under YGG_TEST_MODE=1 the value of YGG_TEST_PLATFORM; None (not checked) in test mode
    when that is unset, so the earlier acceptance suites run the same on any host."""
    if util.test_mode():
        return util.env("YGG_TEST_PLATFORM")
    return sys.platform


def platform_refusal(platform: str) -> str:
    return "Codex builders have not been probed on " + platform + "; they run only on " + platform_text()


def version_refusal(line: str, platform: str | None) -> str:
    return line + " has not been probed with Yggdrasil; Codex builders run only on " + _probed_line(platform)


def require_platform(platform: str | None, prefix: str) -> None:
    """Refuse a platform with no probed pair; None (test mode without YGG_TEST_PLATFORM) is not checked."""
    if platform is not None and not any(p == platform for p, _ in CODEX_PROBED_VERSIONS):
        raise util.refuse(prefix + platform_refusal(platform))


def is_probed(line: str | None, platform: str | None) -> bool:
    """The exact version line is paired with the platform (any platform when that is None)."""
    return any(v == line and (platform is None or p == platform) for p, v in CODEX_PROBED_VERSIONS)


def unprobed_platform(line: str | None, platform: str | None) -> str | None:
    """The platform when the exact version line is probed on some other platform but not on this one; None when
    it is probed here, probed nowhere, or the platform is not checked."""
    if platform is None or is_probed(line, platform):
        return None
    return platform if any(v == line for _, v in CODEX_PROBED_VERSIONS) else None


def version_line(text: str) -> str:
    """One `--version` output as the pin compares it: one trailing newline removed, and anything but a single
    `codex-cli ` line read as an unknown version."""
    if text.endswith("\n"):
        text = text[:-1]
    if not text or "\n" in text or "\r" in text or not text.startswith("codex-cli "):
        return UNKNOWN_VERSION
    return text


def resolve_binary() -> str | None:
    """The real path (links resolved) of the Codex binary Yggdrasil would start: YGG_CODEX_BIN, else `codex` on
    PATH; None when there is none."""
    name = util.env("YGG_CODEX_BIN", "codex")
    found = name if os.sep in name else shutil.which(name)
    if not found:
        return None
    return os.path.realpath(found)


def identity(path: str) -> dict | None:
    """Device, inode, size and modification time of `path` (followed), or None when it cannot be read."""
    try:
        found = os.stat(path)
    except OSError:
        return None
    return {"device": found.st_dev, "inode": found.st_ino, "size": found.st_size, "mtime_ns": found.st_mtime_ns}


def same_binary(recorded: dict | None) -> bool:
    """A fresh stat of the checked path shows the identity recorded at the check."""
    if not recorded or not recorded.get("path"):
        return False
    return identity(str(recorded["path"])) == recorded.get("identity")


def trusted_env() -> dict:
    """The environment of the `--version` run: PATH, HOME, LANG and a TMPDIR under the state root, never a box."""
    folder = util.ensure_dir(state.root() / "tmp" / "codex-version")
    environment = {"TMPDIR": str(folder)}
    for name in ("PATH", "HOME", "LANG"):
        if os.environ.get(name) is not None:
            environment[name] = os.environ[name]
    return environment


def _version_output(path: str) -> str | None:
    """The binary's version line as found, or None when the output is not exactly one line."""
    if util.test_mode():
        # The fake `codex` would record a --version call as an agent launch, so the binary is never run here.
        text = os.environ.get("YGG_TEST_CODEX_VERSION")
        if text is None:
            return None
    else:
        try:
            result = subprocess.run([path, "--version"], env=trusted_env(), stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=VERSION_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError):
            return UNKNOWN_VERSION
        if result.returncode != 0:
            return UNKNOWN_VERSION
        text = result.stdout.decode("utf-8", errors="replace")
    return version_line(text)


def checked_in_test_mode() -> bool:
    """Whether the version is checked at all: always outside test mode, and in test mode only with
    YGG_TEST_CODEX_VERSION set."""
    return not util.test_mode() or os.environ.get("YGG_TEST_CODEX_VERSION") is not None


def probe(*, strict: bool, prefix: str = LAUNCH) -> dict:
    """Check (strict, for builders and helpers) or record (the project reviewer) the platform and the Codex
    binary. Returns {"path", "identity", "version", "probed", "checked"}; a strict check that fails raises the
    refusal with `prefix`."""
    platform = current_platform()
    if strict:
        require_platform(platform, prefix)
    path = resolve_binary()
    found = identity(path) if path else None
    checked = checked_in_test_mode()
    line = None
    if checked:
        line = _version_output(path) if path and found is not None else UNKNOWN_VERSION
        if line != UNKNOWN_VERSION and path and identity(path) != found:
            line = UNKNOWN_VERSION  # the binary changed while it was read
    probed = (not checked) or is_probed(line, platform)
    if strict and not probed:
        raise util.refuse(prefix + version_refusal(str(line), platform))
    return {"path": path, "identity": found, "version": line, "probed": probed, "checked": checked}


def check_builder(prefix: str = LAUNCH) -> dict:
    return probe(strict=True, prefix=prefix)


def check_helper() -> dict:
    """The check before every helper run (the receive step, lane sync, a launch's tending)."""
    return probe(strict=True, prefix=RECHECK)


def version_text(found: dict) -> str:
    if not found.get("checked"):
        return "codex version: not checked (test mode)"
    text = "codex version: " + str(found.get("version"))
    if not found.get("probed"):
        text += " (not probed)"
    return text


# ---------------------------------------------------------------- the hermetic home

def home_path(activation: str) -> Path:
    return state.root() / "launch" / activation / "codex-home"


def _source_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path(os.path.expanduser("~")) / ".codex"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(_toml_key(str(k)) + " = " + _toml_value(v) for k, v in value.items()) + "}"
    raise ValueError("unsupported value in the Codex provider table")


def _toml_key(key: str) -> str:
    if key and all(c.isalnum() or c in "-_" for c in key) and key.isascii():
        return key
    return _toml_string(key)


def _user_provider() -> tuple[str | None, dict | None]:
    """The user's `model_provider` value and that one provider table (allowed keys only), if present."""
    path = _source_home() / "config.toml"
    try:
        import tomllib
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        return None, None
    name = data.get("model_provider")
    if not isinstance(name, str) or not name:
        return None, None
    table = (data.get("model_providers") or {}).get(name)
    if not isinstance(table, dict):
        return name, None
    kept = {k: table[k] for k in PROVIDER_KEYS if k in table}
    try:
        _toml_value(kept)
    except ValueError:
        return name, None
    return name, kept


def user_provider() -> tuple[str | None, dict | None]:
    """The provider the hermetic config names: the user's `model_provider` value and that one provider table
    (allowed keys only), or (None, None) for Codex's default provider."""
    return _user_provider()


def excluded_names(table: dict | None = None, *, read: bool = True) -> list[str]:
    """The variables Codex keeps out of the commands it runs for the agent: the Codex and OpenAI keys, the
    provider's `env_key` and every variable its `env_http_headers` names."""
    if table is None and read:
        table = _user_provider()[1]
    names = list(CODEX_KEY_NAMES)
    table = table or {}
    if isinstance(table.get("env_key"), str) and table["env_key"]:
        names.append(table["env_key"])
    headers = table.get("env_http_headers")
    if isinstance(headers, dict):
        names += [v for v in headers.values() if isinstance(v, str) and v]
    return list(dict.fromkeys(names))


def config_text(*, reviewer: bool = False) -> str:
    """The whole `config.toml` of a hermetic Codex home."""
    return _config(reviewer)[0]


def _config(reviewer: bool) -> tuple[str, list[str]]:
    """The hermetic `config.toml` and the variable names its `shell_environment_policy` excludes, from one read
    of the user's provider, so the builder's command line repeats exactly what the file says."""
    lines = []
    provider, table = _user_provider()
    excluded = excluded_names(table, read=False)
    if provider:
        lines.append("model_provider = " + _toml_string(provider))
    lines.append('approval_policy = "never"')
    if not reviewer:
        lines.append('sandbox_mode = "workspace-write"')
    lines.append('web_search = "disabled"')
    # The login stays in a file, never in the keychain: the shared login is the file the links point to.
    lines.append('cli_auth_credentials_store = "file"')
    lines.append("")
    # Codex's own process keeps the keys; the commands it runs for the agent do not get them.
    lines.append("[shell_environment_policy]")
    lines.append("exclude = " + _toml_value(excluded))
    lines.append("")
    lines.append("[sandbox_workspace_write]")
    for key, value in SANDBOX_SETTINGS:
        lines.append(key + " = " + _toml_value(value))
    lines.append("")
    lines.append("[agents]")
    lines.append("enabled = false")
    lines.append("")
    lines.append("[skills]")
    lines.append("include_instructions = false")
    lines.append("")
    lines.append("[features]")
    for name in CODEX_DISABLED_FEATURES:
        lines.append(name + " = false")
    if provider and table is not None:
        lines.append("")
        lines.append("[model_providers." + _toml_key(provider) + "]")
        for key, value in table.items():
            lines.append(_toml_key(key) + " = " + _toml_value(value))
    return "\n".join(lines) + "\n", excluded


def _write_new(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_home(activation: str, *, reviewer: bool = False, probed: bool = True, version: str | None = None) -> dict:
    """Write `<state root>/launch/<activation>/codex-home/` (0700) holding `config.toml` and, when the user has a
    safe Yggdrasil Codex login and the Codex version is probed, `auth.json` and `auth.lock` as links into the login
    folder. The launching environment's own Codex login is never read.

    Returns {"home", "links", "sign_in", "excluded", "login"}: whether the links were made; the text `ygg status`
    shows for a reviewer that got none for a reason (an unprobed version, named with the platform when
    that version is probed only elsewhere, or a login folder that failed a check);
    the names the config keeps from the agent's commands; and the login folder linked (the lease's folder). A
    builder whose login folder fails a check is refused instead."""
    home = home_path(activation)
    util.ensure_dir(home.parent)
    if home.is_symlink() or (home.exists() and not home.is_dir()):
        raise util.refuse(LAUNCH + "the Codex home " + str(home) + " is not a folder Yggdrasil made")
    home.mkdir(mode=0o700, exist_ok=True)
    os.chmod(home, 0o700)
    for entry in os.listdir(home):
        if entry not in ("config.toml",) + LOGIN_LINKS:
            raise util.refuse(LAUNCH + "the Codex home " + str(home) + " holds " + entry)
    text, excluded = _config(reviewer)
    _write_new(home / "config.toml", text.encode(), 0o600)
    links, sign_in = False, None
    from . import codexsignin
    try:
        login = checked_login(probed=probed)
    except codexsignin.LocationRefused:
        raise  # a forbidden location refuses every Codex launch, a reviewer's too
    except util.YggError as error:
        if not reviewer:
            raise
        login, sign_in = None, _refusal_text(error)
    if login is None and sign_in is None and not probed:
        sign_in = UNPROBED_SIGN_IN
        platform = unprobed_platform(version, current_platform())
        if platform is not None:
            sign_in += "; " + codexsignin.platform_step(platform)
    descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if login is not None:
            for name in LOGIN_LINKS:
                _replace_link(descriptor, name, str(login) + "/" + name)
            links = True
        else:
            _unlink_names(descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {"home": home, "links": links, "sign_in": sign_in, "excluded": excluded,
            "login": str(login) if links else None}


def _refusal_text(error: util.YggError) -> str:
    return str(error.message).removeprefix("REFUSED: ")


def checked_login(*, probed: bool = True) -> Path | None:
    """The login folder `L` a Codex session links to, or None when it gets no links.

    The location check refuses every Codex launch while something exists at `L`. The folder and file checks work through one descriptor of
    `L` opened without following links, so `L` cannot be swapped between the check and the use; they raise for a
    builder and for a probed reviewer (the caller turns that into the reviewer's status text). A missing `L`, a
    missing `auth.json` and an unprobed version mean no links; launches never create `L`."""
    from . import codexsignin
    login = codexsignin.login_dir()
    if not os.path.lexists(login):
        return None
    # One existence probe decides both: whatever is at `L` from here on is checked before it is used.
    codexsignin.check_location(login)
    if not probed:
        return None
    descriptor = codexsignin.open_dir(login, LAUNCH)
    try:
        codexsignin.check_login_file(descriptor, login, LAUNCH)
        if not codexsignin.login_file_ok(descriptor):
            return None
        codexsignin.ensure_file(descriptor, "auth.lock", login, LAUNCH)
    finally:
        os.close(descriptor)
    return login


def _replace_link(descriptor: int, name: str, target: str) -> None:
    """Put a link `name` with the exact target text into the folder: written under a temporary name and renamed
    over the old entry, which is never opened."""
    temporary = "." + name + "." + str(os.getpid()) + "." + os.urandom(4).hex() + ".tmp"
    os.symlink(target, temporary, dir_fd=descriptor)
    try:
        os.rename(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
    except OSError:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except OSError:
            pass
        raise


def _unlink_names(descriptor: int) -> None:
    for name in LOGIN_LINKS:
        try:
            os.unlink(name, dir_fd=descriptor)
        except FileNotFoundError:
            pass


def drop_links(activation: str) -> None:
    """At settlement, by any path: remove `auth.json` and `auth.lock` from the activation's Codex home by name,
    never following them, so a settled launch folder holds no live link to the login. Harmless to repeat, and a
    no-op for a launch with no Codex home."""
    try:
        descriptor = os.open(home_path(activation), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        for name in LOGIN_LINKS:
            try:
                os.unlink(name, dir_fd=descriptor)
            except OSError:
                pass  # absent, or a folder an agent could never have made here: nothing to remove
    finally:
        os.close(descriptor)


# The one process keeping an activation's sign-in lease (its supervisor, or after a supervisor died, a holder)
# holds an exclusive lock on this file in the activation's launch folder for as long as it keeps the lease.
HOLDER_LOCK = "sign-in-lease.lock"
LEASE_FILE = ".ygg-sign-in.lock"


def holder_lock_path(activation: str) -> Path:
    return state.root() / "launch" / activation / HOLDER_LOCK


def claim_holder(path: Path) -> int | None:
    """An exclusive lock, without waiting, on an activation's holder lock file, through a descriptor no child
    inherits; None when another process holds it or the file cannot be used."""
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        descriptor = os.open(str(path), flags, 0o600)
    except OSError:
        return None
    try:
        found = os.fstat(descriptor)
        if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid():
            os.close(descriptor)
            return None
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def take_lease_at(login: Path, timeout: float) -> int | None:
    """The shared sign-in lease on `<login>/.ygg-sign-in.lock` for exactly this login folder (the one recorded at
    launch, never one read again from configuration), with the login folder's checks; None when the folder holds
    no safe login or the lease is not had within `timeout` seconds. The descriptor is not inherited."""
    from . import codexsignin
    try:
        folder = codexsignin.open_dir(login, "")
    except util.YggError:
        return None
    try:
        if not codexsignin.login_file_ok(folder):
            return None
        codexsignin.ensure_file(folder, LEASE_FILE, login, "")
        descriptor = os.open(LEASE_FILE, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=folder)
    except (OSError, util.YggError):
        return None
    finally:
        os.close(folder)
    found = os.fstat(descriptor)
    if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid() or found.st_nlink != 1:
        os.close(descriptor)
        return None
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


def ensure_lease_holder(activation: str, data: dict, pid, started: str | None) -> bool:
    """Recovery found a Codex agent of this activation alive. When its session got the links and no process
    keeps its sign-in lease (its supervisor died, with or without recording the agent), start a detached holder
    that takes the lease again and keeps it until the agent is gone. One holder per activation: a running
    supervisor or holder owns the activation's holder lock, so calling this on every tick starts at most one.
    The holder gets the login folder recorded at launch and the launch's pinned, absolute configuration paths,
    so it never reads a different configuration. Returns whether a holder was started."""
    if not data.get("codex_login_links") or not started or not isinstance(pid, int) or isinstance(pid, bool):
        return False
    path = holder_lock_path(activation)
    if not path.parent.is_dir() or path.parent.is_symlink():
        return False
    probe = claim_holder(path)
    if probe is None:
        return False  # held by the supervisor or a holder (or unusable): nothing to start
    os.close(probe)
    login = data.get("codex_login_dir")
    if not login:
        from . import codexsignin
        login = str(codexsignin.login_dir())  # a launch from before the folder was recorded
    environment = dict(os.environ, **{k: str(v) for k, v in (data.get("pinned") or {}).items()})
    try:
        subprocess.Popen([sys.executable, "-I", "-m", "yggdrasil.codexlease", str(path), str(login), str(pid),
                          str(started)], cwd=str(path.parent), env=environment, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


def write_version(activation: str, found: dict) -> Path:
    path = state.root() / "launch" / activation / "codex-version"
    util.ensure_dir(path.parent)
    _write_new(path, (version_text(found) + "\n").encode(), 0o600)
    return path


# ---------------------------------------------------------------- the command line

def critical_overrides(excluded: list[str] | None = None) -> list[str]:
    """`--ignore-rules`, `--strict-config` and the repeated critical values (the P-5 row of `scripts/codex-reprobe`
    checks them on 0.170.0), with the file credential store and the key exclusion the hermetic config holds
    (both accepted under `--strict-config` on 0.170.0)."""
    names = excluded_names() if excluded is None else list(excluded)
    args = ["--ignore-rules", "--strict-config",
            "-c", 'approval_policy="never"', "-c", "mcp_servers={}", "-c", 'web_search="disabled"',
            "-c", "agents.enabled=false", "-c", "skills.include_instructions=false",
            "-c", 'cli_auth_credentials_store="file"',
            "-c", "shell_environment_policy.exclude=[" + ",".join(_toml_string(n) for n in names) + "]"]
    args += sandbox_overrides()
    for name in CODEX_DISABLED_FEATURES:
        args += ["--disable", name]
    return args


def sandbox_overrides() -> list[str]:
    args = []
    for key, value in SANDBOX_SETTINGS:
        args += ["-c", "sandbox_workspace_write." + key + "=" + _toml_value(value).replace(" ", "")]
    return args


def unprobed_review_lines(projects: set) -> list[str]:
    """`ygg status` marks a review whose project reviewer ran on a Codex version or platform not in the list as
    `unprobed`: that reviewer's hermetic settings are verified on the probed pairs only. A review run that got
    no shared sign-in for a reason (an unprobed version, or a login folder that failed a check) gets a second
    line with that reason."""
    folder = state.root() / "activations"
    lines = []
    if not folder.is_dir():
        return lines
    for path in sorted(folder.glob("*.json")):
        data = util.read_json(path, None)
        if not isinstance(data, dict) or data.get("role") != "reviewer" or data.get("cli") != "codex":
            continue
        if not data.get("review") or data.get("project") not in projects:
            continue
        label = "review " + str(data["review"]) + " (" + str(data.get("activation") or path.stem) + "): "
        pin = data.get("codex_pin") or {}
        if pin.get("checked") and not pin.get("probed"):
            lines.append(label + "unprobed, " + str(pin.get("version")))
        sign_in = data.get("codex_sign_in")
        if isinstance(sign_in, str) and sign_in:
            lines.append(label + " ".join(sign_in.split()))
    return lines

