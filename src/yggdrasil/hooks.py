"""Guard hooks: the Claude Code PreToolUse hook and hard-stop matching."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

from . import state, util

T3_TOOL = re.compile(r"^mcp__t3", re.I)
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
CURL_SEND_LONG = ("--data", "--data-binary", "--data-raw", "--data-ascii", "--data-urlencode", "--form",
                  "--form-string", "--upload-file", "--request", "--json", "--post-data", "--post-file",
                  "--body-data", "--body-file", "--method")
CURL_SEND_SHORT = ("-d", "-F", "-T", "-X")


PUNCT = ";&|()<>`"
SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


# A "#" at the start of a word: at the start of a line, or after a space, tab, newline or operator.
WORD_START_HASH = re.compile(r"(?:^|(?<=[ \t\n;&|()<>`]))#")


def unclear_text(text: str) -> bool:
    """Text the hook does not try to read like a shell would, so it is unresolved: a carriage return, a line
    continuation, whitespace other than space, tab and newline, a "#" inside a word or with arithmetic,
    parameter expansion or a test (`((`, `${`, `[[`), or a word-start "#" in a command that also holds a
    quote or backslash anywhere."""
    if "\\\n" in text or any(char.isspace() and char not in " \t\n" for char in text):
        return True
    if "#" not in text:
        return False
    if any(opening in text for opening in ("((", "$((", "${", "[[")):
        return True  # inside these a "#" is no comment to the shell, but shlex reads one
    starts = {match.start() for match in WORD_START_HASH.finditer(text)}
    if any(char == "#" and index not in starts for index, char in enumerate(text)):
        return True  # shlex would read it as a comment; the shell reads it as part of the word
    return bool(starts) and any(char in text for char in "'\"\\`")


def segments(command: str, depth: int = 0) -> list[list[str]]:
    """Split a shell command into simple commands, unwrapping env, command and sh -c."""
    if unclear_text(command):
        return [UNRESOLVED]
    # The whole command is read once. Each line starts with a ";", which a comment (ending at its newline)
    # cannot swallow, so lines stay separate commands. Text shlex cannot read is unresolved, never guessed.
    text = command.replace("\n", "\n; ")
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=PUNCT)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError:
        return [UNRESOLVED]
    if any("\n" in token for token in tokens):
        return [UNRESOLVED]  # a quoted string spanning a newline
    found, current = [], []
    for token in tokens:
        if token and all(c in PUNCT for c in token):
            if current:
                found.extend(unwrap(current, depth))
                current = []
            continue
        if "$(" in token or "`" in token or "<(" in token:
            matches = SUBST.findall(token)
            if depth >= 4 or len(matches) < token.count("$(") + token.count("`") // 2 + token.count("<(") \
                    or token.count("`") % 2:
                found.append(UNRESOLVED)
            for first, second in matches:
                found.extend(segments(first or second, depth + 1))
        current.append(token)
    if current:
        found.extend(unwrap(current, depth))
    return found


UNRESOLVED = ["__unresolved__"]
# Options each wrapper may carry: name -> (flags without value, flags with a value).
WRAPPER_OPTIONS = {
    "command": ({"-p"}, set()),
    "builtin": (set(), set()),
    "exec": ({"-c", "-l"}, {"-a"}),
    "nohup": (set(), set()),
    "time": ({"-p"}, set()),
    "sudo": (set(), set()),
    "nice": (set(), {"-n"}),
    "timeout": ({"--preserve-status", "--foreground", "-v", "--verbose"}, {"-s", "-k", "--signal", "--kill-after"}),
    "stdbuf": (set(), {"-i", "-o", "-e"}),
    "setsid": ({"-f", "-w", "-c", "--fork", "--wait", "--ctty"}, set()),
}
WRAPPERS = set(WRAPPER_OPTIONS)


def _wrapper_options(head: str, words: list[str]) -> bool:
    """Consume a wrapper's options; return False when an option is not recognised."""
    flags, valued = WRAPPER_OPTIONS[head]
    while words and words[0].startswith("-") and words[0] != "-":
        option = words.pop(0)
        if option == "--":
            break
        if option in flags:
            continue
        if option in valued and words:
            words.pop(0)
            continue
        joined = next((v for v in valued if option.startswith(v) and len(option) > len(v)), None)
        if joined and (joined.startswith("--") and option[len(joined):].startswith("=") or
                       not joined.startswith("--")):
            continue
        return False
    if head == "timeout":
        if not words or not re.fullmatch(r"[0-9.]+[smhd]?", words[0]):
            return False
        words.pop(0)
    return True


def unwrap(words: list[str], depth: int) -> list[list[str]]:
    words = list(words)
    assignments = []
    prefix = []
    while words:
        head = os.path.basename(words[0])
        if ENV_ASSIGN.match(words[0]):
            assignments.append(words.pop(0))
            continue
        if head == "env":
            words.pop(0)
            while words and (words[0].startswith("-") or ENV_ASSIGN.match(words[0])):
                option = words.pop(0)
                if option in ("-u", "--unset") and words:
                    assignments.append("unset:" + words.pop(0))
                elif option.startswith("--unset="):
                    assignments.append("unset:" + option.split("=", 1)[1])
                elif option.startswith("-u") and len(option) > 2:
                    assignments.append("unset:" + option[2:])
                elif option in ("-i", "-", "--ignore-environment"):
                    assignments.append("unset:YGG_NODE")
                elif option == "--":
                    break
                elif ENV_ASSIGN.match(option):
                    assignments.append(option)
                else:
                    return prefix + [UNRESOLVED]
            continue
        if head in WRAPPERS:
            words.pop(0)
            if not _wrapper_options(head, words):
                return prefix + [UNRESOLVED]
            continue
        if head in SHELLS:
            if assignments:
                prefix.append(["__assign__", *assignments])
            arguments = words[1:]
            for index, word in enumerate(arguments):
                if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
                    if index + 1 >= len(arguments) or depth >= 4:
                        return prefix + [UNRESOLVED]
                    inner_text = arguments[index + 1]
                    if "$" in inner_text or "`" in inner_text and SUBST.search(inner_text) is None:
                        return prefix + [UNRESOLVED]
                    return prefix + segments(inner_text, depth + 1)
            # A shell fed by stdin, a pipe, a redirect or a script file cannot be resolved.
            return prefix + [UNRESOLVED]
        break
    result = list(prefix)
    if assignments:
        result.append(["__assign__", *assignments])
    if words:
        result.append([os.path.basename(words[0]), *words[1:]])
    return result


def _touches_ygg_env(parts: list[str]) -> bool:
    if parts[0] in ("read", "mapfile", "readarray", "getopts"):
        return any(p.startswith("YGG_") for p in parts[1:])
    if parts[0] == "printf":
        for index, word in enumerate(parts[1:], start=1):
            if word == "-v" and index + 1 < len(parts) and parts[index + 1].startswith("YGG_"):
                return True
            if word.startswith("-vYGG_"):
                return True
        return False
    if parts[0] == "__assign__":
        return any(a.split(":", 1)[-1].split("=", 1)[0].startswith("YGG_") for a in parts[1:])
    if parts[0] in ("unset", "export", "declare", "typeset", "local", "readonly", "setenv", "unsetenv"):
        return any(p.lstrip("-").split("=", 1)[0].startswith("YGG_") for p in parts[1:])
    return False


