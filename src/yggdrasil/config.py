"""Layered configuration (Amendment A3): core, preset, user config, profile, programme, PID, package, env."""

from __future__ import annotations

import difflib
import json
import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

from . import util

DATA = Path(__file__).parent / "data"
CORE = DATA / "yggdrasil.yaml"
PRESETS = DATA / "presets"

GLOBAL, PROJECT = "global", "project"
# Every configurable key and its scope. Project keys may also be set at programme, PID and package level.
SCHEMA = {
    "schema": GLOBAL, "preset": GLOBAL, "tracker": GLOBAL, "launcher": GLOBAL, "launcher_cli": GLOBAL,
    "vocabulary": GLOBAL, "alert_forwarder": GLOBAL, "state_root": GLOBAL, "files_root": GLOBAL,
    "plugin_paths": GLOBAL, "t3": GLOBAL, "t3_bin": GLOBAL, "t3_supported_cli_versions": GLOBAL,
    "t3_supported_server_versions": GLOBAL, "linear_team_key": GLOBAL, "linear_token_store": GLOBAL,
    "linear_token_file": GLOBAL, "linear_keychain_service": GLOBAL, "linear_authority_actor_ids": GLOBAL,
    "linear_api_url": GLOBAL, "linear_deadline_seconds": GLOBAL, "stall_threshold_minutes": GLOBAL, "max_silence_minutes": GLOBAL,
    "retry_limit": GLOBAL, "settle_delay_seconds": GLOBAL, "load_threshold": GLOBAL,
    "load_max_age_seconds": GLOBAL, "launch_confirm_seconds": GLOBAL, "startup_grace_seconds": GLOBAL, "capacity_backoff_seconds": GLOBAL, "stop_grace_seconds": GLOBAL,
    "rehydration_line_cap": GLOBAL, "l1_lock_ttl_minutes": GLOBAL, "checkpoint_batch_hours": GLOBAL,
    "tick_seconds": GLOBAL, "tick_budget_seconds": GLOBAL, "tick_cap": GLOBAL, "max_launches_per_tick": GLOBAL, "authority": GLOBAL, "delivery_approach_default": GLOBAL,
    "agile_cadence_default": GLOBAL,
    # profile keys
    "name": GLOBAL, "hub_base_url": GLOBAL, "pools": GLOBAL, "launch_env": GLOBAL, "digest_dir": GLOBAL,
    "digest_filename": GLOBAL, "timezone": GLOBAL, "platform_pause_dir": GLOBAL, "commit_identity": GLOBAL,
    "leak_denylist": GLOBAL, "hub_state_file": GLOBAL, "agent_claude_config_dir": GLOBAL,
    "max_concurrent_sessions": GLOBAL, "agent_permissions": GLOBAL, "agent_permissions_version": GLOBAL,
    "agent_commands": GLOBAL, "agent_project_settings": GLOBAL,
    # project-scoped keys
    "models": PROJECT, "codex_models": GLOBAL, "review_rounds": PROJECT, "delivery_approach": PROJECT, "agile_cadence": PROJECT,
    "cadence": PROJECT, "wip_limit": PROJECT, "timebox_hours": PROJECT, "merge_mode": PROJECT,
    "branch_protection": PROJECT, "work_ahead": PROJECT,
    # A11 keys (AC-35, the lane-0 seam). Each lane reads these; only lane 0 adds keys here.
    "stage_close": PROJECT, "agent_env_passthrough": GLOBAL, "command_retry_limit": GLOBAL,
    "busy_max_minutes": GLOBAL, "needs_l1_minutes": GLOBAL, "merge_check_timeout_minutes": GLOBAL,
    "merge_check_path": GLOBAL, "merge_check_setup": PROJECT, "alert_t3_thread": GLOBAL, "alert_command": GLOBAL,
    "alert_send_timeout_seconds": GLOBAL, "alert_max_attempts": GLOBAL, "alert_tick_budget_seconds": GLOBAL,
    "dashboard_allowed_hosts": GLOBAL, "alert_macos": GLOBAL,
}
# Keys inside a mapping key that `ygg config explain` answers for by their dotted name (AC-35).
SUBKEYS = {"models.review_model"}
# Signed defaults whose behaviour isn't built yet: `ygg config explain` shows the default, while `config.get`
# keeps today's value until the default moves into data/yggdrasil.yaml. agent_claude_config_dir moved there
# (lane 3, AC-75). agent_commands stays shown here: its rules come from the approved PID's header only
# (permissions.commands_for, AC-74), never from a config layer.
DEFERRED_DEFAULTS = {"agent_commands": []}
# Project keys honoured only from the approved PID snapshot (AC-35, AC-98): never from a programme or package.
APPROVED_PID_KEYS = ("agent_commands", "merge_check_setup")
# Positive whole-number keys (AC-35).
POSITIVE_WHOLE_KEYS = ("command_retry_limit", "busy_max_minutes", "needs_l1_minutes", "merge_check_timeout_minutes",
                       "alert_send_timeout_seconds", "alert_max_attempts", "alert_tick_budget_seconds")
