"""What a Claude Code agent may do without asking: the permission rules in its launch settings file.

The user agrees once, with a user-only verb that records the agreement, and the version of the rule set agreed,
in the user-only agreement layer (`config.agreement_path()`):
- `ygg agents allow-standard` (ruling 129) shows the exact rules, and with `--yes` agrees to them;
- `ygg agents allow-minimal` agrees to the strict minimum;
- `ygg agents revoke` withdraws either (`revoke-minimal` is the older name).
No config file, profile, programme, PID or package can carry the agreement. Until then no rule is written, and a
launch that would need them, one whose agent runs with its own Claude settings folder (`agent_claude_config_dir`,
where nothing else allows it anything), is refused with the command to run. A rule set whose version is newer
than the one agreed (it allows more) is refused the same way until the user agrees again.

The rules are the narrowest Claude Code accepts (checked against Claude Code 2.1.322 and its settings schema):
- `defaultMode: dontAsk`: whatever no rule allows is denied at once, never left on a prompt nobody answers.
- `Bash(ygg *)`: the node's own ygg commands; ygg itself refuses user-only verbs in a managed session. A rule
  never matches a compound command (`ygg ... && other`).
- `Read(//<project folder>/**)`: its project's tracker records and documents (rehydration document, brief).
  Reads inside its own working folder need no rule.
- Editing roles only: `Edit(//<worktree>/**)` (it covers Write) and, in a Git worktree, `Bash(git add *)` and
  `Bash(git commit *)`. Senior users and reviewers edit nothing.
The standard profile adds, in a Git worktree, read-only git for every role, which the guard hook holds to a few
exact shapes with no paths (hooks.READ_ONLY_MESSAGE), with `--output` and `--no-index` denied because they write
or read files elsewhere; the programme's named
commands (`agent_commands`, the user's own config only) for team leads, workers and senior users; and for a team
lead, `git push` of its own lane branch, exactly, which the push guard still checks.
Paths are written absolute (`//`): a `/path` rule in a `--settings` file is anchored at that file's own folder.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import config, util

MINIMAL, STANDARD, NOT_GRANTED = "minimal", "standard", "not-granted"
# The version of each rule set. Raise one whenever its rules allow more: an older agreement then no longer counts.
VERSIONS = {MINIMAL: 1, STANDARD: 1}
READ_ONLY_ROLES = ("su", "reviewer")
COMMAND_ROLES = ("tl", "worker", "su")
# Read-only git commands can still write a file (--output) or read one outside the repository (--no-index).
DENY = ["Bash(git *--output*)", "Bash(git *--no-index*)"]
# Claude Code reads rule paths as gitignore patterns; a path holding one of these would not mean itself.
PATTERN_CHARACTERS = set("*?[]\\!()\n\r")

ALLOW_STANDARD = "ygg agents allow-standard"
ALLOW = "ygg agents allow-minimal"
REVOKE = "ygg agents revoke"
WHAT = ("each Claude Code agent may read its project's tracker records, run its own ygg commands, and edit and "
        "commit files only in its own worktree; everything else is denied")
WHAT_STANDARD = ("as with the minimum, and also read-only git for every role, the programme's test and build "
                 "commands for team leads, workers and senior users, and a team lead's push of its own lane branch")
REFUSAL = ("launch precondition: agent permissions are not agreed. Agents that run with their own Claude "
           "settings folder (agent_claude_config_dir) can do nothing without them. Run `" + ALLOW_STANDARD +
           "` yourself, outside any agent session, to see the standard rules and agree to them (" + WHAT_STANDARD +
           "), or `" + ALLOW + "` for the strict minimum (" + WHAT + ").")


def _version_of(profile, version) -> bool:
    return profile in VERSIONS and isinstance(version, int) and not isinstance(version, bool)


AGREEMENT_SCHEMA = "ygg.agents-agreement.v1"
NEEDED_TEXT = "Yggdrasil's agent rules changed. Run ygg agents allow-standard to see them, then agree again."


def agreement_path() -> Path:
    """The user's agreement (rulings 129 and 134, AC-19): `<files root>/_ygg/agents-agreement.json`, written only by
    the user-only `ygg agents` verbs, with the profile, the rule-set version agreed, the programmes it covers and
    its origin. The files root itself, whichever tracker is chosen: the Linear tracker's local cache lives in the
    state root, and a wiped state root must not take the user's agreement with it."""
    # Inside an agent session (YGG_NODE set) the launcher pins the user's files root as YGG_AGREEMENT_ROOT: on the
    # Linear tracker the session's YGG_FILES_ROOT is the cache, which never holds the agreement. Elsewhere (the
    # user's shell, the conductor) the files root itself is used, whatever the environment says.
    pinned = util.env("YGG_AGREEMENT_ROOT") if util.env("YGG_NODE") else None
    return Path(pinned or util.files_root()) / "_ygg" / "agents-agreement.json"