def _curl_sends(parts: list[str]) -> bool:
    for word in parts[1:]:
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            if name in CURL_SEND_LONG:
                return True
        elif word.startswith("-") and len(word) > 1:
            letters = word[1:]
            if any(letter in "dFTX" for letter in letters):
                return True
    return False


PLAIN_WORD = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./+-]*$|^/[A-Za-z0-9_./+-]+$|^\./[A-Za-z0-9_./+-]+$")
RESERVED_WORDS = {"{", "}", "!", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "case",
                  "esac", "function", "for", "select", "in", "[[", "]]", "coproc"}
UNRESOLVABLE_HEADS = {"eval", "source", ".", "xargs", "parallel", "exec", "trap", "alias", "fc", "command_not_found_handle"}


def _unresolved(parts: list[str]) -> bool:
    if parts == UNRESOLVED:
        return True
    if parts[0] == "__assign__":
        return False
    head = parts[0]
    if head in RESERVED_WORDS or head in UNRESOLVABLE_HEADS or not PLAIN_WORD.match(head):
        return True
    for token in parts:
        stripped = SUBST.sub("", token)
        if "$" in stripped or "`" in stripped:
            return True
    if head == "find" and any(p in ("-exec", "-execdir", "-ok", "-okdir") for p in parts[1:]):
        return True
    return False


# --exec-path is deliberately absent: it chooses which git programs run, so a call naming it is unresolved.
GIT_VALUED = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix",
              "--list-cmds"}
GIT_FLAGS = {"--no-pager", "-p", "--paginate", "-P", "--bare", "--literal-pathspecs", "--no-optional-locks",
             "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs", "--no-replace-objects"}


def _git_normalised(parts: list[str]) -> list[str] | None:
    """Drop git's global options so the subcommand is parts[1]; None when an option is not recognised."""
    rest = list(parts[1:])
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)
        name = option.split("=", 1)[0]
        if option in GIT_FLAGS:
            continue
        if name in GIT_VALUED:
            if "=" not in option:
                if not rest:
                    return None
                rest.pop(0)
            continue
        return None
    return ["git", *rest]


# A git subcommand that is not a built-in runs an alias or a `git-<name>` program found on PATH, which the
# hook cannot resolve, so it is unresolved. Built-ins cannot be overridden either way.
CLASSIFIED_SCRIPTS = {"filter-branch", "filter-repo"}  # not built-ins, but classified as history rewrites
FALLBACK_BUILTINS = {"add", "am", "apply", "bisect", "blame", "branch", "cat-file", "checkout", "cherry-pick",
                     "clean", "clone", "commit", "config", "describe", "diff", "fetch", "format-patch", "grep",
                     "init", "log", "ls-files", "ls-remote", "ls-tree", "merge", "merge-base", "mv", "notes",
                     "pull", "push", "rebase", "reflog", "remote", "replace", "reset", "restore", "rev-list",
                     "rev-parse", "revert", "rm", "show", "show-ref", "stash", "status", "switch", "symbolic-ref",
                     "tag", "update-ref", "version", "worktree"}
_BUILTINS: set = set()


def git_builtins() -> set:
    if not _BUILTINS:
        import subprocess
        try:
            listed = subprocess.run(["git", "--list-cmds=builtins"], capture_output=True, text=True,
                                    timeout=10).stdout.split()
        except (OSError, subprocess.SubprocessError):
            listed = []
        _BUILTINS.update(listed or FALLBACK_BUILTINS)
    return _BUILTINS


# PATH or GIT_EXEC_PATH named as a variable by any word: an assignment (`PATH=`, `PATH+=`), an unset, a
# bare name for `readonly`, `printf -v`, `read`, `declare -n` and the like, or a nameref value (`p=PATH`).
GIT_PATH_TOKEN = re.compile(r"(^|[=:])(PATH|GIT_EXEC_PATH)(\+?=|$)")
# The same names anywhere in the command text once every quote and backslash is removed, so quoting,
# braces and escapes cannot hide them. In a command that runs git, either makes the command unresolved.
GIT_PATH_WORD = re.compile(r"(?<![A-Za-z0-9_])(PATH|GIT_EXEC_PATH)(?![A-Za-z0-9_])")


def _names_git_path(command: str, found: list[list[str]]) -> bool:
    if GIT_PATH_WORD.search(re.sub(r"[\'\"\\]", "", command)):
        return True
    return any(GIT_PATH_TOKEN.search(word) for parts in found for word in parts)


BYPASS_KEY = re.compile(r"^(core\.hookspath|core\.fsmonitor|alias\..+|include\.path|includeif\..+\.path|"
                        r"extensions\.worktreeconfig)$", re.I)
BYPASS_ENV = re.compile(r"^(GIT_CONFIG_PARAMETERS|GIT_CONFIG_COUNT|GIT_CONFIG_KEY_[0-9]+)$")
# git config actions, as legacy options and as subcommands (git 2.46 and later).
CONFIG_ACTION_OPTIONS = {
    "--get": "read", "--get-all": "read", "--get-regexp": "read", "--get-urlmatch": "read",
    "--get-color": "read", "--get-colorbool": "read", "-l": "read", "--list": "read",
    "--replace-all": "write", "--add": "write", "--unset": "write", "--unset-all": "write",
    "--rename-section": "section", "--remove-section": "section", "-e": "edit", "--edit": "edit",
}
CONFIG_SUBCOMMANDS = {
    "get": "read", "list": "read", "get-color": "read", "get-colorbool": "read",
    "set": "write", "unset": "write", "rename-section": "section", "remove-section": "section", "edit": "edit",
}
CONFIG_VALUED = {"--file", "-f", "--blob", "--type", "--default", "--comment", "--value"}
GUARDED_SECTIONS = re.compile(r"^(core|alias|include|includeif|extensions)(\..*)?$", re.I)


def _config_key(setting: str) -> str:
    return setting.split("=", 1)[0].strip()


def _config_bypass(arguments: list[str]) -> bool:
    """Whether a `git config` call writes a guarded key or edits a section that holds one."""
    action, positional, words = None, [], list(arguments)
    while words:
        word = words.pop(0)
        if word in CONFIG_ACTION_OPTIONS:
            action = action or CONFIG_ACTION_OPTIONS[word]
        elif word in CONFIG_VALUED:
            if words:
                words.pop(0)
        elif word == "--":
            positional.extend(words)
            break
        elif not word.startswith("-"):
            positional.append(word)
    if action is None and positional and positional[0] in CONFIG_SUBCOMMANDS:
        action, positional = CONFIG_SUBCOMMANDS[positional[0]], positional[1:]
    if action is None:
        action = "write" if len(positional) >= 2 else "read"
    if action == "read":
        return False
    if action == "edit":
        return True
    if action == "section":
        return any(GUARDED_SECTIONS.match(name) for name in positional[:2])
    return bool(positional) and BYPASS_KEY.match(_config_key(positional[0])) is not None


# git commit's short options that take a value (attached, or the next word) and those whose value is optional
# and attached only; the long ones that take a value.
COMMIT_VALUED_SHORT = set("mFCct")
COMMIT_OPTIONAL_SHORT = set("Su")
COMMIT_VALUED_LONG = {"--message", "--file", "--reuse-message", "--reedit-message", "--fixup", "--squash",
                      "--author", "--date", "--template", "--cleanup", "--trailer", "--pathspec-from-file"}