STAGE_CLOSE_VALUES = ("default", "auto", "pm-only")
# Project keys that only the PID layer may set (Amendment A4: the approved snapshot or the user verb).
PID_ONLY = {"work_ahead"}
ENV_KEYS = {"tracker": "YGG_TRACKER", "state_root": "YGG_STATE_ROOT", "files_root": "YGG_FILES_ROOT",
            "plugin_paths": "YGG_PLUGIN_PATH"}
PATH_KEYS = ("state_root", "files_root", "digest_dir", "platform_pause_dir", "agent_claude_config_dir")
LAYER_WORDS = {"programme": "in a programme", "pid": "in a PID", "package": "in a work package"}
# Security-relevant keys: honoured only from trusted layers (core, preset, the user's own config,
# profile, environment); for the PID only the user-approved snapshot counts.
SECURITY_KEYS = {"authority", "state_root", "files_root", "plugin_paths", "branch_protection", "merge_mode",
                 "agent_permissions", "agent_commands", "agent_project_settings", "merge_check_setup"}
PLUGIN_KEYS = {"tracker": "trackers", "launcher": "launchers", "alert_forwarder": "alert_forwarders"}
# The user's agreements: honoured only from the agreement layer their user-only verb writes, never from any
# other layer (a config file, a profile, a programme, a PID or a package).
AGREEMENT_KEYS = ("agent_permissions", "agent_permissions_version")
# Super-user settings that widen what agents may do: only from the user's own layers (core, preset, a config
# named by YGG_CONFIG or the home config, a profile), never a current-folder config, programme, PID or package.
USER_ONLY_KEYS = ("agent_commands", "agent_project_settings")
# A11 trusted-only keys (AC-35, AC-98 row 3): a current-folder config that sets one is refused, never ignored.
# Every key that names a program to run, a search path for programs, or a folder whose settings an agent's tool
# loads and acts on (t3_bin, alert_command, merge_check_path, agent_claude_config_dir) is one of them: a
# repository's own config must never choose what Yggdrasil or its agents execute. Nor where the Linear
# credentials are read or written: linear_token_store, linear_token_file and linear_keychain_service are
# trusted-only too.
TRUSTED_ONLY_KEYS = ("agent_env_passthrough", "alert_command", "merge_check_path", "agent_project_settings",
                     "alert_macos", "t3_bin", "agent_claude_config_dir", "linear_token_store", "linear_token_file",
                     "linear_keychain_service")
AGREEMENT_FILE = "agreements.yaml"


def _load(path) -> dict:
    try:
        data = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


NO_CONFIG = "none"