def agreement() -> dict:
    """The agreement as the file holds it now, read on every call (a running conductor sees an agreement or a
    revocation made by another process at its next launch). Nothing counts through a symbolic link, from another
    origin than the user, or with a malformed programme list."""
    path = agreement_path()
    if os.path.islink(path) or os.path.islink(path.parent):
        return {}
    if config.managed_folder(os.path.dirname(os.path.realpath(path))):
        return {}  # a files root inside an agent's working folder: an agent could have written it
    try:
        data = util.read_json(path, {}) or {}
    except util.YggError:
        return {}
    if not isinstance(data, dict) or data.get("origin") != "user":
        return {}
    programmes = data.get("programmes")
    if not isinstance(programmes, list) or not all(isinstance(p, str) for p in programmes):
        return {}
    return data


def _agreement() -> tuple:
    """(profile, version) as recorded right now, never from the process's cached configuration."""
    record = agreement()
    return record.get("profile"), record.get("rules_version")


def agreed_profile(programme: str | None = None) -> str | None:
    """The agreed profile, when its recorded version covers this Yggdrasil's rules for it and, with `programme`,
    the agreement names that programme (consent is per programme, AC-34 and AC-97); else None."""
    record = agreement()
    profile, version = record.get("profile"), record.get("rules_version")
    if not (_version_of(profile, version) and version >= VERSIONS[profile]):
        return None
    if programme is not None and programme not in record.get("programmes", []):
        return None
    return profile


def granted(programme: str | None = None) -> bool:
    return agreed_profile(programme) is not None


def _stale() -> str | None:
    profile, version = _agreement()
    if not _version_of(profile, version) or version >= VERSIONS[profile]:
        return None
    return ("launch precondition: you agreed to the " + profile + " agent rules version " + str(version) +
            "; this Yggdrasil's " + profile + " rules are version " + str(VERSIONS[profile]) + " and allow more. "
            "Run `ygg agents allow-" + profile + "` yourself to see them and agree again.")


def _uncovered(programme: str | None) -> str | None:
    """The refusal for a programme the current agreement doesn't name (created after the user agreed)."""
    record = agreement()
    profile, version = record.get("profile"), record.get("rules_version")
    if programme is None or not _version_of(profile, version) or version < VERSIONS[profile]:
        return None
    if programme in record.get("programmes", []):
        return None
    return ("launch precondition: your agreement to the " + profile + " agent rules doesn't cover programme " +
            programme + ", which was created after you agreed. Run `ygg agents allow-" + profile + " --yes "
            "--programme " + programme + "` yourself to extend it.")


def _user_only(verb: str) -> None:
    from . import state
    state.check_user_only("agents " + verb)
    here = os.path.realpath(os.getcwd())
    # An agent's working folder refuses the verbs, whatever its environment says.
    if config.managed_folder(here):
        raise util.refuse("user-only: 'agents " + verb + "' is a user verb and cannot run inside a managed "
                          "worktree (" + here + ")")


def _programmes_for(profile: str, programme: str | None) -> list[str]:
    """Every programme that exists now; with `programme`, that one added to what the same, current agreement
    already covers."""
    from . import tracker
    existing = sorted(str(p["id"]) for p in tracker.programmes() if p.get("id"))
    if programme is None:
        return existing
    util.check_id(programme, "programme")
    if programme not in existing:
        raise util.refuse("ygg agents: there is no programme " + programme)
    current = agreement()
    kept = current.get("programmes", []) if current.get("profile") == profile and \
        current.get("rules_version") == VERSIONS.get(profile) else []
    return sorted(set(kept) | {programme})