def _commit_skips_hooks(arguments: list[str]) -> bool:
    """Whether `git commit` arguments skip the pre-commit and commit-msg hooks: --no-verify (or an
    abbreviation), -n, or a cluster of short flags holding n."""
    words = list(arguments)
    while words:
        word = words.pop(0)
        if word == "--":
            return False
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            if len(name) >= len("--no-v") and "--no-verify".startswith(name):
                return True
            if name in COMMIT_VALUED_LONG and "=" not in word and words:
                words.pop(0)
            continue
        if word.startswith("-") and len(word) > 1:
            for index, letter in enumerate(word[1:]):
                if letter == "n":
                    return True
                if letter in COMMIT_VALUED_SHORT:
                    if index == len(word) - 2 and words:
                        words.pop(0)  # the value is the next word
                    break
                if letter in COMMIT_OPTIONAL_SHORT:
                    break
    return False


def _hook_bypass(parts: list[str]) -> bool:
    """A git call that skips or replaces the pre-push or the lane commit hooks, or a config override that can."""
    if parts[0] == "__assign__" or parts[0] in ("export", "declare", "typeset", "setenv"):
        names = [a.split(":", 1)[-1].split("=", 1)[0] for a in parts[1:]]
        return any(BYPASS_ENV.match(n.lstrip("-")) for n in names)
    if parts[0] != "git":
        return False
    rest = list(parts[1:])
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)
        if option == "-c" and rest:
            if BYPASS_KEY.match(_config_key(rest.pop(0))):
                return True
        elif option.startswith("--config-env"):
            value = option.split("=", 1)[1] if "=" in option else (rest.pop(0) if rest else "")
            if BYPASS_KEY.match(_config_key(value)):
                return True
        elif option in GIT_VALUED and "=" not in option and rest:
            rest.pop(0)
    if not rest:
        return False
    if rest[0] == "push" and "--no-verify" in rest[1:]:
        return True
    if rest[0] == "commit" and _commit_skips_hooks(rest[1:]):
        return True
    if rest[0] == "config":
        return _config_bypass(rest[1:])
    return False


def _ref_write(parts: list[str]) -> bool:
    """A git call that writes or replaces a ref directly instead of through a guarded push."""
    if len(parts) < 2 or parts[0] != "git":
        return False
    sub, arguments = parts[1], parts[2:]
    if sub == "update-ref":
        return True
    if sub == "symbolic-ref":
        positional = [a for a in arguments if not a.startswith("-")]
        return "-d" in arguments or "--delete" in arguments or len(positional) >= 2
    if sub == "replace":
        return not (arguments and arguments[0] in ("-l", "--list"))
    return False


SENIOR_GIT = {"status", "log", "diff", "show", "rev-parse", "ls-files", "grep", "blame"}


def senior_git_allowed(command: str) -> bool:
    """Senior users may run git only with an allowlisted read-only subcommand and no config overrides."""
    for parts in segments(command):
        if not parts or parts[0] != "git":
            continue
        if any(p in ("-c", "--config-env") or p.startswith("--config-env=") for p in parts[1:]):
            return False
        normalised = _git_normalised(parts)
        if normalised is not None and len(normalised) > 1 and normalised[1] == "branch" and _is_read(parts):
            continue  # a branch listing; the read-only shapes already held it to `git branch --list [pattern]`
        if normalised is None or len(normalised) < 2 or normalised[1] not in SENIOR_GIT:
            return False
    return True


def hardstop_category(command: str) -> str | None:
    found = segments(command)
    if any(parts and parts[0] == "git" for parts in found) and _names_git_path(command, found):
        return "unresolved"
    for parts in found:
        if _unresolved(parts):
            if parts and parts[0] in ("find",) and "-delete" in parts:
                return "delete"
            return "unresolved"
        if _touches_ygg_env(parts):
            return "identity"
        if _hook_bypass(parts):
            return "hook-bypass"
        head = parts[0]
        if head in ("eval", "source", "."):
            return "unresolved"
        if head == "t3":
            return "t3"
        if head == "security":
            return "credentials"  # the macOS Keychain CLI: tracker credentials live there
        if head == "git":
            parts = _git_normalised(parts)
            if parts is None:
                return "unresolved"
            if len(parts) > 1 and parts[1] not in git_builtins() and parts[1] not in CLASSIFIED_SCRIPTS:
                return "unresolved"
            if _ref_write(parts):
                return "ref-write"
        if head == "git" and "push" in parts:
            rest = parts[parts.index("push") + 1:]
            if any(p.startswith("--force") or p in ("-f",) or (p.startswith("-") and not p.startswith("--")
                   and "f" in p[1:]) or (p.startswith("+") and len(p) > 1) for p in rest):
                return "force"
            if any(p in ("--delete", "-d") or p.startswith(":") or "(delete)" in p for p in rest):
                return "delete"
            if any(p in ("--mirror", "--all", "--tags", "--follow-tags") for p in rest):
                return "push"
            refs = [p for p in rest if not p.startswith("-")]
            if len(refs) < 2:
                return "push"
            for ref in refs[1:]:
                destination = ref.split(":")[-1].replace("refs/heads/", "")
                if not destination.startswith("ygg/"):
                    return "push"
            continue
        if head in ("rm", "rmdir", "shred", "unlink"):
            if head != "rm" or any(p.startswith("-") and ("r" in p or "R" in p) for p in parts[1:]) or \
                    "--recursive" in parts:
                return "delete"
        if head == "find" and ("-delete" in parts or "-exec" in parts and "rm" in parts):
            return "delete"
        if head == "git" and len(parts) > 2 and parts[1] == "branch" and \
                any(p in ("-D", "-d", "--delete") for p in parts[2:]):
            return "delete"
        if head == "git" and len(parts) > 1 and parts[1] in ("filter-branch", "filter-repo"):
            return "rewrite-history"
        if head == "git" and len(parts) > 1 and parts[1] == "reset" and "--hard" in parts:
            return "rewrite-history"
        if head in ("curl", "wget", "http", "https", "xh") and _curl_sends(parts):
            return "send-external"
        if head in ("scp", "rsync", "sftp", "ftp", "nc", "ncat", "sendmail", "mail"):
            return "send-external"
    return None


UNAVAILABLE = "Yggdrasil tracker unavailable or corrupt; stop work now."

# Git options that read files outside the repository (--no-index) or write one anywhere (--output), matched with
# the abbreviations git accepts, in any position of any git call.
FILE_OPTIONS = {"--no-index": len("--no-i"), "--output": len("--out")}
# Read-only git (security review): rather than model the shell, only a fixed set of exact shapes is
# allowed, as one simple command with no paths and no option that takes a file. Anything else that reads with
# git is refused with READ_ONLY_MESSAGE.
READ_GIT = {"status", "diff", "log", "show", "rev-parse", "branch", "grep", "blame", "ls-files", "ls-tree",
            "cat-file", "shortlog", "describe", "rev-list", "for-each-ref", "show-ref", "diff-tree", "diff-index",
            "diff-files", "whatchanged", "range-diff", "name-rev", "reflog", "format-patch", "archive",
            "show-branch", "cherry", "count-objects", "verify-commit", "verify-tag", "annotate"}