def user_config_trusted() -> bool:
    """Whether the user config was named explicitly (YGG_CONFIG) or is the home config. A config found in the
    current folder may be one an agent wrote, so it is not trusted to load code."""
    explicit = util.env("YGG_CONFIG")
    if explicit and explicit != NO_CONFIG:
        return True
    path = user_config_path()
    return path is None or path != (Path.cwd() / "yggdrasil.yaml").resolve()


TRUSTED_ONLY = "may be set only in a config named by YGG_CONFIG, the home config, a profile or the environment"


def untrusted_code_settings() -> list[str]:
    """Problems for settings in an untrusted (current-folder) config that would make ygg import code."""
    if user_config_trusted():
        return []
    path = user_config_path()
    raw = _load(path) if path else {}
    problems = []
    if raw.get("plugin_paths"):
        problems.append(str(path) + ": plugin_paths " + TRUSTED_ONLY)
    for key in TRUSTED_ONLY_KEYS:
        if key in raw:
            problems.append(str(path) + ": " + key + " " + TRUSTED_ONLY)
    from .plugins import BUILTINS
    for key, group in PLUGIN_KEYS.items():
        value = raw.get(key)
        if value is not None and str(value) not in BUILTINS[group]:
            problems.append(str(path) + ": " + key + ": " + str(_shown(value, key)) + " " + TRUSTED_ONLY)
    return problems


def user_config_path() -> Path | None:
    explicit = util.env("YGG_CONFIG")
    if explicit == NO_CONFIG:
        return None
    if explicit:
        return Path(explicit).resolve()
    if util.env("YGG_NODE") is not None or util.env("YGG_ACTIVATION") is not None:
        return None
    local = Path.cwd() / "yggdrasil.yaml"
    # The working-directory lookup is for the user's own shell only; managed sessions run in
    # agent-writable worktrees and get YGG_CONFIG from the conductor instead.
    if local.is_file():
        return local.resolve()
    home = Path.home() / ".config" / "yggdrasil" / "config.yaml"
    return home if home.is_file() else None


def _resolve_paths(data: dict, base: Path) -> dict:
    data = dict(data)
    for key in PATH_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value and not os.path.isabs(os.path.expanduser(value)):
            data[key] = str((base / value).resolve())
        elif isinstance(value, str):
            data[key] = os.path.expanduser(value)
    if isinstance(data.get("plugin_paths"), list):
        data["plugin_paths"] = [str((base / p).resolve()) if not os.path.isabs(str(p)) else str(p)
                                for p in data["plugin_paths"]]
    return data


@lru_cache(maxsize=1)
def profile() -> dict | None:
    path = util.env("YGG_PROFILE")
    if not path:
        return None
    return _load(path)


@lru_cache(maxsize=1)
def file_layers() -> tuple:
    """(label, values) pairs for the global file layers, lowest precedence first."""
    layers = [("core", _load(CORE))]
    user_path = user_config_path()
    user = _resolve_paths(_load(user_path), user_path.parent) if user_path else {}
    if user_path and not user_config_trusted():
        # A current-folder config never contributes code: no plugin search paths, no plugin selection, and no
        # trusted-only key (validate refuses such a config; this keeps the value out for commands that skip it).
        from .plugins import BUILTINS
        user.pop("plugin_paths", None)
        for key in USER_ONLY_KEYS + TRUSTED_ONLY_KEYS:
            user.pop(key, None)
        for key, group in PLUGIN_KEYS.items():
            if key in user and str(user[key]) not in BUILTINS[group]:
                user.pop(key)
    profile_path = util.env("YGG_PROFILE")
    prof = _resolve_paths(profile() or {}, Path(profile_path).parent) if profile_path else {}
    preset = prof.get("preset") or user.get("preset") or "starter"
    preset_values = _load(PRESETS / (str(preset) + ".yaml")) if _safe_name(preset) else {}
    layers.append(("preset:" + str(preset), preset_values))
    if user_path:
        layers.append(("config:" + str(user_path), user))
    if profile_path:
        layers.append(("profile:" + str(profile_path), prof))
    layers = [(label, {k: v for k, v in values.items() if k not in AGREEMENT_KEYS}) for label, values in layers]
    agreed = agreement_path()
    recorded = agreement_record(_state_root_of(layers))
    if recorded:
        layers.append(("agreement:" + str(agreed), recorded))
    return tuple(layers)