def _record(profile: str, programme: str | None = None) -> Path:
    path = agreement_path()
    if os.path.islink(path) or os.path.islink(path.parent):
        raise util.refuse("ygg agents: " + str(path) + " is a symbolic link; an agreement is never read or written "
                          "through one. Remove the link and run the command again.")
    if config.managed_folder(os.path.dirname(os.path.realpath(path))):
        raise util.refuse("ygg agents: " + str(path.parent) + " is inside an agent's working folder; an agreement "
                          "there never counts. Move the files root out of it.")
    record = {"schema": AGREEMENT_SCHEMA, "profile": profile,
              "rules_version": VERSIONS.get(profile, 0),
              "programmes": _programmes_for(profile, programme) if profile in VERSIONS else [],
              "origin": "user", "recorded_at": util.iso()}
    util.ensure_dir(path.parent)
    util.atomic_write(path, json.dumps(record, indent=2) + "\n", mode=0o600)
    return path


def _covers(path: Path) -> str:
    programmes = agreement().get("programmes") or []
    return "programmes " + ", ".join(programmes) if programmes else "no programme yet"


def allow_minimal(yes: bool = False, programme: str | None = None) -> str:
    """User-only: agree that agents get the minimal launch permissions."""
    _user_only("allow-minimal")
    path = _record(MINIMAL, programme)
    return ("Agreed, recorded in " + str(path) + " for " + _covers(path) + ": from the next launch, " + WHAT +
            ". Withdraw with `" + REVOKE + "`.\n")


def allow_standard(yes: bool = False, programme: str | None = None) -> str:
    """User-only: show the standard rules by role; with `yes`, agree to them."""
    _user_only("allow-standard")
    shown = describe(STANDARD)
    if not yes:
        return (shown + "\nNothing is recorded yet. If you agree to these rules, run `" + ALLOW_STANDARD +
                " --yes`.\n")
    path = _record(STANDARD, programme)
    return (shown + "\nAgreed to the standard rules version " + str(VERSIONS[STANDARD]) + ", recorded in " +
            str(path) + " for " + _covers(path) + ", from the next launch. Withdraw with `" + REVOKE + "`.\n")


def revoke(verb: str = "revoke") -> str:
    """User-only: withdraw the agreement; launches that need it are refused again."""
    _user_only(verb)
    path = _record(NOT_GRANTED)
    return ("Withdrawn, recorded in " + str(path) + ": launches whose agent has its own Claude settings folder "
            "are refused until you run `" + ALLOW_STANDARD + "` or `" + ALLOW + "` again. Sessions already "
            "running keep their settings.\n")


def revoke_minimal() -> str:
    return revoke("revoke-minimal")


def needed() -> bool:
    """A launch needs the grant when its agent runs with its own Claude settings folder (by default it does,
    AC-75)."""
    return bool(config.get("agent_claude_config_dir"))


def refusal(programme: str | None = None) -> str | None:
    """Why a Claude Code launch in `programme` needs the user's agreement first, or None."""
    if not needed() or granted(programme):
        return None
    return _stale() or _uncovered(programme) or REFUSAL


def hold_reason(node: dict) -> str | None:
    """Why a launch of `node` would be refused for its permissions right now, or None: the visible hold
    status shows for owed work (A7 I4), derived on every read and gone once the agreement is valid."""
    from . import launcher
    try:
        cli = launcher.model_for(node)[2]
    except util.YggError:
        return None
    if cli != "claude-code":
        return None
    # The agreement first (on either route: a T3 launch is checked the same way), then whether the agreed rules
    # can be written for the node.
    return refusal(node.get("programme")) or rule_problem(node, cli)


def rule_problem(node: dict, cli: str) -> str | None:
    """Why the agreed rules can't be written for `node` right now (a path holding a pattern character), as
    its launch would refuse: the working folder, the project folders and, on the T3 route, the session's
    prompt file. None when they can."""
    from . import launcher, state, t3, tracker
    try:
        nodes = state.all_nodes()
        cwd = launcher.work_area(node, nodes)
        is_git = (tracker.programme(node["programme"]) or {}).get("kind") in ("git-repo", "shared-tree")
        prompt = [state.root() / "launch" / "act-0" / "prompt.md"] if t3.would_use(cli) else None
        block(node, nodes, cwd, is_git, prompt)
    except util.YggError as error:
        return error.message
    except (OSError, KeyError, TypeError, ValueError):
        return None
    return None