BRANCH_WRITES = {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-u", "--set-upstream-to",
                 "--unset-upstream", "--edit-description", "-f", "--force", "--track", "-t", "--no-track"}
SHELL_META = set(";&|<>$`()\n\r\\{}?[]!#'\"=")
REV = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/^@-]*(~[0-9]*)*$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/*-]*$")
# AC-21 shape 5: `git rev-parse HEAD | --abbrev-ref HEAD | --show-toplevel | <rev>`.
REV_PARSE_LONE = {"--show-toplevel"}
REV_PARSE_FLAGS = {"--abbrev-ref"}
# The settings rules that mirror the shapes: exact where a shape takes no revision, prefixed where it does (the
# guard hook then holds the revision to one plain word).
READ_ONLY_RULES = ["Bash(git status)", "Bash(git status --short)", "Bash(git status --porcelain)",
                   "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)", "Bash(git rev-parse *)",
                   "Bash(git branch --list *)"]
READ_ONLY_MESSAGE = (
    "Read-only git allows only: git status, git diff, git log, git show, git rev-parse, git branch --list, with "
    "no paths and no -C. Denied: run one of these shapes as one simple command from your worktree: "
    "git status [--short|-s|--porcelain|--branch|-b]; "
    "git diff [--cached|--staged|--stat|--name-only|--name-status] [<rev>..<rev> | <rev> [<rev>]]; "
    "git log [--oneline] [--stat] [-n N|--max-count=N] [<rev> | <rev>..<rev>]; git show [--stat|--name-only] [<rev>]; "
    "git rev-parse HEAD | --abbrev-ref HEAD | --show-toplevel | <rev>; git branch --list [-v]. A revision is a "
    "commit id, HEAD, HEAD~N, main or a ygg/ branch. No cd, env, assignments, pipes or other commands in the "
    "same call.")
GIT_LOCATION_ENV = re.compile(r"^(GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR|GIT_OBJECT_DIRECTORY|"
                              r"GIT_ALTERNATE_OBJECT_DIRECTORIES|GIT_INDEX_FILE|GIT_CEILING_DIRECTORIES|"
                              r"GIT_NAMESPACE|GIT_DISCOVERY_ACROSS_FILESYSTEM)$")


def _file_option(word: str) -> bool:
    name = word.split("=", 1)[0]
    return any(name == full or (len(name) >= shortest and full.startswith(name))
               for full, shortest in FILE_OPTIONS.items())


def _inside(path: str, root: str) -> bool:
    real, top = os.path.realpath(path), os.path.realpath(root)
    return real == top or real.startswith(top.rstrip(os.sep) + os.sep)


def _is_read(parts: list[str]) -> bool:
    normalised = _git_normalised(parts)
    if normalised is None:
        return True  # unreadable global options: treated as a read, so it must match a shape (and won't)
    if len(normalised) < 2 or normalised[1] not in READ_GIT:
        return False
    if normalised[1] == "branch":
        arguments = normalised[2:]
        return not any(a in BRANCH_WRITES or a.split("=", 1)[0] in BRANCH_WRITES for a in arguments) and \
            (not [a for a in arguments if not a.startswith("-")] or "--list" in arguments or "-l" in arguments)
    return True


def reads_with_git(command: str) -> bool:
    """Whether any part of the command reads with git (any segment, however it is wrapped)."""
    return any(parts and parts[0] == "git" and _is_read(parts) for parts in segments(command))


# A revision (AC-21): a 7 to 40 character commit id, HEAD, HEAD~N, main or a branch under ygg/.
REVISION = re.compile(r"^(?:[0-9a-f]{7,40}|HEAD(?:~[0-9]*)?|main|ygg/[A-Za-z0-9_][A-Za-z0-9._/-]*)$")
STATUS_FLAGS = {"--short", "-s", "--porcelain", "--branch", "-b"}
DIFF_FLAGS = {"--cached", "--staged", "--stat", "--name-only", "--name-status"}
SHOW_FLAGS = {"--stat", "--name-only"}


def _rev(word: str, cwd: str) -> bool:
    # A revision is one plain word that names no file: git would read an existing name as a path.
    return bool(REVISION.match(word)) and ".." not in word.split("/") and \
        not os.path.lexists(os.path.join(cwd, word))


def _range(word: str, cwd: str) -> bool:
    """`<rev>..<rev>` or `<rev>...<rev>`."""
    separator = "..." if "..." in word else ".."
    parts = word.split(separator)
    return len(parts) == 2 and all(_rev(part, cwd) for part in parts)


def _revisions(words: list[str], cwd: str, *, pairs: bool) -> bool:
    """At most one revision or range; with `pairs`, two plain revisions also count (git diff A B)."""
    if not words:
        return True
    if len(words) == 1:
        return _rev(words[0], cwd) or _range(words[0], cwd)
    return pairs and len(words) == 2 and all(_rev(word, cwd) for word in words)


def _shape(sub: str, arguments: list[str], cwd: str) -> bool:
    """The six read-only shapes of AC-21, each flag at most once."""
    words = list(arguments)
    if sub == "status":
        return all(word in STATUS_FLAGS for word in words) and len(set(words)) == len(words)
    if sub == "diff":
        flags = [word for word in words if word.startswith("-")]
        if any(flag not in DIFF_FLAGS for flag in flags) or len(set(flags)) != len(flags):
            return False
        return _revisions([word for word in words if not word.startswith("-")], cwd, pairs=True)
    if sub == "log":
        rest, seen = [], set()
        while words:
            word = words.pop(0)
            if word in ("--oneline", "--stat") and word not in seen:
                seen.add(word)
            elif word == "-n" and "n" not in seen:
                if not words or not words[0].isdigit():
                    return False
                words.pop(0)
                seen.add("n")
            elif re.fullmatch(r"--max-count=[0-9]+|-n[0-9]+", word) and "n" not in seen:
                seen.add("n")
            elif word.startswith("-"):
                return False
            else:
                rest.append(word)
        return _revisions(rest, cwd, pairs=False)
    if sub == "show":
        flags = [word for word in words if word.startswith("-")]
        if any(flag not in SHOW_FLAGS for flag in flags) or len(flags) > 1:
            return False
        rest = [word for word in words if not word.startswith("-")]
        return not rest or (len(rest) == 1 and _rev(rest[0], cwd))
    if sub == "rev-parse":
        if len(words) == 1 and words[0] in REV_PARSE_LONE:
            return True
        if words[:1] == ["--abbrev-ref"]:
            return words[1:] == ["HEAD"]
        return len(words) == 1 and _rev(words[0], cwd)
    if sub == "branch":
        if words[:1] != ["--list"]:
            return False
        rest = words[1:]
        if rest[:1] == ["-v"]:
            rest = rest[1:]
        return not rest or (len(rest) == 1 and bool(BRANCH_PATTERN.match(rest[0])))
    return False


def _shape_words(command: str) -> list[str] | None:
    """The words of a command that may be a read shape: one simple git command, `--no-pager` dropped. An `=`
    is read by the grammar (`--max-count=N`); an assignment before git fails the first-word check."""
    if set(command) & (SHELL_META - {"="}):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if words[:2] == ["git", "--no-pager"]:
        words = ["git", *words[2:]]
    return words if len(words) >= 2 and words[0] == "git" else None


def read_git_refusal(command: str, cwd: str, root: str, *, every_git: bool = False) -> str | None:
    """READ_ONLY_MESSAGE when a command that reads with git is not one allowed shape, run inside `root`. With
    `every_git` (senior users and reviewers, who may run no other git), any git call is held to the shapes."""
    if every_git:
        if not any(parts and parts[0] == "git" for parts in segments(command)) and \
                not re.search(r"(^|[^A-Za-z0-9_.-])git([^A-Za-z0-9_.-]|$)", command):
            return None
    elif not reads_with_git(command):
        return None
    if not _inside(cwd, root):
        return READ_ONLY_MESSAGE
    words = _shape_words(command)
    if words is None or not _shape(words[1], words[2:], cwd):
        return READ_ONLY_MESSAGE
    return None


REPOSITORY_MESSAGE = ("Denied: git from this folder selects a repository other than your lane's (a nested or "
                      "rewritten .git), or your launch recorded none. Run read-only git from your own worktree.")


def same_repository(cwd: str, recorded: dict, root: str) -> bool:
    """Whether git, run from `cwd`, selects the repository the launch recorded for the lane: the same git
    directory and the same common git directory (so neither a nested or rewritten .git nor redirected lane-local
    metadata such as commondir or HEAD can reach another repository), with neither inside the lane itself, where
    the agent could rewrite them. Nothing recorded fails closed."""
    wanted = (recorded.get("git_dir"), recorded.get("git_common_dir"))
    if not all(wanted):
        return False
    selected = util.trusted_git("-C", cwd, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    lines = [line.strip() for line in selected.stdout.splitlines() if line.strip()]
    if selected.returncode != 0 or len(lines) != 2:
        return False
    found = tuple(os.path.realpath(line) for line in lines)
    if found != tuple(os.path.realpath(str(path)) for path in wanted):
        return False
    return not any(_inside(path, root) for path in found)


def sets_git_location(command: str) -> bool:
    """An assignment, export or env prefix of a variable that points git at another repository or index."""
    for parts in segments(command):
        if not parts:
            continue
        if parts[0] == "__assign__" or parts[0] in ("export", "declare", "typeset", "setenv", "env"):
            names = [a.split(":", 1)[-1].split("=", 1)[0] for a in parts[1:] if "=" in a or parts[0] != "env"]
            if any(GIT_LOCATION_ENV.match(n.lstrip("-")) for n in names):
                return True
    return bool(re.search(r"(^|[\s;&|(])(export\s+|env\s+)?(" + GIT_LOCATION_ENV.pattern[2:-2] + r")=", command))


OUTSIDE_MESSAGE = ("git --no-index and --output read or write files outside your worktree and are denied in "
                   "managed sessions, in any position.")


def token_store_paths() -> list[str]:
    """Paths of a file-based Linear token store (best effort; a credential broker is planned for a later release)."""
    paths = []
    try:
        from . import config
        if config.get("linear_token_file"):
            paths.append(os.path.expanduser(str(config.get("linear_token_file"))))
    except Exception:  # noqa: BLE001  (configuration problems are reported elsewhere; the guard stays static)
        pass
    fake = util.env("YGG_LINEAR_FAKE")
    if fake:
        paths.append(os.path.join(fake, "token-store.json"))
    return [p for p in paths if p]


FILE_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Grep", "Glob")
PATH_FIELDS = ("file_path", "notebook_path", "path", "pattern")
GUARDED_NAMES = (".claude", ".ygg-managed")
GLOB_CHARS = re.compile(r"[*?\[\]{}]")
# The raw-text checks stay next to the word checks (either one denies): the raw command, case-folded.
GUARD_TEXT = re.compile(r"(^|[\s/'\"=<>|;&(`])\.claude(/|[\s'\";&|)`]|$)|\.ygg-managed")
WORD_SPLIT = re.compile(r"[/\\=\s'\"(),;:&|<>`]+")
# Brace expansion (`{a,b}` or `{x..y}`) can spell any name, so a word using it is not resolved.
BRACE = re.compile(r"\{[^{}]*(,|\.\.)[^{}]*\}")


def brace_word(command: str) -> bool:
    """Whether any unsplit word of the command uses brace expansion (fail closed, as the lexer does)."""
    return any(BRACE.search(token) for token in _tokens(command))
INTERPRETERS = {"python", "python3", "node", "perl", "ruby", "osascript", "deno", "bun", "php", "lua", "tclsh"}


def _tokens(command: str) -> list[str]:
    """The shell-unquoted words of a command line, every simple command (wrappers unwrapped)."""
    return [token for parts in segments(command) if parts != UNRESOLVED for token in parts]


def _fold(text: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFC", str(text)).casefold()


def _resolved(value: str) -> str:
    return os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(str(value))))


def _file_values(tool_input: dict) -> list[str]:
    return [str(tool_input[f]) for f in PATH_FIELDS if isinstance(tool_input.get(f), str) and tool_input[f]]


def _secret_targets() -> set[str]:
    """The token store file and its folder, as real paths."""
    found = set()
    for path in token_store_paths():
        real = os.path.realpath(path)
        found |= {real, os.path.dirname(real)}
    return found


def secret_touched(tool: str, tool_input: dict) -> bool:
    """A tool input naming the token store or its folder: the serialised input of any tool searched for the
    paths, file tools by real path, and shell words by real path, by the store's file name (a relative name
    after a `cd`) and by glob patterns that match it. Best effort: any process of the user can read the store,
    a known gap until the credential broker ships (planned for a later release)."""
    targets = _secret_targets()
    if not targets:
        return False
    # Compared case-folded throughout: macOS volumes usually ignore case.
    folded_targets = {_fold(path) for path in targets | set(token_store_paths())}
    raw = _fold(json.dumps(tool_input, ensure_ascii=False))
    if any(path in raw for path in folded_targets):
        return True
    names = {_fold(os.path.basename(path)) for path in token_store_paths()}
    if tool == "Bash":
        import fnmatch
        for token in _tokens(str(tool_input.get("command") or "")):
            for piece in WORD_SPLIT.split(_fold(token)):
                base = os.path.basename(piece)
                if base in names or (GLOB_CHARS.search(base) and any(fnmatch.fnmatchcase(n, base) for n in names)):
                    return True
    if tool in FILE_TOOLS:
        values = _file_values(tool_input)
    elif tool == "Bash":
        values = _tokens(str(tool_input.get("command") or ""))
    else:
        return False
    for value in values:
        for piece in [value, *value.split("=", 1)[1:]]:
            if _fold(_resolved(piece)) in folded_targets or any(t in _fold(piece) for t in folded_targets):
                return True
            if _same_file(_resolved(piece), targets):
                return True
    return False


def _same_file(path: str, targets: set) -> bool:
    """Filesystem identity: a case variant or another name of the store or its folder is the same file."""
    try:
        return os.path.exists(path) and any(os.path.exists(t) and os.path.samefile(path, t) for t in targets)
    except OSError:
        return False


def _clean_is_dry(flags: list[str]) -> bool:
    """Whether a `git clean` is provably a dry run. Fail closed by allowlist: every option must be one of
    -n, --dry-run, -f, --force, -d, -x, -X, -q or --quiet (short ones may be bundled) and a dry run must be
    among them;
    anything else (exclusion patterns, negations such as --no-dry-run, `--`, paths) is not a provable dry
    run, so the command is refused."""
    dry = False
    for flag in flags:
        if flag in ("-n", "--dry-run"):
            dry = True
        elif flag in ("--quiet", "--force"):
            continue
        elif re.fullmatch(r"-[ndxXqf]+", flag):
            dry = dry or "n" in flag
        else:
            return False
    return dry


def git_clean_destructive(command: str) -> bool:
    """A `git clean` that is not a dry run, or a `git stash` that also takes ignored files: either can delete
    the guard settings and the managed marker. Git's global options are dropped the way the other git rules
    do it; an option this cannot read counts as destructive."""
    for parts in segments(command):
        if parts == UNRESOLVED or not parts or os.path.basename(parts[0]) != "git":
            continue
        normal = _git_normalised(parts)
        if normal is None:
            return True  # fail closed on a global option the hook cannot read
        rest = normal[1:]
        if not rest:
            continue
        if rest[0] == "clean" and not _clean_is_dry(rest[1:]):
            return True
        if rest[0] == "stash" and any(f in ("-a", "--all") or (f.startswith("-") and not f.startswith("--")
                                                                and "a" in f[1:]) for f in rest[1:]):
            return True
    return False


def _names_guard(value: str) -> bool:
    folded = _fold(value)
    if any(name in piece for piece in WORD_SPLIT.split(folded) for name in GUARDED_NAMES):
        return True  # any word containing a guarded name, split on separators and quotes
    for component in folded.replace("\\", "/").split("/"):
        if component in GUARDED_NAMES:
            return True
        if GLOB_CHARS.search(component):
            import fnmatch
            if any(fnmatch.fnmatchcase(name, component) for name in GUARDED_NAMES):
                return True  # a pattern that could resolve to the guard files
    return False


def guard_path_touched(tool: str, tool_input: dict) -> bool:
    """Changes to `.claude/**` or `.ygg-managed`: file tools by their path (as given and resolved), shell
    commands by their unquoted words, including glob patterns that could resolve to them. Best effort; the
    T3 settings tamper check (hash and allowlist) is the real control."""
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return any(_names_guard(v) or _names_guard(_resolved(v)) for v in _file_values(tool_input))
    if tool == "Bash":
        command = str(tool_input.get("command") or "")
        if branch_listing(command):
            return False  # git only lists branches, whatever the shell expands its pattern to; it changes nothing
        return bool(GUARD_TEXT.search(_fold(command))) or any(_names_guard(token) for token in _tokens(command))
    return False


def branch_listing(command: str) -> bool:
    """Exactly the read-only shape `git branch --list [-v] [pattern]`, one simple command (see read_git_refusal)."""
    words = _shape_words(command)
    return words is not None and words[1:3] == ["branch", "--list"] and _shape("branch", words[2:], "")


def credentials_named(command: str) -> bool:
    """The Keychain CLI, the keychain service's name anywhere, or an interpreter one-liner naming the
    Keychain (A9 treats interpreters as unresolved: these are refused as credentials)."""
    folded = _fold(command)
    try:
        from . import config
        service = str(config.get("linear_keychain_service") or "yggdrasil-linear")
    except Exception:  # noqa: BLE001
        service = "yggdrasil-linear"
    if _fold(service) in folded:
        return True
    for parts in segments(command):
        if parts == UNRESOLVED or not parts:
            continue
        head = os.path.basename(parts[0])
        if head == "security":
            return True
        if (head in INTERPRETERS or re.match(r"^python[0-9.]*$", head)) and \
                re.search(r"\bsecurity\b|keychain", folded):
            return True
    return False


UNRESOLVED_MESSAGE = (
    "Hard stop (unresolved) denied: Yggdrasil could not read this command with certainty. This is not a real "
    "hard stop and needs nobody's approval. Run one simple command per Bash call, don't put a # and quotes in "
    "the same command, and write file contents with the Write or Edit tool; for a multi-line commit message, "
    "write it to a file with the Write tool, then run git commit -F <file>. Then carry on. If a step truly "
    "can't be written that way, raise it with your PM.")


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
GIT_METADATA_MESSAGE = "Editing git's own metadata (a .git file or folder) is denied in managed sessions."


def _edit_target(tool_input: dict) -> str:
    return str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")


def _names_git_metadata(path: str) -> bool:
    # File systems may ignore case (macOS does by default): .GIT is .git.
    return any(part.casefold() == ".git" for part in Path(path).parts)


def edits_git_metadata(target: str, cwd: str, recorded: dict) -> bool:
    """Whether an edit reaches git's metadata once resolved (against the agent's folder, through symbolic
    links): a .git component in any case, or a place inside the lane's recorded git or common git directory."""
    joined = os.path.join(cwd, os.path.expanduser(target))
    resolved = os.path.realpath(joined)
    if _names_git_metadata(target) or _names_git_metadata(resolved):
        return True
    # Compared by file identity, not by name: a differently cased alias (case-insensitive file systems), a
    # symbolic link or a file that does not exist yet all resolve to the same folder.
    places = {_identity(str(place)) for place in (recorded.get("git_dir"), recorded.get("git_common_dir")) if place}
    places.discard(None)
    return any(_identity(ancestor) in places for path in (joined, resolved) for ancestor in _ancestors(path))


def _identity(path: str):
    try:
        status = os.stat(path)
    except OSError:
        return None
    return status.st_dev, status.st_ino


def _ancestors(path: str):
    current = os.path.abspath(path)
    while True:
        yield current
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


INTERNAL_MESSAGE = "internal ygg command: agents may not run it"
# ygg's internal verbs (AC-76 row 2): the hooks, the push guard, the lane check and the supervisor.
INTERNAL_VERBS = {"hook", "push-guard", "lane-check", "_supervise"}
NO_VERIFY_MESSAGE = "git commit --no-verify is refused: Yggdrasil's lane check is quick, so commit without it"


def internal_verb(command: str) -> bool:
    """A call of one of ygg's internal verbs, however it is wrapped."""
    for parts in segments(command):
        if parts and parts != UNRESOLVED and os.path.basename(parts[0]) == "ygg":
            verb = next((word for word in parts[1:] if not word.startswith("-")), "")
            if verb in INTERNAL_VERBS:
                return True
    return False


def commit_no_verify(command: str) -> bool:
    """A `git commit` that skips the lane hooks with --no-verify in any spelling: an abbreviation, -n, or a
    cluster of short flags holding n (AC-69)."""
    for parts in segments(command):
        if not parts or parts[0] != "git":
            continue
        normalised = _git_normalised(parts)
        if normalised and len(normalised) > 1 and normalised[1] == "commit" and _commit_skips_hooks(normalised[2:]):
            return True
    return False


def static_checks(tool: str, tool_input: dict) -> tuple[int, str] | None:
    """Checks that need no tracker or state: raw T3 tools, the t3 CLI, identity, git's metadata, the guard
    settings, the tracker credentials and hard stops."""
    if tool in EDIT_TOOLS and _names_git_metadata(_edit_target(tool_input)):
        return 2, GIT_METADATA_MESSAGE
    if T3_TOOL.match(tool):
        return 2, "Raw T3 tools are denied in managed sessions; use `ygg send` to message another node."
    if tool == "Bash" and brace_word(str(tool_input.get("command") or "")):
        return 2, ("Brace expansion is not resolved in managed sessions; spell out each name (the guard cannot "
                   "tell what it expands to).")
    if secret_touched(tool, tool_input):
        return 2, "Reading the Linear token store is denied in managed sessions (tracker credentials)."
    if tool == "Bash" and git_clean_destructive(str(tool_input.get("command") or "")):
        if hardstop_category(str(tool_input.get("command") or "")) == "unresolved":
            return 2, UNRESOLVED_MESSAGE  # denied either way; an unreadable command gets the guidance
        return 2, ("git clean (other than a dry run) and git stash --all are denied in managed sessions: they can "
                   "remove the guard settings (use git clean -n to list).")
    if tool == "Bash" and credentials_named(str(tool_input.get("command") or "")):
        return 2, "The Keychain is denied in managed sessions (tracker credentials)."
    if guard_path_touched(tool, tool_input):
        return 2, "Changing the guard settings (.claude/, .ygg-managed) is denied in managed sessions."
    if tool == "Bash":
        if internal_verb(str(tool_input.get("command") or "")):
            return 2, INTERNAL_MESSAGE
        if commit_no_verify(str(tool_input.get("command") or "")):
            return 2, NO_VERIFY_MESSAGE
        category = hardstop_category(str(tool_input.get("command") or ""))
        if category == "t3":
            return 2, "The t3 CLI is denied in managed sessions; use `ygg send` to message another node."
        if category == "identity":
            return 2, "Setting or unsetting YGG_ variables is denied in managed sessions (identity)."
        if category == "credentials":
            return 2, "The Keychain CLI is denied in managed sessions (tracker credentials)."
        if category == "unresolved":
            return 2, UNRESOLVED_MESSAGE
        if category:
            return 2, "Hard stop (" + category + ") denied: ask the user through your PM."
        command = str(tool_input.get("command") or "")
        if any(parts and parts[0] == "git" and any(_file_option(word) for word in parts[1:])
               for parts in segments(command)) or (re.search(r"--no-i|--output", command) and
                                                     any(parts and parts[0] == "git" for parts in segments(command))):
            return 2, OUTSIDE_MESSAGE
        if sets_git_location(command):
            return 2, ("Setting GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE or another variable that points git at "
                       "another repository is denied in managed sessions.")
    return None


# ---------------------------------------------------------------- unread mail (AC-10, AC-51 to AC-53)

UNREADABLE_NOTICE = "Your inbox can't be read right now. Don't commit until it can."


def unread_counts(node: str, activation: str | None) -> tuple[int, int] | None:
    """(unread, urgent) for the node's current activation, from the local inbox store only: messages not acked
    and not in the work set the activation was given at reservation. None when the store can't be read."""
    path = state.inbox_path(node)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        try:
            path.parent.stat()
        except FileNotFoundError:
            return 0, 0
        except OSError:
            return None
        if path.parent.is_dir() and not os.access(path.parent, os.R_OK | os.X_OK):
            return None
        return 0, 0
    except OSError:
        return None
    if not text.strip():
        return None  # an empty store is not an empty inbox (A7 I13): unavailable until it is repaired
    try:
        items = json.loads(text)
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    given: set = set()
    if activation:
        try:
            from . import launcher
            given = set(((launcher.receipt(activation) or {}).get("consumed") or {}).get("messages") or [])
        except (util.YggError, OSError, ValueError):
            given = set()
    count = urgent = 0
    for item in items:
        if not isinstance(item, dict) or item.get("acked_at") or item.get("msg_id") in given:
            continue
        count += 1
        urgent += 1 if item.get("urgent") else 0
    return count, urgent


def unread_notice(node: str, counts: tuple[int, int] | None) -> str | None:
    """The fixed notice template: integers and the node id only, never message text (AC-52)."""
    if counts is None:
        return UNREADABLE_NOTICE
    count, urgent = counts
    if not count:
        return None
    text = str(count) + (" unread message" if count == 1 else " unread messages")
    if urgent:
        text += " (" + str(urgent) + " urgent)"
    return text + ": run ygg inbox " + node + " now."


def outgoing(command: str) -> bool:
    """A commit, a push or a `ygg wp ready`, however it is spelled or wrapped (AC-51)."""
    for parts in segments(command):
        if not parts or parts == UNRESOLVED:
            continue
        if parts[0] == "git":
            normalised = _git_normalised(parts)
            if normalised is None or (len(normalised) > 1 and normalised[1] in ("commit", "push", "merge")):
                return True
        elif os.path.basename(parts[0]) == "ygg":
            words = [word for word in parts[1:] if not word.startswith("-")]
            if words[:2] == ["wp", "ready"] or words[:2] == ["wp", "complete"]:
                return True
    return False


def unread_refusal(node: str, counts: tuple[int, int] | None) -> str | None:
    if counts is None:
        return "inbox unavailable: Yggdrasil can't read your inbox, so commits, pushes and ready wait until it can."
    if counts[0]:
        return "read your inbox first (" + str(counts[0]) + " unread): ygg inbox " + node
    return None


# ---------------------------------------------------------------- the freeze, at the guard (AC-15 row 1)

def _commit_pathspecs(arguments: list[str]) -> list[str]:
    """The pathspecs a `git commit` names: after `--`, or its words that aren't options or option values."""
    found, words = [], list(arguments)
    while words:
        word = words.pop(0)
        if word == "--":
            found.extend(words)
            break
        if word.startswith("--"):
            if word.split("=", 1)[0] in COMMIT_VALUED_LONG and "=" not in word and words:
                words.pop(0)
            continue
        if word.startswith("-") and len(word) > 1:
            for index, letter in enumerate(word[1:]):
                if letter in COMMIT_VALUED_SHORT:
                    if index == len(word) - 2 and words:
                        words.pop(0)
                    break
                if letter in COMMIT_OPTIONAL_SHORT:
                    break
            continue
        found.append(word)
    return found


def frozen_commit_refusal(command: str, node: str, info: dict, cwd: str) -> str | None:
    """A `git commit` naming a file of an accepted package (pathspecs only; the lane hooks check the index)."""
    named = []
    for parts in segments(command):
        if not parts or parts[0] != "git":
            continue
        normalised = _git_normalised(parts)
        if normalised and len(normalised) > 1 and normalised[1] == "commit":
            named.extend(_commit_pathspecs(normalised[2:]))
    if not named or not info.get("project"):
        return None
    from . import lanecheck
    try:
        frozen = lanecheck.frozen_files(str(info["project"]), cwd)
    except (lanecheck.CheckError, util.YggError, OSError):
        return None
    top = util.trusted_git("-C", cwd, "rev-parse", "--show-toplevel").stdout.strip() or cwd
    for name in named:
        relative = os.path.relpath(os.path.realpath(os.path.join(cwd, name)), os.path.realpath(top))
        for path, package in frozen.items():
            if relative == path or path.startswith(relative.rstrip("/") + "/") or relative == ".":
                return lanecheck.frozen_message(path, package)
    return None


# ---------------------------------------------------------------- the in-flight record (the busy module reads it)

def inflight_dir(activation: str) -> Path:
    return state.root() / "inflight" / activation


def record_inflight(node: str, activation: str | None, payload: dict, command: str) -> None:
    """One record per allowed tool call, keyed by (activation, tool_use_id): {node, activation, tool_use_id,
    tool_name, command (redacted), started_at}. Lane 2's busy reads it with `inflight` and clears it with
    `clear_inflight` on the post event. Best effort: a call is never refused for it."""
    tool_use_id = str(payload.get("tool_use_id") or "")
    if not activation or not tool_use_id or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tool_use_id):
        return
    try:
        util.atomic_write(inflight_dir(activation) / (tool_use_id + ".json"), json.dumps({
            "node": node, "activation": activation, "tool_use_id": tool_use_id,
            "tool_name": str(payload.get("tool_name") or ""), "command": util.redact(command)[:500],
            "started_at": util.iso()}))
    except OSError:
        pass


def inflight(activation: str) -> list[dict]:
    """The activation's in-flight tool calls (allowed by the guard, no post event yet)."""
    found = []
    folder = inflight_dir(activation)
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            data = util.read_json(path, None)
            if isinstance(data, dict):
                found.append(data)
    return found


def clear_inflight(activation: str, tool_use_id: str) -> None:
    if not activation or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", str(tool_use_id or "")):
        return
    try:
        (inflight_dir(activation) / (str(tool_use_id) + ".json")).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- the guard

READ_ONLY_ROLES = ("su", "reviewer")


def pretool(payload_text: str) -> tuple[int, str]:
    code, message, _ = pretool_result(payload_text)
    return code, message


def _git_root(activation: str | None) -> tuple[dict, str | None]:
    try:
        from . import launcher
        recorded = launcher.receipt(str(activation or "")) or {}
        return recorded, recorded.get("cwd")
    except (util.YggError, OSError, ValueError):
        return {}, None


def pretool_result(payload_text: str) -> tuple[int, str, str | None]:
    """(exit code, refusal on stderr, notice for additionalContext)."""
    node = util.current_node()
    if node is None:
        if state.managed_session():
            # Inside a managed worktree, or carrying an activation, but without a node identity: the
            # session lost its environment, so it fails closed rather than acting as the user (A5.1).
            return 2, "This is a Yggdrasil-managed session without its node identity; stop work now.", None
        return 0, "", None
    try:
        payload = json.loads(payload_text or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        tool = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            raise ValueError("tool_input is not an object")
    except ValueError:
        return 2, "Malformed hook payload; stop work now.", None
    activation = util.env("YGG_ACTIVATION")
    command = str(tool_input.get("command") or "") if tool == "Bash" else ""
    if command and internal_verb(command):
        return 2, INTERNAL_MESSAGE, None
    if command and (" git" in " " + command or "git" in command):
        # Senior users and reviewers may run no git but the read shapes (AC-21): checked before the other
        # rules, so every other git call they make gets the read-only message.
        try:
            role = (state.all_nodes().get(node) or {}).get("role")
        except (util.YggError, OSError, ValueError, KeyError, TypeError):
            return 2, UNAVAILABLE, None
        if role in READ_ONLY_ROLES and any(parts and parts[0] == "git" for parts in segments(command)):
            recorded, root = _git_root(activation)
            if not root:
                return 2, UNAVAILABLE, None
            shell_cwd = str(payload.get("cwd") or root)
            refusal = read_git_refusal(command, shell_cwd, str(root), every_git=True)
            if refusal:
                return 2, refusal, None
    denied = static_checks(tool, tool_input)
    if denied:
        return denied[0], denied[1], None
    if tool in EDIT_TOOLS and _edit_target(tool_input):
        recorded, _ = _git_root(activation)
        if edits_git_metadata(_edit_target(tool_input), str(payload.get("cwd") or recorded.get("cwd") or os.getcwd()),
                              recorded):
            return 2, GIT_METADATA_MESSAGE, None
    if command and reads_with_git(command):
        # Read-only git runs in the agent's own worktree, as its launch recorded it; unknown fails closed.
        recorded, root = _git_root(activation)
        if not root:
            return 2, UNAVAILABLE, None
        shell_cwd = str(payload.get("cwd") or root)
        refusal = read_git_refusal(command, shell_cwd, str(root))
        if refusal:
            return 2, refusal, None
        if not same_repository(shell_cwd, recorded, str(root)):
            return 2, REPOSITORY_MESSAGE, None
    try:
        with_heartbeat(node)
        nodes = state.all_nodes()
        info = nodes.get(node)
        if info is None:
            # A managed caller the tracker does not know cannot be checked, so it is refused.
            return 2, UNAVAILABLE, None
        if state.paused(info.get("programme")):
            return 2, "Yggdrasil is paused for this scope; stop work now.", None
        from . import launcher
        if launcher.stopped({"id": node, "programme": info.get("programme")}):
            return 2, "Yggdrasil emergency stop for this node; stop work now.", None
        if info.get("role") == "reviewer":
            # The reviewer rule set (A11 AC-94 row 1, AC-32 row 3): its text is the refusal.
            from . import permissions
            reviewer_refusal = getattr(permissions, "reviewer_refusal", None)
            refusal = reviewer_refusal(tool, tool_input, activation) if reviewer_refusal else None
            if refusal:
                return 2, str(refusal), None
        if info.get("role") in ("tl", "su", "worker"):
            from . import decisions
            blocked = decisions.blocking(node, nodes)
            if blocked:
                return 2, "Your branch is blocked by " + ", ".join(blocked) + " until the user answers; stop work now.", None
        if info.get("role") == "su":
            if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                return 2, "Senior users test read-only; record findings with ygg instead of editing files.", None
            if tool == "Bash" and not senior_git_allowed(command):
                return 2, "Senior users test read-only; only read-only git subcommands are allowed.", None
    except (util.YggError, OSError, ValueError, KeyError, TypeError):
        return 2, UNAVAILABLE, None
    counts = unread_counts(node, activation)
    if command and outgoing(command):
        refusal = unread_refusal(node, counts)
        if refusal:
            return 2, refusal, None
    if command:
        refusal = frozen_commit_refusal(command, node, info, str(payload.get("cwd") or os.getcwd()))
        if refusal:
            return 2, refusal, None
        try:
            from . import retrycap
            refusal = retrycap.check(node, activation, command, str(payload.get("cwd") or ""), info)
        except (util.YggError, OSError, ValueError, KeyError, TypeError):
            refusal = None
        if refusal:
            return 2, refusal, None
    record_inflight(node, activation, payload, command)
    return 0, "", unread_notice(node, counts)


def with_heartbeat(node: str) -> None:
    try:
        util.atomic_write(state.root() / "heartbeats" / node, util.iso() + "\n")
    except OSError:
        pass


def main_pretool() -> int:
    try:
        code, message, notice = pretool_result(sys.stdin.read())
    except Exception:  # fail closed in managed sessions
        code, message, notice = ((0, "", None) if util.current_node() is None else (2, UNAVAILABLE, None))
    if message:
        print(message, file=sys.stderr)
    if code == 0 and notice:
        # Plain stdout never reaches the agent; the JSON additionalContext does (smoke check, 18 Nov).
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": notice}}))
    return code