def agreement_record(state_root=None) -> dict:
    """The user's agreement as the file holds it now, read on every call (a running conductor sees an
    agreement or a revocation made by another process at its next launch). Nothing counts from a symbolic
    link, or from a file whose real location is in an agent's working folder."""
    agreed = agreement_path()
    if agreed is None or os.path.islink(agreed):
        return {}
    if managed_folder(os.path.dirname(os.path.realpath(agreed)), state_root):
        return {}
    return {k: v for k, v in _load(agreed).items() if k in AGREEMENT_KEYS}


def agreement_path() -> Path | None:
    """The user-only layer that holds the user's agreements, written only by `ygg agents allow-minimal` and
    `revoke-minimal`: next to the config YGG_CONFIG names, else ~/.config/yggdrasil/agreements.yaml. None
    when the user config is one found in the current folder (an agent may have written it): then nothing is
    recorded or read."""
    if not user_config_trusted():
        return None
    explicit = util.env("YGG_CONFIG")
    if explicit and explicit != NO_CONFIG:
        return Path(explicit).resolve().parent / AGREEMENT_FILE
    return Path.home() / ".config" / "yggdrasil" / AGREEMENT_FILE


# Folders agents work in: worktrees and staging folders inside a programme's home, and the scratch root.
MANAGED_FOLDERS = (".ygg-worktrees", ".ygg-staging")


def _state_root_of(layers) -> str:
    """The state root as util.state_root() resolves it, from layers already read (no config lookup)."""
    configured = None
    for _, values in layers:
        if isinstance(values.get("state_root"), str) and values["state_root"]:
            configured = values["state_root"]
    return util.env("YGG_STATE_ROOT") or configured or str(Path.home() / ".local" / "state" / "yggdrasil")


def managed_folder(path, state_root=None) -> bool:
    """Whether `path` lies in a folder an agent works in (and may write): a worktree, a staging folder or the
    scratch root of the state root."""
    real = os.path.realpath(str(path))
    if any(part in MANAGED_FOLDERS for part in Path(real).parts):
        return True
    scratch = os.path.realpath(os.path.join(str(state_root if state_root is not None else util.state_root()),
                                            "scratch"))
    return real == scratch or real.startswith(scratch + os.sep)


def misplaced_agreements() -> list[str]:
    """An agreement written by hand into a config file or profile instead of recorded by its verb."""
    sources = [user_config_path(), util.env("YGG_PROFILE")]
    return [str(path) + ": " + key + " is recorded only by `ygg agents allow-minimal` (and withdrawn by `ygg "
            "agents revoke-minimal`), never set in a config file; remove it from this file"
            for path in sources if path for key in AGREEMENT_KEYS if key in _load(path)]


def _safe_name(value) -> bool:
    return isinstance(value, str) and util.ID_RE.match(value) is not None


def env_layer() -> dict:
    values = {}
    for key, name in ENV_KEYS.items():
        value = util.env(name)
        if value:
            values[key] = value.split(os.pathsep) if key == "plugin_paths" else value
    return values


def project_layers(project: str | None, package: str | None = None) -> list:
    if not project:
        return []
    from . import tracker
    layers = []
    data = tracker.project(project)
    if not data:
        return layers
    programme = tracker.programme(data.get("programme")) or {}
    values = {key: programme[key] for key in APPROVED_PID_KEYS if key in programme}
    if isinstance(programme.get("config"), dict):
        values.update(programme["config"])
    if values:
        # Approved-PID-only keys written on the programme record itself are shown, and ignored (AC-98 row 1).
        layers.append(("programme:" + programme["id"], values))
    text = tracker.read_document(project, "pid")
    if text:
        header, _ = util.split_frontmatter(text)
        layers.append(("pid:" + project, {k: v for k, v in (header or {}).items() if SCHEMA.get(k) == PROJECT}))
    if package:
        util.check_record_id(package)
        issue = tracker.read_issue(project, package)
        if issue is not None and isinstance(issue.get("config"), dict):
            layers.append(("package:" + project + "/" + package, dict(issue.get("config"))))
    return layers