def check(cli: str, node: dict | None = None) -> None:
    """Refuse, before anything is reserved, a Claude Code launch that needs the grant the user has not given."""
    if cli != "claude-code":
        return
    why = refusal((node or {}).get("programme"))
    if why:
        raise util.refuse(why)


def alert_needed(node: dict) -> None:
    """One `ygg-agreement-needed` alert (class user) per reason and agreement, raised when the conductor's
    launch waits on it (AC-34 row 2, AC-97 row 2): a rule-set bump, or a programme the agreement doesn't name."""
    from . import state
    programme = node.get("programme")
    if not needed() or granted(programme):
        return
    record = agreement()
    if _stale():
        reason, message, fields = "rules-changed", NEEDED_TEXT, {}
    elif _uncovered(programme):
        reason = "programme:" + str(programme)
        message = ("Yggdrasil's agent rules aren't agreed for programme " + str(programme) + ". Run ygg agents "
                   "allow-" + str(record.get("profile")) + " --programme " + str(programme) + " to see them and "
                   "agree.")
        fields = {"programme": programme}
    else:
        reason, message, fields = "no-agreement", ("Yggdrasil's agents can't start until you agree to their "
                                                   "rules. Run ygg agents allow-standard to see them, then agree."), {}
    key = str(record.get("recorded_at") or "")
    for entry in state.alerts():
        if entry.get("kind") == "ygg-agreement-needed" and entry.get("reason") == reason and \
                str(entry.get("agreement_at") or "") == key:
            return
    state.alert("ygg-agreement-needed", message, reason=reason, agreement_at=key, node=node.get("id"), **fields)


def _path(path) -> str:
    real = os.path.realpath(str(path))
    if PATTERN_CHARACTERS & set(real):
        raise util.refuse("launch precondition: " + real + " cannot be written as a permission rule (it holds "
                          "one of * ? [ ] \\ ! ( ) or a line break); move it to a plain path")
    return "/" + real  # `//<absolute path>`


def commands_for(project: str | None) -> list[str]:
    """The project's named test and build commands, from its approved PID snapshot only (AC-74): the header the
    user approved, never the live PID, a programme record or a config file."""
    if not project:
        return []
    from . import commands, tracker
    if commands.approved_pid(project) is None:
        return []
    try:
        issue = commands.pid_document(project)
    except (util.YggError, OSError, ValueError):
        return []
    approved = None
    for record in getattr(issue, "records", None) or []:
        if isinstance(record, dict) and record.get("kind") == "pid-approval" and \
                isinstance(record.get("approved_header"), dict):
            approved = record["approved_header"]
    named = (approved or {}).get("agent_commands")
    if not isinstance(named, list):
        return []
    return [str(c) for c in named if isinstance(c, str) and config.PLAIN_COMMAND.match(c)]


STANDARD_READ_RULES = ["Bash(git status)", "Bash(git status *)", "Bash(git diff)", "Bash(git diff *)",
                       "Bash(git log)", "Bash(git log *)", "Bash(git show *)", "Bash(git rev-parse *)",
                       "Bash(git branch --list)", "Bash(git branch --list *)"]
BUILDER_ROLES = ("tl", "worker")


def _build(profile: str, role, read_dirs: list[str], edit_root: str, is_git: bool, lane: str | None,
           commands: list[str], read_files: list[str] | None = None, review_dir: str | None = None) -> dict:
    """The rule sets, version 1 (test conventions, "Agent rule sets")."""
    allow = ["Bash(ygg *)"] + ["Read(" + d + "/**)" for d in read_dirs] + ["Read(" + f + ")" for f in read_files or []]
    if role == "reviewer":
        if review_dir:
            allow.append("Edit(" + review_dir + "/**)")
        allow += ["Bash(git diff *)", "Bash(git show *)"]
    elif role in ("pm",) + BUILDER_ROLES:
        allow.append("Edit(" + edit_root + "/**)")
        if role in BUILDER_ROLES and is_git:
            allow += ["Bash(git add *)", "Bash(git commit *)"]
    if profile == STANDARD:
        if is_git or role == "reviewer":
            allow += STANDARD_READ_RULES
        if role in BUILDER_ROLES and is_git:
            allow += ["Bash(git mv *)", "Bash(git rm *)", "Bash(git restore *)"]
        if role == "tl" and is_git and lane:
            allow.append("Bash(git push origin " + lane + ")")
        if role in COMMAND_ROLES:
            for command in commands:
                allow += ["Bash(" + command + ")", "Bash(" + command + " *)"]
    unique = list(dict.fromkeys(allow))
    content = {"defaultMode": "dontAsk", "allow": unique}
    if profile == STANDARD:
        content["deny"] = list(DENY)
    return content


