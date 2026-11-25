"""`ygg init`: guided plain-language setup that writes a commented config (Amendment A3)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

from . import config, state, util
from .permissions import ALLOW, ALLOW_STANDARD

QUESTIONS = {
    "use": ("What is the work mostly?", ["software", "writing", "business", "mix"]),
    "ai_tool": ("Which AI tool do you have?", ["claude-code", "codex", "both", "t3"]),
    "tracker": ("Where should progress be tracked?", ["files", "linear"]),
    "autonomy": ("How much may agents do without asking?", ["ask-each-phase", "overnight-within-limits"]),
    "vocabulary": ("Which words should Yggdrasil use?", ["plain", "prince2"]),
}
COMMENTS = {
    "schema": "Yggdrasil configuration, written by ygg init.",
    "preset": "Starting set of defaults. Change it, or override single keys below.",
    "tracker": "Where progress is tracked: files on this computer, or linear.",
    "files_root": "Folder for the tracker files.",
    "state_root": "Folder for local working state.",
    "launcher": "How agents are started: headless runs your own command-line AI tool; t3 uses T3 Code.",
    "launcher_cli": "Which command-line AI tool to start.",
    "vocabulary": "Words used when Yggdrasil talks to you: plain or prince2.",
}
PRESETS = ("starter", "solo-builder", "team")


def _detected_cli() -> str:
    has_claude = shutil.which("claude") is not None
    has_codex = shutil.which("codex") is not None
    return "codex" if has_codex and not has_claude else "claude-code"


def _ask(field: str) -> str | None:
    question, options = QUESTIONS[field]
    sys.stdout.write(question + " (" + " / ".join(options) + ", empty for the default): ")
    sys.stdout.flush()
    answer = sys.stdin.readline().strip()
    return answer or None


def ignore_runtime(folder: Path, track_records: bool) -> None:
    """Keep the runtime folders next to the config out of git: `ygg-state/` and its safety folder
    `ygg-state.safety/` (generated hooks with absolute paths) always, `ygg-tracker/` unless the user chose to version
    the records. Existing lines stay; nothing is added twice."""
    wanted = ["ygg-state/", "ygg-state.safety/"] + ([] if track_records else ["ygg-tracker/"])
    path = folder / ".gitignore"
    text = path.read_text() if path.exists() else ""
    present = {line.strip() for line in text.splitlines()}
    missing = [line for line in wanted if line not in present]
    if missing:
        util.atomic_write(path, text + ("" if not text or text.endswith("\n") else "\n") + "\n".join(missing) + "\n")


def run(preset: str | None, non_interactive: bool, answers_file: str | None, out: str | None, force: bool,
        track_records: bool = False) -> str:
    if state.managed_session():
        raise util.refuse("user-only: 'init' cannot run in a managed session")
    if preset is not None and preset not in PRESETS:
        raise util.usage("unknown preset '" + preset + "'; available: " + ", ".join(PRESETS))
    answers = {}
    if answers_file:
        path = Path(answers_file)
        try:
            loaded = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as error:
            raise util.usage("cannot read answers file " + str(answers_file) + ": " + str(error))
        if not isinstance(loaded, dict):
            raise util.usage("answers file must be a mapping")
        for field, value in loaded.items():
            if field == "schema":
                continue
            if field not in QUESTIONS:
                raise util.usage("unknown answer field '" + str(field) + "'")
            if value not in QUESTIONS[field][1]:
                raise util.usage("invalid answer for '" + field + "': " + str(value))
            answers[field] = value
    interactive = not non_interactive and sys.stdin.isatty()
    if not non_interactive and not interactive and not answers_file:
        sys.stderr.write("no terminal: the five questions were skipped and their defaults used\n")
    if interactive:
        for field in QUESTIONS:
            if field not in answers:
                value = _ask(field)
                if value is not None:
                    if value not in QUESTIONS[field][1]:
                        raise util.usage("invalid answer for '" + field + "': " + value)
                    answers[field] = value
    if preset is None:
        preset = "solo-builder" if answers.get("use") == "software" and \
            answers.get("autonomy") == "overnight-within-limits" else "starter"
    defaults = config._load(config.PRESETS / (preset + ".yaml"))
    values = {"schema": "ygg.config.v1", "preset": preset,
              "tracker": answers.get("tracker", defaults.get("tracker", "files")),
              "files_root": "./ygg-tracker", "state_root": "./ygg-state"}
    tool = answers.get("ai_tool")
    if tool == "t3":
        values["launcher"] = "t3"
        values["launcher_cli"] = defaults.get("launcher_cli", "claude-code")
    elif tool == "codex":
        values["launcher"], values["launcher_cli"] = "headless", "codex"
    elif tool in ("claude-code", "both"):
        values["launcher"], values["launcher_cli"] = "headless", "claude-code"
    else:
        values["launcher"] = defaults.get("launcher", "headless")
        values["launcher_cli"] = _detected_cli() if not answers_file else defaults.get("launcher_cli", "claude-code")
    values["vocabulary"] = answers.get("vocabulary", defaults.get("vocabulary", "plain"))
    target = Path(out) if out else Path.cwd() / "yggdrasil.yaml"
    if target.exists() and not force:
        raise util.usage(str(target) + " already exists; use --force to overwrite it")
    lines = []
    for key, value in values.items():
        lines.append("# " + COMMENTS[key])
        lines.append(yaml.safe_dump({key: value}, default_flow_style=False).strip())
    util.atomic_write(target, "\n".join(lines) + "\n")
    ignore_runtime(target.parent, track_records)
    message = ["Wrote " + str(target) + " (preset " + preset + ").", "Next steps:",
               "  ygg config validate",
               "  ygg programme create <name> --domain <domain> --home <folder> --kind vault-docs",
               "  ygg project create <project> --programme <name> --pm pm1"]
    message.append("Agents that run with their own Claude settings folder (agent_claude_config_dir) can do nothing "
                   "until you agree to their permissions: run `" + ALLOW_STANDARD + "` to see the standard rules, "
                   "then add --yes if you agree, or run `" + ALLOW + "` for the strict minimum (see "
                   "docs/security.md).")
    if values["tracker"] == "linear":
        message.append("Linear: connect the Yggdrasil app to a private team in Linear; the token is stored "
                       "in your keychain or a 0600 file, never in this config.")
    return "\n".join(message) + "\n"