IGNORED: dict = {}


def _approved_value(project: str, key: str):
    from .commands import approved_pid
    approved = approved_pid(project) or {}
    guarded = approved.get("guarded") or {}
    return (key in guarded and guarded[key] is not None), guarded.get(key)


def explain_all(project: str | None = None, package: str | None = None) -> dict:
    """key -> (value, source) with every layer applied in order; untrusted security values are ignored."""
    result = {}
    IGNORED.clear()
    for label, values in list(file_layers()) + project_layers(project, package):
        kind = label.split(":", 1)[0]
        for key, value in values.items():
            if key in AGREEMENT_KEYS and kind != "agreement" or \
                    key in USER_ONLY_KEYS and kind in ("programme", "pid", "package"):
                IGNORED.setdefault(key, []).append(label)
                continue
            if key in SECURITY_KEYS and kind in ("programme", "package"):
                IGNORED.setdefault(key, []).append(label)
                continue
            if key in SECURITY_KEYS and kind == "pid":
                present, approved = _approved_value(label.split(":", 1)[1], key)
                if not present or approved != value:
                    IGNORED.setdefault(key, []).append(label)
                    continue
            result[key] = (value, label)
    for key, value in env_layer().items():
        result[key] = (value, "env:" + ENV_KEYS[key])
    if "merge_check_path" not in result:
        # The conductor's PATH (AC-35): the PATH this process runs with.
        result["merge_check_path"] = (os.environ.get("PATH", os.defpath), "core")
    for key in PID_ONLY:
        result.pop(key, None)
    if project:
        from . import tracker
        if tracker.project(project):
            from . import workahead
            result["work_ahead"] = workahead.setting(project)
    else:
        result["work_ahead"] = ("off", "core")
    return result


@lru_cache(maxsize=1)
def effective() -> dict:
    return {key: value for key, (value, _) in explain_all().items()}


def get(key: str, default=None):
    return effective().get(key, default)


def project_get(key: str, project: str | None, default=None):
    """The effective value of a project-scoped key for one project, every layer applied (A3)."""
    value, _ = explain_all(project).get(key, (default, "core"))
    return default if value is None else value


# Keys whose names look secret but whose values are a store choice and a file path, shown in full (token-shaped
# strings in them are still masked).
_NAMED_LIKE_SECRETS = ("linear_token_store", "linear_token_file")


def _shown(value, key: str):
    return util.redact_profile(value, "" if key in _NAMED_LIKE_SECRETS else key)


def explain(key: str | None, project: str | None, package: str | None, as_json: bool) -> str:
    import json
    values = explain_all(project, package)
    for name, default in DEFERRED_DEFAULTS.items():
        values.setdefault(name, (default, "core"))
    if key is not None:
        if key not in SCHEMA and key not in SUBKEYS:
            raise util.usage(unknown_key_message(key))
        value, source = values.get(key, (None, "core")) if key in SCHEMA else _subkey(key, project, package)
        # Shown values go through the profile redaction: a value under a secret-looking key (a launch_env
        # API key, a token) is hidden whole, and token-shaped strings are masked, in the source label too.
        value = _shown(value, key)
        item = {"key": key, "value": value, "source": source}
        if IGNORED.get(key):
            item["ignored"] = IGNORED[key]
        if as_json:
            return util.redact(json.dumps(item) + "\n")
        line = key + " = " + json.dumps(value) + "  (" + source + ")"
        if IGNORED.get(key):
            line += "\n  ignored: " + ", ".join(IGNORED[key])
        return util.redact(("# " + COMMENTS[key] + "\n" if key in COMMENTS else "") + line + "\n")
    items = [dict({"key": k, "value": _shown(v, k), "source": s},
                  **({"ignored": IGNORED[k]} if IGNORED.get(k) else {}))
             for k, (v, s) in sorted(values.items())]
    if as_json:
        return util.redact(json.dumps(items) + "\n")
    return util.redact("".join(("# " + COMMENTS[i["key"]] + "\n" if i["key"] in COMMENTS else "") +
                   i["key"] + " = " + json.dumps(i["value"]) + "  (" + i["source"] + ")\n" for i in items))