def review_dir(activation: str) -> Path:
    """A reviewer's own scratch folder, the only place it writes (its verdict): `<state root>/scratch/<activation>`."""
    from . import state
    return state.root() / "scratch" / activation


def block(node: dict, nodes: dict, cwd, is_git: bool, read_files=None, activation: str | None = None) -> dict | None:
    """The `permissions` value of a launch settings file, or None when the user has not agreed. `read_files`
    are single files the agent must read, such as the private prompt file a T3 session is pointed at."""
    from . import tracker
    profile = agreed_profile(node.get("programme"))
    if profile is None:
        return None
    if node.get("project"):
        projects = [node["project"]]
    else:
        projects = sorted(p["id"] for p in tracker.projects() if p.get("programme") == node.get("programme"))
    lane = "ygg/" + node["project"] + "/lane/" + node["id"] if node.get("project") and node.get("id") else None
    scratch = None
    if node.get("role") == "reviewer" and activation:
        folder = util.ensure_dir(review_dir(activation))
        scratch = _path(folder)
    return _build(profile, node.get("role"), [_path(tracker.project_dir(p)) for p in projects], _path(cwd), is_git,
                  lane, commands_for(node.get("project")), [_path(f) for f in read_files or []], scratch)


ROLE_WORDS = {"pm": "Project manager", "tl": "Team lead", "worker": "Worker", "su": "Senior user",
              "reviewer": "Reviewer"}


def describe(profile: str) -> str:
    """The exact rules of `profile` by role, with the paths each launch fills in, for the user to read."""
    from . import tracker
    projects = "/" + os.path.realpath(str(tracker.root())) + "/projects/"
    lines = ["The " + profile + " agent rules, version " + str(VERSIONS[profile]) + ", as each Claude Code "
             "agent's launch settings hold them. Anything no rule allows is denied at once (dontAsk). <worktree> "
             "is the agent's own working folder and <review dir> a reviewer's scratch folder; in a folder that is "
             "not a Git checkout there is no git rule. <named commands> are the test and build commands the "
             "approved PID lists in agent_commands, each as `Bash(C)` and `Bash(C *)`."]
    for role in ("pm", "tl", "worker", "su", "reviewer"):
        rules = _build(profile, role, [projects + "<project>"], "//<worktree>", True,
                       "ygg/<project>/lane/<team lead>", ["<named commands>"] if role in COMMAND_ROLES else [],
                       review_dir="//<review dir>")
        lines.append("")
        lines.append(ROLE_WORDS[role] + ":")
        lines += ["  allow " + rule for rule in rules["allow"] if "<named commands> *" not in rule]
        lines += ["  deny  " + rule for rule in rules.get("deny", [])]
    return "\n".join(lines) + "\n"


REVIEWER_MESSAGE = "Reviewers review read-only; write your verdict to $YGG_REVIEW_DIR/verdict.json."
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def reviewer_refusal(tool: str, tool_input: dict, activation: str | None) -> str | None:
    """For the guard (`hooks.pretool`, lane 1), on a reviewer's tool call: edits only inside its own review folder
    (AC-94 row 1) and no git command that writes (AC-32 row 3). Read-only git shapes are the guard's own check
    (A11 AC-21); a git command that isn't one of them is refused here. Returns the refusal text, or None."""
    if tool in EDIT_TOOLS:
        target = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if target and activation:
            folder = os.path.realpath(str(review_dir(activation)))
            real = os.path.realpath(target)
            if real == folder or real.startswith(folder + os.sep):
                return None
        return REVIEWER_MESSAGE
    if tool == "Bash":
        cwd = os.getcwd()
        if activation:
            from . import launcher
            cwd = str(launcher.receipt(activation).get("cwd") or cwd)  # the reviewer's checkout
        return _reviewer_bash_refusal(str(tool_input.get("command") or ""), cwd)
    return None