def _subkey(key: str, project: str | None, package: str | None) -> tuple:
    """(value, source) of a dotted key such as models.review_model: the last layer whose mapping sets it."""
    head, tail = key.split(".", 1)
    found = (None, "core")
    for label, values in list(file_layers()) + project_layers(project, package):
        mapping = values.get(head)
        if isinstance(mapping, dict) and tail in mapping:
            found = (mapping[tail], label)
    return found


def unknown_key_message(key: str) -> str:
    message = "unknown key '" + key + "'"
    close = difflib.get_close_matches(key, list(SCHEMA), n=1, cutoff=0.75)
    if close:
        message += "; did you mean '" + close[0] + "'?"
    return message


def validate(project: str | None = None, *, check_plugins: bool = True, scan_tracker: bool = True) -> None:
    """Refuse unknown keys, keys at a forbidden layer, unknown plugins and unsupported plugin versions.

    `scan_tracker=False` leaves out the scan of tracker records for PID-only keys: the safety commands
    (stop, pause, resume) must never depend on reading the tracker (I7). The agreement check reads only the
    user's config and profile files, never the tracker."""
    problems = untrusted_code_settings() + misplaced_agreements()
    if problems:
        raise util.usage(util.redact("\n".join(problems)))
    for label, values in file_layers()[1:]:
        for key in values:
            if key not in SCHEMA:
                problems.append(label + ": " + unknown_key_message(key))
            elif key in PID_ONLY:
                problems.append(key + " can be set only in a PID")
    for label, values in _scoped_layers(project):
        kind = label.split(":", 1)[0]
        for key in values:
            if key not in SCHEMA:
                problems.append(label + ": " + unknown_key_message(key))
            elif SCHEMA[key] == GLOBAL:
                problems.append(key + " cannot be set " + LAYER_WORDS[kind])
            elif key in PID_ONLY and kind != "pid":
                problems.append(key + " can be set only in a PID")
    if project is None and scan_tracker:
        problems += _pid_only_everywhere()
    if not problems:
        problems += value_problems(effective_unvalidated())
    if not problems and check_plugins:
        from . import plugins
        for key, group in PLUGIN_KEYS.items():
            value = get(key)
            if value is None:
                continue
            # A wrong value is quoted in the message: shown through the same redaction as explain.
            problem = plugins.check(group, str(_shown(value, key)))
            if problem:
                problems.append(problem)
    if problems:
        raise util.usage(util.redact("\n".join(problems)))


COMMENTS = {
    "tick_cap": "How one conductor tick limits its launches: both (whichever limit comes first), count or time.",
    "max_launches_per_tick": "Most sessions one tick starts (used by count and both).",
    "tick_budget_seconds": "Seconds one tick may spend starting sessions (used by time and both).",
}


def effective_unvalidated() -> dict:
    return {key: value for key, (value, _) in explain_all().items()}


def _positive_number(value, integer: bool) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if integer and not isinstance(value, int):
        return False
    return value > 0


PROTECTION_MODES = ("refuse", "warn", "off")


def protection_problem(value) -> str | None:
    """Why a branch_protection value is unusable, or None. An unquoted YAML off, no, on or yes arrives as a
    boolean, which never silently means a mode: it is refused with the fix."""
    if value is None or value in PROTECTION_MODES:
        return None
    if isinstance(value, bool):
        return ("branch_protection is the YAML boolean " + str(value).lower() + " (an unquoted off, no, on or "
                "yes); quote the mode: \"refuse\", \"warn\" or \"off\"")
    return "branch_protection must be refuse, warn or off, not '" + str(value) + "'"


# One plain command with plain arguments: no shell operators, quotes, expansions, wildcards or parentheses, so
# its permission rule means exactly that command.
PLAIN_COMMAND = re.compile(r"^[A-Za-z0-9_./:=@%+,-]+( [A-Za-z0-9_./:=@%+,-]+)*$")


def _agent_command_problems(named) -> list[str]:
    if named is None:
        return []
    if not isinstance(named, dict):
        return ["agent_commands must map each programme to its list of test and build commands"]
    problems = []
    for programme, commands in named.items():
        if not util.ID_RE.match(str(programme)):
            problems.append("agent_commands: '" + str(programme) + "' is not a programme id")
        if not isinstance(commands, list) or not commands:
            problems.append("agent_commands: " + str(programme) + " must list one or more commands")
            continue
        for command in commands:
            if not isinstance(command, str) or not PLAIN_COMMAND.match(command):
                problems.append("agent_commands: " + str(programme) + ": " + json.dumps(command) + " is not one "
                                "plain command (no shell operators, quotes, $, `, *, parentheses or line breaks)")
    return problems


def value_problems(values: dict) -> list[str]:
    # The messages quote values, so the checks run on a redacted copy: a secret pasted into the wrong key is never
    # echoed back (real values hold no secret-named keys or token-shaped strings, so the checks are unchanged).
    values = {k: _shown(v, k) for k, v in values.items()}
    problems = []
    protection = protection_problem(values.get("branch_protection"))
    if protection:
        problems.append(protection)
    concurrent = values.get("max_concurrent_sessions")
    if concurrent is not None and not _positive_number(concurrent, integer=True):
        problems.append("max_concurrent_sessions must be a positive whole number (or unset for no limit), not '" +
                        str(concurrent) + "'")
    agreement = values.get("agent_permissions")
    if agreement is not None and agreement not in ("minimal", "standard", "not-granted"):
        problems.append("agent_permissions must be minimal, standard or not-granted, not '" + str(agreement) + "'")
    version = values.get("agent_permissions_version")
    if version is not None and not _positive_number(version, integer=True):
        problems.append("agent_permissions_version must be a positive whole number, not '" + str(version) + "'")
    problems += _agent_command_problems(values.get("agent_commands"))
    project_settings = values.get("agent_project_settings")
    if project_settings is not None and project_settings not in ("exclude", "include"):
        problems.append("agent_project_settings must be exclude or include, not '" + str(project_settings) + "'")
    identity = values.get("commit_identity")
    if isinstance(identity, dict) and identity.get("trailers") not in (None, "none", "allow"):
        problems.append("commit_identity.trailers must be none or allow, not '" + str(identity.get("trailers")) + "'")
    problems += a11_value_problems(values)
    cap = values.get("tick_cap", "both")
    if cap not in ("both", "count", "time"):
        problems.append("tick_cap must be both, count or time, not '" + str(cap) + "'")
    if not _positive_number(values.get("max_launches_per_tick", 5), integer=True):
        problems.append("max_launches_per_tick must be a positive whole number, not '" +
                        str(values.get("max_launches_per_tick")) + "'")
    if not _positive_number(values.get("tick_budget_seconds", 240), integer=False):
        problems.append("tick_budget_seconds must be a positive number of seconds, not '" +
                        str(values.get("tick_budget_seconds")) + "'")
    if values.get("tracker") == "linear" and not _positive_number(values.get("settle_delay_seconds", 30),
                                                                   integer=False):
        # Without a settle delay a busy hour under Linear could use most of the app's request window.
        problems.append("settle_delay_seconds must be a positive number of seconds with tracker: linear, not '" +
                        str(values.get("settle_delay_seconds")) + "'")
    return problems