REVIEWER_SEPARATORS = (";", "&&", "||", "|")
# Commands that run another command: refused for a reviewer whatever follows them. Every wrapper and shell the
# guard itself recognises (hooks.WRAPPERS, hooks.SHELLS) is added where the check runs, so a wrapper hooks learns
# later is refused here too.
REVIEWER_REFUSED_HEADS = {"eval", "exec", "env", "xargs", "command", "builtin", "nohup", "time", "sudo", "doas",
                          "nice", "timeout", "stdbuf", "setsid", "source", ".", "alias", "unalias", "function",
                          "trap", "fc", "parallel", "script", "watch", "su", "osascript", "awk", "find", "make"}
# Interpreters and shells by name, versioned forms included (python3.12, bash5, ...); anything else ending in
# "sh" counts as a shell.
_INTERPRETER = __import__("re").compile(r"^(python|perl|ruby|node|deno|bun|php|lua|rscript|osascript)", __import__("re").I)
_SHELL = __import__("re").compile(r"(sh|^fish|^tclsh)[0-9.]*$", __import__("re").I)
_ASSIGNMENT = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_GIT_IN_WORD = __import__("re").compile(r"(^|[^A-Za-z0-9_-])git([^A-Za-z0-9_-]|$)")


def _reviewer_bash_refusal(command: str, cwd: str) -> str | None:
    """A reviewer's Bash call passes only by allowlist (AC-32 row 3, AC-94): every simple command in it is either a
    command that doesn't run git, or git (with no global option but --no-pager) in one of the signed AC-21 read
    shapes, judged by the guard's own validator (hooks._shape), the single source of truth for them.

    This is defence in depth. The primary barriers are the reviewer's permission rules (dontAsk, its rule set)
    and its sandbox (a Codex reviewer gets no hooks at all, A11 scope): a running program can always build a
    command name a text check can't see. So the check fails closed on anything it can't read plainly: it splits
    the command itself (only ; && || | between commands) and refuses any substitution, variable, backslash,
    subshell, redirection or assignment; any command that runs another command (eval, exec, env, xargs, find,
    nice, sudo and the like); any interpreter or shell by name, versioned forms included; git anywhere but at a
    command's start; and anything the guard's own reader leaves unresolved."""
    import shlex
    from . import hooks
    if not command.strip():
        return None
    if any(mark in command for mark in ("$", "`", "\\")) or hooks.unclear_text(command):
        return REVIEWER_MESSAGE
    try:
        lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
        found = hooks.segments(command)
    except Exception:  # noqa: BLE001  (any parse failure refuses)
        return REVIEWER_MESSAGE
    if any(parts == hooks.UNRESOLVED or (parts and hooks._unresolved(parts)) for parts in found):
        return REVIEWER_MESSAGE
    refused = REVIEWER_REFUSED_HEADS | set(hooks.WRAPPERS) | set(hooks.SHELLS)
    for parts in found:
        # Every command word the guard's reader exposes, wrappers unwrapped: an interpreter or shell is refused
        # wherever it sits (setsid --fork python3.12 ...).
        head = os.path.basename(str(parts[0])) if parts else ""
        if parts and parts[0] != "__assign__" and (head in refused or _INTERPRETER.match(head) or _SHELL.search(head)):
            return REVIEWER_MESSAGE
    commands, current = [], []
    for token in tokens:
        if token and all(char in ";&|()<>" for char in token):
            if token not in REVIEWER_SEPARATORS:
                return REVIEWER_MESSAGE  # a subshell, a redirection or a background job
            commands.append(current)
            current = []
            continue
        current.append(token)
    commands.append(current)
    for words in commands:
        if not words:
            continue
        if _ASSIGNMENT.match(words[0]):
            return REVIEWER_MESSAGE
        head = os.path.basename(words[0])
        if head in refused or _INTERPRETER.match(head) or _SHELL.search(head):
            return REVIEWER_MESSAGE
        if head != "git" and any(_GIT_IN_WORD.search(word) for word in words[1:]):
            return REVIEWER_MESSAGE  # git run by another command, or named as its argument
        if head == "git":
            # The guard's AC-21 validator alone judges a direct git command's arguments (ygg/git is a revision).
            rest = words[1:]
            if rest[:1] == ["--no-pager"]:
                rest = rest[1:]
            if not rest or rest[0].startswith("-") or not hooks._shape(rest[0], rest[1:], cwd):
                return REVIEWER_MESSAGE  # a global option, or not one of the read shapes
        elif _GIT_IN_WORD.search(words[0]):
            return REVIEWER_MESSAGE
    return None