PASSTHROUGH_DENIED = re.compile(r"^(PATH|GIT_.*|YGG_.*)$")


def _argv_list(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item for item in value)


def a11_value_problems(values: dict) -> list[str]:
    """Validation of the A11 keys (AC-35). The texts are the signed ones."""
    problems = []
    close = values.get("stage_close")
    if close is not None and close not in STAGE_CLOSE_VALUES:
        problems.append("stage_close must be default, auto or pm-only, not '" + str(close) + "'")
    elif close not in (None, "default"):
        from . import release
        if not release.on("stage_close"):
            problems.append("stage_close: " + str(close) + " is not in this release")
    for key in POSITIVE_WHOLE_KEYS:
        value = values.get(key)
        if value is not None and not _positive_number(value, integer=True):
            problems.append(key + " must be a positive whole number, not '" + str(value) + "'")
    passthrough = values.get("agent_env_passthrough")
    if passthrough is not None:
        if not isinstance(passthrough, list) or not all(isinstance(n, str) and n for n in passthrough):
            problems.append("agent_env_passthrough must be a list of variable names")
        # PATH, GIT_ and YGG_ names are refused by the launch itself (AC-82 row 3, exit 3).
    for key in ("alert_command", "merge_check_setup"):
        value = values.get(key)
        if value is not None and not _argv_list(value):
            problems.append(key + " must be an argv list (a list of strings)")
    hosts = values.get("dashboard_allowed_hosts")
    if hosts is not None and not (isinstance(hosts, list) and all(isinstance(h, str) and h for h in hosts)):
        problems.append("dashboard_allowed_hosts must be a list of host names")
    macos = values.get("alert_macos")  # A11.1 AC-10; unquoted YAML on/off reads as true/false
    if macos is not None and macos not in (True, False, "on", "off"):
        problems.append("alert_macos must be on or off, not '" + str(macos) + "'")
    for key in ("merge_check_path", "alert_t3_thread"):
        value = values.get(key)
        if value is not None and not (isinstance(value, str) and value):
            problems.append(key + " must be a text value")
    return problems


def _pid_only_everywhere() -> list[str]:
    """PID-only keys placed in any programme or work-package config (validation without --project, and the
    validation every command runs first). A tracker that cannot be read is reported by the command itself."""
    from . import tracker
    problems = []
    try:
        for programme in tracker.programmes():
            if isinstance(programme.get("config"), dict):
                problems += [key + " can be set only in a PID" for key in programme["config"] if key in PID_ONLY]
        for project in tracker.projects():
            for issue in tracker.issues(project["id"]):
                if isinstance(issue.get("config"), dict):
                    problems += [key + " can be set only in a PID" for key in issue.get("config") if key in PID_ONLY]
    except Exception:  # noqa: BLE001  (an unreadable tracker is the command's own concern, never this check's)
        return []
    return sorted(set(problems))


def _scoped_layers(project: str | None) -> list:
    """Programme config, raw PID frontmatter config keys and package config for validation."""
    if not project:
        return []
    from . import tracker
    util.check_id(project, "project id")
    data = tracker.project(project)
    if not data:
        raise util.usage("unknown project '" + project + "'")
    layers = []
    programme = tracker.programme(data.get("programme")) or {}
    if isinstance(programme.get("config"), dict):
        layers.append(("programme:" + programme["id"], programme["config"]))
    text = tracker.read_document(project, "pid")
    if text:
        header, _ = util.split_frontmatter(text)
        layers.append(("pid:" + project, {k: v for k, v in (header or {}).items() if k in SCHEMA
                                          and k not in ("schema",)}))
    for issue in tracker.issues(project):
        if isinstance(issue.get("config"), dict):
            layers.append(("package:" + project + "/" + issue.id, issue.get("config")))
    return layers
