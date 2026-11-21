"""T3 launcher on the lifecycle facts (A5.1 to A5.4; rulings 72, 73 and 105 to 108). Every `t3` call lives here.

- Selection (A5.3): a Claude node runs through T3 only when the configured binary (`YGG_T3_BIN` or the
  `t3_bin` setting; PATH is never searched) reports a CLI and a server version that are both supported and
  equal. Otherwise the headless launcher runs and the fallback is logged.
- One activation per launch (ruling 107): the node's first activation starts its thread (`thread start`,
  titled `<node> <activation>`); each later one sends into the same thread (`thread send`). A node has at
  most one unsettled activation; work that arrives while its run is alive waits as owed work.
- Messages (ruling 105, Batch B B30): the message is the marker line `[ygg <activation> <msg-id>]` plus a
  fixed notice naming a private prompt file (mode 0600 in the activation's launch folder). No prompt, brief
  or message body is ever on a command line.
- Gate and final check: the `spawned` fact is committed under the journal lock (the reservation must be the
  node's newest and unsettled), then the stop and pause markers and the revocation record are read under the
  creation lock, before the T3 call. A refusal there is positive no-agent evidence (aborted).
- Evidence (ruling 106): from `thread show --json`. The activation's run is the run of its marker message;
  alive is `starting` or `running` on that run; `completed` and `failed` are its exit (code 0 and 1, the
  failure classed by the capacity classifier on the last assistant message); exit 1 with "no longer
  available" is gone (dead with no exit record: uncertain until `ygg node release`, I2); anything else is
  unknown and held. A call whose outcome is unknown leaves the activation uncertain until a tick finds its
  thread (`thread list` by title) or its marker (`thread show`); a readable answer without either retries
  the call once inside the same activation (AC-61, AC-18).
- Stop (ruling 108): T3 sessions are not signalled. A stopped node's next tool call is denied by the guard
  hook and nothing more is sent to it; a reply already being written finishes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config, state, util
from .util import YggError

CALL_TIMEOUT = 60
# A T3 thread id as Yggdrasil accepts it: never a leading '-' (no option injection).
THREAD_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z:%._-]{0,127}$")
ALIVE = ("starting", "running")
ENDED = {"completed": 0, "failed": 1}
GONE = re.compile(r"^Thread \S+ is no longer available\.\s*$")
MARKER_RE = re.compile(r"^\[ygg (act-[0-9]{4,9}) ([A-Za-z0-9-]+)\]$")
HELD = 125
# Variables a T3 call never carries: tracker credentials and the suite's Linear fake.
CALL_ENV_DENIED = re.compile(r"^(LINEAR_|YGG_LINEAR_)")


# ---------------------------------------------------------------- binary and versions (A5.3)

def binary() -> str | None:
    explicit = util.env("YGG_T3_BIN") or config.get("t3_bin") or (config.profile() or {}).get("t3_bin")
    if explicit and os.access(str(explicit), os.X_OK):
        return str(explicit)
    return None


def _exec(args: tuple, timeout: float = CALL_TIMEOUT) -> subprocess.CompletedProcess:
    """The only place a `t3` process is created."""
    path = binary()
    if not path:
        return subprocess.CompletedProcess(args, 127, "", "t3 binary missing")
    environment = {k: v for k, v in os.environ.items() if not CALL_ENV_DENIED.match(k)}
    try:
        return subprocess.run([path, *map(str, args)], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=environment)
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(args, 124, "", "request timed out: " + str(error))
    except OSError as error:
        return subprocess.CompletedProcess(args, 124, "", str(error))


def _spawn(args: tuple):
    """Start a `t3` call without waiting for it (used under the creation lock: only the process start is
    covered there, never the wait for T3's answer)."""
    path = binary()
    if not path:
        return subprocess.CompletedProcess(args, 127, "", "t3 binary missing")
    environment = {k: v for k, v in os.environ.items() if not CALL_ENV_DENIED.match(k)}
    try:
        return subprocess.Popen([path, *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, env=environment, close_fds=True)
    except OSError as error:
        return subprocess.CompletedProcess(args, 124, "", str(error))


# A T3 call that may create something (a thread start or send) runs behind this gate. It is started first,
# its identity (pid and start time) is recorded in the safety folder, and only then is it released (a token
# naming its pid): it then becomes the `t3` process itself (`exec` keeps pid and start time). A gate whose
# dispatcher dies before the release never calls T3 and exits after GATE_SECONDS. So a call that could still
# reach T3 is always one recovery can identify and wait for.
GATE_SECONDS = 30
GATE = (
    "import os, sys, time\n"
    "token, binary = sys.argv[1], sys.argv[2]\n"
    "deadline = time.monotonic() + float(sys.argv[3])\n"
    "while time.monotonic() < deadline:\n"
    "    try:\n"
    "        with open(token) as handle:\n"
    "            if handle.read().strip() == str(os.getpid()):\n"
    "                os.execv(binary, [binary] + sys.argv[4:])\n"
    "    except OSError:\n"
    "        pass\n"
    "    time.sleep(0.01)\n"
    "sys.exit(125)\n")


def call_path(activation: str) -> Path:
    from . import safety
    return safety.root() / "t3-calls" / (activation + ".json")


def call_record(activation: str) -> dict:
    try:
        data = util.read_json(call_path(activation), {}) or {}
    except util.YggError:
        return {"unreadable": True}
    return data if isinstance(data, dict) and data.get("activation") == activation else {}


def call_in_flight(activation: str) -> bool:
    """Whether a recorded T3 call of this activation may still reach T3 (alive, or not provably dead)."""
    from . import lifecycle
    record = call_record(activation)
    if record.get("unreadable"):
        return True
    if not record.get("pid"):
        return False
    return lifecycle.evidence(record.get("pid"), record.get("started_at")) != "dead"


def _spawn_gated(activation: str, args: tuple):
    """`_spawn` for a call that may create something: under the creation lock (local work only), start the
    gate, record its identity durably, then release it."""
    from . import lifecycle
    path = binary()
    if not path:
        return subprocess.CompletedProcess(args, 127, "", "t3 binary missing")
    previous = call_record(activation)
    attempt = int(previous.get("attempt") or 0) + 1
    token = call_path(activation).parent / (activation + "." + str(attempt) + ".go")
    environment = {k: v for k, v in os.environ.items() if not CALL_ENV_DENIED.match(k)}
    try:
        util.ensure_dir(token.parent)
        started = subprocess.Popen([sys.executable, "-I", "-c", GATE, str(token), path, str(GATE_SECONDS),
                                    *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, text=True, env=environment, close_fds=True)
    except OSError as error:
        return subprocess.CompletedProcess(args, 124, "", str(error))
    started_at = util.process_start(started.pid)
    if not started_at:
        started.kill()  # never released, so it never reached T3
        started.wait()
        return subprocess.CompletedProcess(args, 124, "", "the t3 call could not be identified; not sent")
    lifecycle.write_local(call_path(activation), {"activation": activation, "attempt": attempt,
                                                  "pid": started.pid, "started_at": started_at,
                                                  "at": util.iso()})
    util.atomic_write(token, str(started.pid) + "\n")
    return started


def _finish(started, timeout: float = CALL_TIMEOUT) -> subprocess.CompletedProcess:
    if isinstance(started, subprocess.CompletedProcess):
        return started
    try:
        out, err = started.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        started.kill()
        out, err = started.communicate()
        return subprocess.CompletedProcess(started.args, 124, out or "", (err or "") + "\nrequest timed out")
    return subprocess.CompletedProcess(started.args, started.returncode, out or "", err or "")


def versions() -> tuple[str | None, str | None]:
    cli = re.search(r"t3 v([0-9][0-9A-Za-z.\-]*)", _exec(("--version",), timeout=15).stdout or "")
    server = re.search(r"t3@([0-9][0-9A-Za-z.\-]*)", _exec(("service", "status"), timeout=15).stdout or "")
    if server:
        return (cli[1] if cli else None), server[1]
    # The 0.1.9 CLI prints no `t3@<version>` token when its own version matches the service binary; T3's
    # service state file names the active version (A5.3).
    home = Path(util.env("T3CODE_HOME") or Path.home() / ".t3")
    active = (util.read_json(home / "runtime" / "service-state.json", {}) or {}).get("activeVersion")
    return (cli[1] if cli else None), (str(active) if active else None)


def versions_problem() -> str | None:
    """Why the T3 CLI and server may not be used now, or None when both are supported and equal."""
    if binary() is None:
        return "t3 binary missing"
    cli_version, server_version = versions()
    supported_cli = [str(v) for v in config.get("t3_supported_cli_versions") or []]
    supported_server = [str(v) for v in config.get("t3_supported_server_versions") or []]
    if cli_version is None or server_version is None:
        return "could not read the T3 versions (CLI " + str(cli_version) + ", server " + str(server_version) + ")"
    if cli_version not in supported_cli or server_version not in supported_server:
        return ("unsupported T3 version (CLI " + cli_version + ", server " + server_version + "; supported CLI " +
                ",".join(supported_cli) + ", server " + ",".join(supported_server) + ")")
    if cli_version != server_version:
        return "T3 CLI " + cli_version + " does not equal server " + server_version
    return None


_HOST: dict = {}
HOST_CHECK_SECONDS = 60


def host_usable() -> bool:
    """Whether calls to existing T3 threads may go out now, cached for a minute per state directory. A
    mismatch holds them (their evidence is unknown) and raises one drift alert per problem."""
    key = str(state.root())
    cached = _HOST.get(key)
    if cached is None or time.monotonic() - cached[1] > HOST_CHECK_SECONDS:
        problem = versions_problem()
        cached = (problem is None, time.monotonic())
        _HOST[key] = cached
        if problem:
            drift_alert(problem)
    return cached[0]


def drift_alert(problem: str) -> None:
    path = state.root() / "t3-version-check.json"
    last = util.read_json(path, {}) or {}
    if last.get("problem") != problem:
        state.alert("ygg-t3-drift", "T3 version drift: " + problem + "; T3 calls are held", problem=problem)
    util.write_json(path, {"checked_at": util.iso(), "problem": problem})


def run(*args, timeout: float = CALL_TIMEOUT) -> subprocess.CompletedProcess:
    """Every call to an existing T3 thread or project goes through the version gate first."""
    if not host_usable():
        return subprocess.CompletedProcess(args, HELD, "", "t3 call held: T3 versions are not usable")
    return _exec(args, timeout=timeout)


def log_fallback(message: str) -> None:
    line = util.iso() + " t3 fallback to headless: " + message
    util.append_line(state.root() / "launcher.jsonl", {"ts": util.iso(), "event": "fallback", "message": line})
    print(line, file=sys.stderr)


def explicit_headless() -> bool:
    value, source = config.explain_all().get("launcher", ("headless", "core"))
    return value == "headless" and not (source == "core" or str(source).startswith("preset:"))


def would_use(cli: str) -> bool:
    """The routing choose() makes, without recording or logging anything: for status reads."""
    return cli == "claude-code" and not explicit_headless() and binary() is not None and versions_problem() is None


def choose(cli: str) -> bool:
    """Whether this launch goes through T3 (logging the reason when T3 exists but is not used)."""
    if cli != "claude-code" or explicit_headless() or binary() is None:
        return False
    problem = versions_problem()
    _HOST[str(state.root())] = (problem is None, time.monotonic())
    if problem:
        log_fallback(problem)
        return False
    return True


# ---------------------------------------------------------------- the lane worktree (A5.1, B31)

def _quote(value) -> str:
    import shlex
    return shlex.quote(str(value))


MANAGED_MARKER = ".ygg-managed"


def _git(cwd: Path, *args) -> subprocess.CompletedProcess:
    """A git call that runs no repository hooks or fsmonitor (the worktree is agent-writable)."""
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(cwd),
                           *args], capture_output=True, text=True)


def _tracked(cwd: Path, relative: str) -> bool:
    return _git(cwd, "ls-files", "--error-unmatch", "--", relative).returncode == 0


def _common_dir(home) -> Path | None:
    """The git folder of the programme's home repository, from the programme record (never from the
    agent-writable worktree's own `.git` file), and only when it lies inside that repository."""
    if not home:
        return None
    repo = Path(os.path.realpath(str(home)))
    candidate = repo / ".git"
    if not candidate.is_dir() or candidate.is_symlink():
        return None
    real = Path(os.path.realpath(candidate))
    return real if real == repo / ".git" else None


def _exclude_from_git(home, relative: str) -> None:
    common = _common_dir(home)
    if common is None:
        return
    info = common / "info"
    if os.path.lexists(info) and (os.path.islink(info) or not info.is_dir()):
        return  # never written through a link
    if not os.path.lexists(info):
        os.mkdir(info, 0o755)
    if os.path.realpath(info) != str(common / "info"):
        return
    exclude = info / "exclude"
    if os.path.islink(exclude) or (os.path.lexists(exclude) and not exclude.is_file()):
        return
    text = util.read_text(exclude)
    if "/" + relative not in text.splitlines():
        util.atomic_write(exclude, text + ("" if text.endswith("\n") or not text else "\n") + "/" + relative + "\n")


def session_environment(node: dict, activation: str, pinned: dict) -> dict:
    """The explicit, non-secret environment a T3 session gets through its settings file (B31): its identity
    and the roots the launching process pinned, nothing inherited."""
    environment = {"YGG_NODE": node["id"], "YGG_ACTIVATION": activation,
                   "YGG_PROGRAMME": str(node.get("programme") or ""), "YGG_PROJECT": str(node.get("project") or "")}
    for key in ("YGG_STATE_ROOT", "YGG_FILES_ROOT", "YGG_AGREEMENT_ROOT", "YGG_PROFILE", "YGG_CONFIG"):
        if pinned.get(key):
            environment[key] = str(pinned[key])
    if util.env("YGG_TRACKER") in ("files", "linear"):
        environment["YGG_TRACKER"] = str(util.env("YGG_TRACKER"))
    return environment


SETTINGS = ".claude/settings.json"


def _refuse_injection(detail: str) -> YggError:
    return util.refuse("launch precondition: hook injection failed: " + detail)


def _folded(name: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFC", name).casefold()


def _claude_extras(claude: Path) -> list[str]:
    """Entries of `.claude/` other than one `settings.json` (compared after NFC and casefolding, since the
    default macOS file system ignores case): any of them could override the guard settings. A folder that
    cannot be listed counts as holding something unknown."""
    try:
        entries = sorted(os.listdir(claude)) if claude.is_dir() else []
    except OSError:
        return ["(unlistable folder)"]
    own = [e for e in entries if _folded(e) == "settings.json"]
    extras = [e for e in entries if _folded(e) != "settings.json"]
    return extras + (own[1:] if len(own) > 1 else [])


def _safe_claude_dir(cwd: Path) -> Path:
    """`<worktree>/.claude` as a real folder inside the worktree (never a link), created if missing; the
    settings file and the marker must not be links either."""
    import stat
    claude = cwd / ".claude"
    if os.path.lexists(claude):
        info = os.lstat(claude)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _refuse_injection(str(claude) + " is not a plain folder")
    else:
        os.mkdir(claude, 0o700)
    if os.path.realpath(claude) != os.path.join(os.path.realpath(cwd), ".claude"):
        raise _refuse_injection(str(claude) + " resolves outside the worktree")
    extras = _claude_extras(claude)
    if extras:
        # Only Yggdrasil's own settings file may be there: anything else could override the guard hook.
        raise _refuse_injection(str(claude) + " holds " + ", ".join(extras) + "; a T3 worktree's .claude folder "
                                "may hold only Yggdrasil's settings.json")
    for path in (cwd / SETTINGS, cwd / MANAGED_MARKER):
        if os.path.islink(path):
            raise _refuse_injection(str(path) + " is a link")
    return claude


def prepare_worktree(cwd: Path, node: dict, activation: str, pinned: dict, home=None,
                     permissions: dict | None = None) -> str:
    """`.claude/settings.json` registering the guard hook (hooks not disabled), the session environment and the
    agreed agent permission rules (the same block a headless launch gets), and the `.ygg-managed` marker, both
    kept out of Git; a settings file the repository tracks is never replaced (B31). Returns the settings
    content's hash, kept on the receipt for the tamper check."""
    from . import launcher
    from .launcher import ygg_executable
    cwd = Path(cwd)
    if _tracked(cwd, SETTINGS):
        raise _refuse_injection(SETTINGS + " is tracked in " + str(cwd) +
                                "; Yggdrasil does not replace a repository's own settings file")
    content = launcher.settings_content(_quote(ygg_executable()), permissions)
    settings = {"disableAllHooks": content.pop("disableAllHooks"),
                "env": session_environment(node, activation, pinned), **content}
    text = json.dumps(settings, indent=2) + "\n"
    _check_baseline(cwd, node["id"], activation)
    try:
        _safe_claude_dir(cwd)
        util.atomic_write(cwd / SETTINGS, text)
        util.atomic_write(cwd / MANAGED_MARKER, node["id"] + "\n")
    except OSError as error:
        raise _refuse_injection("cannot write " + str(cwd / SETTINGS) + ": " + str(error))
    _exclude_from_git(home, SETTINGS)
    _exclude_from_git(home, MANAGED_MARKER)
    digest = _digest(text)
    with util.locked():
        launcher.update_receipt(activation, t3_settings_sha=digest)
        baselines = util.read_json(_baselines_path(), {}) or {}
        baselines[os.path.realpath(str(cwd))] = digest
        util.write_json(_baselines_path(), baselines)
    return digest


def _baselines_path() -> Path:
    return state.root() / "t3-settings.json"


def _check_baseline(cwd: Path, node_id: str, activation: str) -> None:
    """Before the settings file is written again: it must still be what Yggdrasil wrote last time (a
    session may have rewritten it between activations). A changed file is tamper and refuses the launch."""
    expected = (util.read_json(_baselines_path(), {}) or {}).get(os.path.realpath(str(cwd)))
    if not expected:
        return  # never written here before (or the state root was wiped): nothing to compare with
    problem = settings_problem(cwd, expected)
    if problem:
        tampered(node_id, activation, problem)
        raise _refuse_injection("the guard settings of " + str(cwd) + " " + problem.split(" ", 1)[-1] +
                                " since the last launch; " + node_id + " is stopped")


def _digest(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def settings_problem(worktree, expected: str | None) -> str | None:
    """Why the worktree's guard settings are not as Yggdrasil wrote them, or None: the settings file must
    be unchanged (not a link) and no other settings file may override it."""
    if not worktree or not expected:
        return "no recorded guard settings"
    cwd = Path(worktree)
    claude = cwd / ".claude"
    if os.path.islink(claude) or not claude.is_dir():
        return ".claude is missing or a link"
    path = cwd / SETTINGS
    if os.path.islink(path) or not path.is_file():
        return SETTINGS + " is missing or a link"
    try:
        current = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return SETTINGS + " is unreadable"
    if _digest(current) != expected:
        return SETTINGS + " was changed"
    extras = _claude_extras(claude)
    if extras:
        return "settings override " + ", ".join(extras) + " present"
    return None


def tampered(node_id: str, activation: str, problem: str) -> None:
    """A T3 worktree whose guard settings changed: the node is marked stopped (a local stop marker, so its
    next tool call is denied and nothing more is sent) and one alert is raised for the activation."""
    from . import launcher, safety
    safety.publish_marker("stop", node_id)
    data = launcher.receipt(activation)
    if not data.get("t3_tamper_alerted"):
        state.alert("ygg-t3-settings-tampered", "The guard settings of " + node_id + "'s T3 worktree " + problem +
                    "; " + node_id + " is stopped until you check the worktree and run ygg resume " + node_id,
                    node=node_id, activation=activation, problem=problem)
        with util.locked():
            launcher.update_receipt(activation, t3_tamper_alerted=True)


def _projects_path() -> Path:
    return state.root() / "t3-projects.json"


def _journalled_project(cwd: Path) -> str | None:
    """The T3 project id last journalled for this worktree (durable): from a registration or an earlier
    activation's spawned fact, newest in journal order, never a removed one."""
    from . import journal
    target = os.path.realpath(str(cwd))
    for programme in journal.all_programmes():
        removed = _removed_projects(programme)
        trusted, _ = journal.trusted_entries(programme)
        found = None
        for entry in trusted:
            if entry.get("cleared_at"):
                continue
            if entry.get("kind") == "t3-project":
                ident, path = entry.get("project_id"), entry.get("worktree")
            elif entry.get("kind") == "activation" and entry.get("phase") == "spawned" and entry.get("launcher") == "t3":
                ident, path = entry.get("t3_project_id"), entry.get("t3_worktree")
            else:
                continue
            if ident and str(ident) not in removed and os.path.realpath(str(path or "")) == target:
                found = str(ident)
        if found:
            return found
    return None


def _journal_registration(cwd: Path, project_id: str, node: dict | None) -> None:
    from . import journal, tracker
    programme, project = (node or {}).get("programme"), (node or {}).get("project")
    if not programme or not project:
        for record in tracker.programmes():
            try:
                slot = _slot(str(tracker.programme_home(record)), str(cwd))
            except (YggError, KeyError, TypeError):
                slot = None
            if slot:
                programme, project = record["id"], slot[0]
                break
    if programme and project and util.ID_RE.match(str(project)):
        journal.add(programme, "t3-project", project=str(project), worktree=str(cwd), project_id=project_id)


def ensure_project(cwd: Path, title: str, node: dict | None = None) -> str:
    """Register the worktree as its own T3 project once (A5.1); the id is kept locally and on every spawned
    fact, so a wiped state root reuses it instead of registering the worktree again."""
    key = os.path.realpath(str(cwd))
    from . import journal
    for programme in journal.all_programmes():
        pending = _unresolved_intents(programme, key)
        if pending:
            raise util.refuse("launch precondition: the removal of T3 project " + ", ".join(sorted(pending)) +
                              " of " + key + " is not settled")
    known = util.read_json(_projects_path(), {}) or {}
    removed = _removed_projects()
    if key in known and str(known[key].get("id")) not in removed:
        return known[key]["id"]
    journalled = _journalled_project(cwd)
    if journalled:
        with util.locked():
            known = util.read_json(_projects_path(), {}) or {}
            # Restored from the journal, not from `project add`: a close confirms it with T3 before removing it.
            known[key] = {"id": journalled, "title": title, "path": str(cwd), "confirmed": False}
            util.write_json(_projects_path(), known)
        return journalled
    result = run("project", "add", "--title", title, "--", str(cwd))
    match = re.search(r"Added project (\S+) ", result.stdout or "")
    if result.returncode != 0 or not match:
        raise util.refuse("launch precondition: t3 project add failed for " + str(cwd) + ": " +
                          (result.stderr or result.stdout).strip()[:200])
    _journal_registration(cwd, match[1], node)  # durable before the local map: a wipe never hides it
    with util.locked():
        known = util.read_json(_projects_path(), {}) or {}
        known[key] = {"id": match[1], "title": title, "path": str(cwd)}
        util.write_json(_projects_path(), known)
    return match[1]


# ---------------------------------------------------------------- closing a lane or a project (A5.1)
#
# Closing removes the T3 project (`t3 project remove --force <id>`), then the worktree (`git worktree
# remove`). What is owed is derived on every pass from durable facts, so no crash or wipe can lose it: a
# worktree with a T3 footprint (the local project map, or the journalled spawned facts) is owed a close when
# its project is closed, or, for a lane worktree, when the lane has work packages and every one is merged.
# The journal records the removal intent and the removal of each project id (never reused). A close runs
# under a local claim taken under the journal lock, which a launch reservation in that worktree checks, so
# no activation can start in a worktree while it is being removed.

PROJECT_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._:-]{0,127}$")


def _removal_records(programme=None) -> list[dict]:
    from . import journal
    found = []
    for name in ([programme] if programme is not None else journal.all_programmes()):
        trusted, _ = journal.trusted_entries(name)
        found += [e for e in trusted if e.get("kind") == "t3-close"]
    return found


def _registrations(programme) -> list[dict]:
    from . import journal
    trusted, _ = journal.trusted_entries(programme)
    return [e for e in trusted if e.get("kind") == "t3-project" and e.get("worktree") and
            PROJECT_ID_RE.match(str(e.get("project_id") or ""))]


def _removed_projects(programme=None) -> set:
    """Project ids never managed again: removed, or kept by the user (`ygg t3 settle --as kept`)."""
    return {str(e.get("removed") or e.get("kept")) for e in _removal_records(programme)
            if e.get("removed") or e.get("kept")}


def _kept_worktrees(programme) -> set:
    """Worktrees the user kept with their T3 project: no close touches them again."""
    return {os.path.realpath(str(e["worktree"])) for e in _removal_records(programme)
            if e.get("kept") and e.get("worktree")}


def _number(key: str) -> int:
    try:
        return int(str(key).split("-")[1])
    except (IndexError, ValueError):
        return 0


def _slot(home: str, path: str) -> tuple[str, str | None] | None:
    """(project, lane or None) of a worktree path Yggdrasil lays out in the programme's home, else None."""
    base = os.path.realpath(os.path.join(str(home), ".ygg-worktrees"))
    real = os.path.realpath(str(path))
    if not real.startswith(base + os.sep):
        return None
    parts = real[len(base) + 1:].split(os.sep)
    if len(parts) < 2 or not util.ID_RE.match(parts[0]):
        return None
    if parts[1:] == ["_integration"] or (len(parts) == 2 and parts[1].startswith("_test-") and
                                        util.ID_RE.match(parts[1][6:])):
        return parts[0], None
    if len(parts) == 3 and parts[1] == "lane" and util.ID_RE.match(parts[2]):
        return parts[0], parts[2]
    return None


def _footprints(programme: str, home: str) -> dict:
    """realpath -> {"path", "ids": {id: confirmed}} for every worktree of this programme with a T3 project:
    local map entries (confirmed unless restored from the journal) and journalled spawned facts (to be
    confirmed by T3 before anything is removed)."""
    from . import lifecycle
    found: dict = {}
    for key, entry in (util.read_json(_projects_path(), {}) or {}).items():
        if isinstance(entry, dict) and entry.get("id") and _slot(home, key):
            item = found.setdefault(os.path.realpath(key), {"path": str(entry.get("path") or key), "ids": {}})
            item["ids"][str(entry["id"])] = entry.get("confirmed") is not False
    for found_ in lifecycle.all_facts(programme).values():
        spawned = lifecycle.phase_fields(found_, "spawned")
        path, ident = spawned.get("t3_worktree"), spawned.get("t3_project_id")
        if spawned.get("launcher") == "t3" and path and ident and _slot(home, str(path)):
            item = found.setdefault(os.path.realpath(str(path)), {"path": str(path), "ids": {}})
            item["ids"].setdefault(str(ident), False)
    for entry in _registrations(programme):
        if _slot(home, str(entry["worktree"])):
            item = found.setdefault(os.path.realpath(str(entry["worktree"])), {"path": str(entry["worktree"]), "ids": {}})
            item["ids"].setdefault(str(entry["project_id"]), False)
    from . import tracker
    for project in tracker.projects():
        if project.get("programme") != programme:
            continue
        lanes = {str(i.get("lane")) for i in tracker.issues(project["id"])
                 if i.get("kind") == "work-package" and i.get("lane")}
        for lane in sorted(lanes):
            # Every merged lane's worktree goes, with or without a T3 project: derived from the tracker's
            # package states and the fixed worktree layout, never from what happens to be on disk.
            path = os.path.join(str(home), ".ygg-worktrees", project["id"], "lane", lane)
            if util.ID_RE.match(lane) and _slot(home, path) and os.path.lexists(path) and \
                    _lane_merged(project["id"], lane):
                found.setdefault(os.path.realpath(path), {"path": path, "ids": {}})
    for real, path in _pending_worktree_removals(programme).items():
        # A worktree removal journalled without its completion stays in view even when the directory is
        # already gone (a stop right after `git worktree remove`), so its completion is journalled next time.
        if _slot(home, path):
            found.setdefault(real, {"path": path, "ids": {}})
    for entry in _removal_records(programme):
        # A worktree whose project was removed stays in view until the worktree itself is gone, and an id
        # whose removal was started is one of its ids (a removed one is filtered out by the caller).
        if entry.get("worktree") and _slot(home, str(entry["worktree"])):
            item = found.setdefault(os.path.realpath(str(entry["worktree"])), {"path": str(entry["worktree"]), "ids": {}})
            if entry.get("removing") and PROJECT_ID_RE.match(str(entry["removing"])):
                item["ids"].setdefault(str(entry["removing"]), False)
    return found


# Project states that give a lane no more work: only then does a merged lane lose its worktree (a lane merged
# while its project is still executing may be authorised another package, as A11's multi-package flow does).
NO_MORE_LANE_WORK = ("ready-to-merge", "closed")


def _lane_merged(project_id: str, lane: str) -> bool:
    """Whether a lane is done for removal: every package on it merged, and its project gives it no more work."""
    from . import tracker
    project = tracker.project(project_id) or {}
    if project.get("state") not in NO_MORE_LANE_WORK:
        return False
    packages = [i for i in tracker.issues(project_id) if i.get("kind") == "work-package" and i.get("lane") == lane]
    return bool(packages) and all(i.get("state") == "merged" for i in packages)


def _owed(programme: str, home: str, only: str | None = None) -> list[dict]:
    from . import tracker
    owed = []
    removed = _removed_projects(programme)
    kept = _kept_worktrees(programme)
    for real, item in _footprints(programme, home).items():
        if only is not None and real != only:
            continue
        if real in kept:
            continue
        ids = {k: v for k, v in item["ids"].items() if k not in removed}
        # A settings baseline left for a worktree that is gone (a stop after its removal) always goes, even if
        # the lane has since reopened: a launch would read it as tamper of the recreated worktree.
        stale_baseline = not ids and not os.path.lexists(real) and \
            real in (util.read_json(_baselines_path(), {}) or {})
        # A journalled removal of a worktree that is gone always finishes: only its completion is owed.
        unfinished = not ids and not os.path.lexists(real) and real in _pending_worktree_removals(programme)
        stale_baseline = stale_baseline or unfinished
        if not ids and not os.path.lexists(real) and not stale_baseline:
            continue  # nothing is left of it
        project_id, lane = _slot(home, real)
        project = tracker.project(project_id)
        if project is None or project.get("programme") != programme:
            continue
        if project.get("state") != "closed" and not (lane and _lane_merged(project_id, lane)) and not stale_baseline:
            continue
        owed.append({"programme": programme, "home": home, "path": item["path"], "ids": ids,
                     "project": project_id, "lane": lane, "t3": bool(item["ids"])})
    return owed


def owed_closes() -> list[dict]:
    """The worktrees owed a close now (derived, never stored)."""
    from . import journal, tracker
    owed = []
    for programme in journal.all_programmes():
        if programme is None:
            continue
        try:
            home = str(tracker.programme_home(tracker.programme(programme)))
        except (YggError, KeyError, TypeError):
            continue
        owed += _owed(programme, home)
    return owed


def _unresolved(programme) -> dict:
    """realpath -> project ids whose removal was started there but is not recorded as done."""
    records = _removal_records(programme)
    done = _removed_projects(programme)
    found: dict = {}
    for entry in records:
        if entry.get("removing") and str(entry["removing"]) not in done:
            found.setdefault(os.path.realpath(str(entry.get("worktree") or "")), set()).add(str(entry["removing"]))
    return found


def _unresolved_intents(programme, real: str) -> set:
    return _unresolved(programme).get(real, set())


def _claims_path() -> Path:
    from . import safety
    return safety.layout() / "t3-closing.json"  # beside the locks: a wiped state root never drops a live claim


def refuse_if_closing(node: dict, nodes: dict) -> None:
    """Called under the journal lock by a launch reservation: no activation starts in a worktree a live
    closer has claimed."""
    from . import launcher
    unresolved = _unresolved(node.get("programme"))
    claims = util.read_json(_claims_path(), {}) or {}
    if not unresolved and not claims:
        return  # nothing is being closed: no need to work out the node's worktree
    try:
        real = os.path.realpath(str(launcher.work_area(node, nodes)))
    except (YggError, KeyError, OSError) as error:
        # Something is being closed and this node's worktree cannot be worked out: fail closed.
        raise util.refuse("launch precondition: the worktree of " + str(node.get("id")) + " cannot be checked "
                          "against a close in progress: " + str(getattr(error, "message", error))[:160])
    pending = unresolved.get(real, set())
    if pending:
        # A removal was started and not settled (the closer stopped): its project may be gone in T3, so
        # nothing may start there until a close settles it.
        raise util.refuse("launch precondition: the removal of T3 project " + ", ".join(sorted(pending)) + " of " +
                          real + " is not settled; the next conductor tick settles it, or settle it yourself with " +
                          "`ygg t3 settle " + sorted(pending)[0] + " --as removed|kept`")
    claim = claims.get(real)
    if isinstance(claim, dict) and util.pid_alive(claim.get("pid")) and \
            util.process_start(claim.get("pid")) == claim.get("started_at"):
        raise util.refuse("launch precondition: " + real + " is being closed (its T3 project and worktree are "
                          "being removed); try again after the close")


def _claim(real: str) -> None:
    from . import lifecycle
    me = lifecycle.own_identity()
    claims = util.read_json(_claims_path(), {}) or {}
    claims[real] = {"pid": me["pid"], "started_at": me["started_at"]}
    util.write_json(_claims_path(), claims)


def _prune_claims() -> None:
    """Drop claims whose closer is gone (it stopped mid-close); the close lock is held, so none is live."""
    with util.locked():
        claims = util.read_json(_claims_path(), {}) or {}
        if claims:
            util.write_json(_claims_path(), {})


def _release(real: str) -> None:
    with util.locked():
        claims = util.read_json(_claims_path(), {}) or {}
        if claims.pop(real, None) is not None:
            util.write_json(_claims_path(), claims)


def _holds_path() -> Path:
    return state.root() / "t3-close-holds.json"


def _hold(real: str, why: str, kind: str = "ygg-t3-cleanup", path: str | None = None) -> None:
    """One alert per worktree and reason (a local mark: a wiped state root alerts once more)."""
    with util.locked():
        holds = util.read_json(_holds_path(), {}) or {}
        seen = holds.get(real) if isinstance(holds.get(real), list) else []
        if why in seen:
            return
        # The alert first, then the mark: a stop in between repeats an alert, never loses one.
        if kind == "ygg-worktree-dirty":
            state.alert(kind, "Kept worktree " + str(path or real) + " on close: it has uncommitted work",
                        path=path or real)
        else:
            state.alert(kind, "Closing the T3 project and worktree " + str(path or real) + " is held: " + why,
                        worktree=path or real)
        holds[real] = seen + [why]
        util.write_json(_holds_path(), holds)


def _unsettled(programme, real: str, project_id: str, lane: str | None) -> bool:
    """An activation that works in this worktree (or a T3 activation recorded at it) has not settled."""
    from . import launcher, lifecycle
    nodes = state.all_nodes()
    users = set()
    for node in nodes.values():
        if node.get("project") != project_id:
            continue
        try:
            if os.path.realpath(str(launcher.work_area(node, nodes))) == real:
                users.add(node["id"])
        except (YggError, KeyError, OSError):
            users.add(node["id"])  # unknown: counted as a user of the worktree
    for found in lifecycle.all_facts(programme).values():
        spawned = lifecycle.phase_fields(found, "spawned")
        mine = any(e.get("node") in users for e in found) or (
            spawned.get("launcher") == "t3" and os.path.realpath(str(spawned.get("t3_worktree") or "")) == real)
        if mine and lifecycle.terminal_of(found) is None:
            return True
    return False


def _threads(worktree: str, project_id: str) -> int | None:
    """How many threads of the project T3 lists at this path (None: the listing could not be read)."""
    listed = run("thread", "list", "--project", worktree, "--json")
    try:
        threads = json.loads(listed.stdout) if listed.returncode == 0 else None
    except ValueError:
        threads = None
    if not isinstance(threads, list):
        return None
    # A thread of a `project add` project reports no worktree path of its own (the listing is scoped to the
    # path); one that reports a different path is not counted.
    return sum(1 for t in threads if isinstance(t, dict) and t.get("projectId") == project_id and
               (not t.get("worktreePath") or os.path.realpath(str(t["worktreePath"])) == os.path.realpath(worktree)))


def close_lane(project_id: str, lane: str) -> None:
    """After a lane merge: finish what is owed now (the next conductor tick finishes the rest)."""
    finish_closes()


def close_project(project_id: str) -> None:
    """After a project close: finish what is owed now (the next conductor tick finishes the rest)."""
    finish_closes()


def finish_closes() -> None:
    """Close every worktree owed a close that can close now (each conductor tick, and after a merge or a
    project close). A busy close lock means another process is closing: it is skipped, never waited on."""
    try:
        with util.locked("t3-close", timeout=0):
            _prune_claims()
            for item in owed_closes():
                real = os.path.realpath(item["path"])
                try:
                    _close_one(item, real)
                except (YggError, OSError) as error:
                    _hold(real, "failed: " + str(getattr(error, "message", error))[:200], path=item["path"])
                finally:
                    _release(real)
    except util.LockTimeout:
        return


def settle(project_id: str, outcome: str) -> str:
    """User-only: settle a T3 project removal that was journalled and never confirmed, which holds every
    launch in its worktree. `removed`: the project is gone in T3 (the close then finishes as usual).
    `kept`: the project stays, and Yggdrasil leaves it and its worktree alone from now on. Journalled once;
    asking again with the same answer changes nothing, and a different answer is refused."""
    from . import journal
    state.check_user_only("t3 settle")  # identity markers, or a managed worktree even without them
    if outcome not in ("removed", "kept"):
        raise util.usage("--as must be removed or kept")
    if not PROJECT_ID_RE.match(str(project_id or "")):
        raise util.usage("invalid T3 project id '" + str(project_id) + "'")
    try:
        with util.locked("t3-close", timeout=0):  # no closer may act on the project while it is settled
            with util.locked():
                for programme in journal.all_programmes():
                    records = _removal_records(programme)
                    started = [e for e in records if str(e.get("removing") or "") == project_id]
                    if not started:
                        continue
                    done = next((key for e in records for key in ("removed", "kept")
                                 if str(e.get(key) or "") == project_id), None)
                    if done == outcome:
                        return "the removal of T3 project " + project_id + " is already settled as " + done + "\n"
                    if done:
                        raise util.refuse("the removal of T3 project " + project_id + " is already settled as " + done)
                    entry = started[-1]
                    journal.add(programme, "t3-close", close_id="tc-" + os.urandom(6).hex(),
                                project=entry["project"], worktree=entry.get("worktree"), **{outcome: project_id})
                    if outcome == "removed":
                        known = util.read_json(_projects_path(), {}) or {}
                        real = os.path.realpath(str(entry.get("worktree") or ""))
                        if (known.get(real) or {}).get("id") == project_id:
                            known.pop(real)
                            util.write_json(_projects_path(), known)
                    break
                else:
                    raise util.refuse("no unsettled removal of T3 project " + project_id + " is on record")
    except util.LockTimeout:
        raise util.refuse("a close is running; try `ygg t3 settle` again when it ends")
    if outcome == "removed":
        finish_closes()
    return "settled the removal of T3 project " + project_id + " as " + outcome + "\n"


def _close_one(item: dict, real: str) -> None:
    from . import journal
    programme, worktree = item["programme"], item["path"]
    with util.locked():
        held = _unsettled(programme, real, item["project"], item.get("lane"))
        if not held:
            # Re-derived under the lock, with the claim taken in the same hold: a project registered or a
            # worktree recreated by a launch since the first read is seen, and no reservation starts here
            # after this.
            fresh = _owed(programme, item["home"], only=real)
            if not fresh:
                return
            item = fresh[0]
            _claim(real)
    if held:
        _hold(real, "an activation in it has not settled", path=worktree)
        return
    problem = _kept(worktree, real, item["home"])
    if problem:
        # Checked under the claim before anything is removed in T3: a worktree that will be kept keeps its
        # T3 project and threads too (the check runs again just before the worktree itself goes).
        _hold(real, problem[0], kind=problem[1], path=worktree)
        return
    intents: dict = {}
    for entry in _removal_records(programme):
        if entry.get("removing"):
            intents[str(entry["removing"])] = max(intents.get(str(entry["removing"]), 0),
                                                  int(entry.get("threads_seen") or 0))
    for project_id, confirmed in sorted(item["ids"].items()):
        if not PROJECT_ID_RE.match(project_id):
            _hold(real, "the recorded T3 project id is not valid", path=worktree)
            return
        if not host_usable():
            _hold(real, "the T3 CLI and server versions are not usable", path=worktree)
            return
        if project_id not in intents:
            seen = _threads(worktree, project_id)
            if seen is None:
                _hold(real, "T3 cannot list the threads at this path", path=worktree)
                return
            if not confirmed and not seen:
                _hold(real, "T3 project " + project_id + " is not confirmed at this path", path=worktree)
                return
            # The intent is journalled before the call, with what T3 listed of the project here then.
            journal.add(programme, "t3-close", close_id="tc-" + os.urandom(6).hex(), project=item["project"],
                        worktree=worktree, removing=project_id, threads_seen=seen)
            intents[project_id] = seen
        result = run("project", "remove", "--force", project_id)
        gone = result.returncode == 0 and (result.stdout or "").startswith("Removed project " + project_id + " ")
        if not gone and intents[project_id] > 0 and _threads(worktree, project_id) == 0:
            # A project seen with threads here now lists none: an earlier removal went through before its
            # record (a crash). A project that never had a thread here cannot be told gone, so it is held.
            gone = True
        if not gone:
            _hold(real, "t3 project remove " + project_id + " failed: " +
                  (result.stderr or result.stdout or "no answer").strip()[:120], path=worktree)
            return
        journal.add(programme, "t3-close", close_id="tc-" + os.urandom(6).hex(), project=item["project"],
                    worktree=worktree, removed=project_id)  # journalled before any local cache changes
        with util.locked():
            known = util.read_json(_projects_path(), {}) or {}
            if (known.get(real) or {}).get("id") == project_id:
                known.pop(real)
                util.write_json(_projects_path(), known)
    if not item.get("t3") and os.path.lexists(os.path.join(real, MANAGED_MARKER)):
        # T3 worked here, but no project of it is on record (its registration was lost): removing the
        # worktree first could leave that project behind, so it is held for the user.
        _hold(real, "it carries T3's marker but no T3 project of it is on record", path=worktree)
        return
    _remove_worktree(worktree, real, item["home"], programme, item["project"])


def _tip_merged(path: str, home: str, real: str) -> bool | None:
    """Whether the worktree's HEAD is in the project's integration branch or in main (None: cannot be read, which
    keeps the worktree). Every close removes a worktree through this check (run while the close is claimed)."""
    slot = _slot(home, real)
    if slot is None:
        return None
    tip = util.trusted_git("-C", path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if tip.returncode != 0 or not tip.stdout.strip():
        return None
    for target in ("refs/heads/ygg/" + slot[0] + "/_integration", "refs/heads/main"):
        exists = util.trusted_git("-C", home, "rev-parse", "--verify", "--quiet", target)
        if exists.returncode != 0:
            continue
        check = util.trusted_git("-C", home, "merge-base", "--is-ancestor", tip.stdout.strip(), target)
        if check.returncode == 0:
            return True
        if check.returncode != 1:
            return None
    return False


def _kept(path: str, real: str, home: str) -> tuple[str, str] | None:
    """(reason, alert kind) when the worktree must be kept: it has uncommitted work, or commits that reached
    neither the integration branch nor main, or either cannot be read. None for a removable or absent one."""
    if not os.path.lexists(real):
        return None
    util.helper_refusal(path, "t3 cleanup")
    status = util.trusted_git("-C", path, "status", "--porcelain")
    if status.returncode != 0:
        return "git status failed: " + status.stderr.strip()[:120], "ygg-t3-cleanup"
    if status.stdout.strip():
        return "dirty", "ygg-worktree-dirty"
    merged = _tip_merged(path, home, real)
    if merged is not True:
        return ("its commits are not merged into the integration branch or main" if merged is False
                else "whether its commits are merged cannot be read"), "ygg-t3-cleanup"
    return None


def _pending_worktree_removals(programme) -> dict:
    """realpath -> path of every worktree whose removal was journalled and whose completion was not."""
    from . import journal
    trusted, _ = journal.trusted_entries(programme)
    last: dict = {}
    for entry in trusted:
        if entry.get("kind") == "worktree-close" and entry.get("worktree"):
            last[os.path.realpath(str(entry["worktree"]))] = (str(entry["worktree"]), entry.get("phase"))
    return {real: path for real, (path, phase) in last.items() if phase == "removing"}


def _worktree_removal_pending(programme, real: str) -> bool:
    """A removal of this worktree was journalled and its completion was not."""
    return real in _pending_worktree_removals(programme)


def _journal_worktree(programme, project_id: str, path: str, phase: str) -> None:
    from . import journal
    journal.add(programme, "worktree-close", close_id="wc-" + os.urandom(6).hex(), project=project_id,
                worktree=path, phase=phase)


def _owned_leftover(worktree: Path, real: str, relative: str) -> bool:
    """Whether the file is one Yggdrasil wrote into the worktree: a regular file (not through a link) that
    the repository does not track, and for the guard settings, the content whose digest was recorded."""
    target = worktree / relative
    if os.path.islink(target.parent) or os.path.islink(target) or not target.is_file():
        return False
    listed = util.trusted_git("-C", str(worktree), "ls-files", "--error-unmatch", "--", relative)
    if listed.returncode != 1:
        return False  # tracked by the repository, or it cannot be told: left for git
    if relative == SETTINGS:
        expected = (util.read_json(_baselines_path(), {}) or {}).get(real)
        try:
            return bool(expected) and _digest(target.read_text()) == expected
        except (OSError, UnicodeDecodeError):
            return False
    return True


def _remove_worktree(path: str, real: str, home: str, programme=None, project_id: str | None = None) -> None:
    """`git worktree remove` a clean worktree whose commits are merged from its home repository and drop its
    settings baseline. Any other one is kept with one alert (the work in it is the user's). The removal is
    journalled only once the worktree is found clean and merged, before anything local changes, and its
    completion after."""
    worktree = Path(path)
    journalled = programme is not None and project_id is not None
    if os.path.lexists(real):
        problem = _kept(path, real, home)
        if problem:
            _hold(real, problem[0], kind=problem[1], path=path)
            return
        if journalled and not _worktree_removal_pending(programme, real):
            _journal_worktree(programme, project_id, path, "removing")
        # The marker and the guard settings Yggdrasil wrote are kept out of Git; they go so the clean worktree
        # can be removed. A file the repository tracks (its own settings) is never touched here: git removes
        # it with the worktree.
        for leftover in (MANAGED_MARKER, SETTINGS):
            if _owned_leftover(worktree, real, leftover):
                (worktree / leftover).unlink(missing_ok=True)
        claude = worktree / ".claude"
        if claude.is_dir() and not claude.is_symlink() and not any(claude.iterdir()):
            claude.rmdir()
        result = util.trusted_git("-C", home, "worktree", "remove", path)
        if result.returncode != 0:
            _hold(real, "git worktree remove failed: " + result.stderr.strip()[:120], path=path)
            return
    if journalled and _worktree_removal_pending(programme, real):
        _journal_worktree(programme, project_id, path, "removed")
    with util.locked():
        baselines = util.read_json(_baselines_path(), {}) or {}
        if baselines.pop(real, None) is not None:
            util.write_json(_baselines_path(), baselines)
        holds = util.read_json(_holds_path(), {}) or {}
        if holds.pop(real, None) is not None:
            util.write_json(_holds_path(), holds)


# ---------------------------------------------------------------- threads and evidence (ruling 106)

def _show(thread: str) -> tuple[str, dict | None, str]:
    """(answer, payload, stderr): `json`, `gone` or `unreadable`."""
    if not THREAD_RE.match(str(thread or "")):
        return "unreadable", None, "no usable thread id"
    result = run("thread", "show", "--json", "--", thread)
    if result.returncode == 1 and GONE.match((result.stderr or "").strip()):
        return "gone", None, result.stderr
    try:
        payload = json.loads(result.stdout) if result.returncode == 0 and (result.stdout or "").strip() else None
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("thread"), dict):
        return "unreadable", None, result.stderr
    return "json", payload, ""


def _markers(payload: dict) -> list[tuple[str, str]]:
    """(activation, runId) of every user message whose first line is a Yggdrasil marker, in thread order."""
    found = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        first = str(message.get("text") or "").splitlines()[0] if message.get("text") else ""
        match = MARKER_RE.match(first.strip())
        if match:
            found.append((match[1], str(message.get("runId") or "")))
    return found


def _last_assistant(payload: dict) -> str:
    texts = [str(m.get("text") or "") for m in payload.get("messages") or []
             if isinstance(m, dict) and m.get("role") == "assistant"]
    return texts[-1] if texts else ""


def classify(payload: dict, activation: str, identity: dict) -> dict:
    """The activation's evidence from one readable `thread show` (ruling 106)."""
    thread = payload["thread"]
    unknown = {"state": "unknown"}
    if not identity.get("title") or thread.get("title") != identity["title"]:
        return dict(unknown, why="the thread title does not match")
    if not identity.get("project_id") or thread.get("projectId") != identity["project_id"]:
        return dict(unknown, why="the thread is not in the activation's T3 project")
    if not isinstance(thread.get("status"), str) or "activeRunId" not in thread or \
            not isinstance(thread.get("archived"), bool) or isinstance(thread.get("pendingRequestCount"), bool) or \
            not isinstance(thread.get("pendingRequestCount"), int):
        return dict(unknown, why="the thread details lack a required field")
    if thread["archived"] or thread["pendingRequestCount"] > 0:
        return dict(unknown, why="the thread is archived or waits on a request")
    status = thread["status"]
    if status not in ALIVE and status not in ENDED:
        _log_unseen(status)
        return dict(unknown, why="status " + str(status))
    if not isinstance(payload.get("messages"), list) or \
            not all(isinstance(m, dict) and isinstance(m.get("role"), str) and isinstance(m.get("text"), str)
                    and isinstance(m.get("runId"), str) for m in payload["messages"]):
        # Every message must be complete: a marker is trusted, and its absence acted on, only then.
        return dict(unknown, why="the thread's messages are missing or malformed")
    markers = _markers(payload)
    if any(not run_id for _, run_id in markers):
        return dict(unknown, why="a marker message has no run")
    mine = [m for m in markers if m[0] == activation]
    if not mine:
        return {"state": "no-marker"}
    if markers[-1][0] != activation:
        return dict(unknown, why="a newer activation's marker is in the thread")
    if status in ALIVE:
        return {"state": "alive"}
    if thread.get("activeRunId") in (None, ""):
        return {"state": "ended", "exit_code": ENDED[status], "text": _last_assistant(payload)}
    return dict(unknown, why="status " + str(status) + " with an active run")


def _log_unseen(status) -> None:
    path = state.root() / "t3-statuses.json"
    seen = util.read_json(path, {}) or {}
    if str(status) not in seen:
        seen[str(status)] = util.iso()
        util.write_json(path, seen)
        state.alert("ygg-t3-status", "T3 reported an unrecognised thread status '" + str(status) +
                    "'; activations in it are held", status=str(status))


# ---------------------------------------------------------------- the activation's T3 identity

def identity(node_id: str, activation: str) -> dict | None:
    """The T3 identity of an activation: from its spawned fact (durable) and its record (thread id)."""
    from . import launcher, lifecycle
    found = lifecycle.facts(launcher._programme(node_id), activation)
    spawned = lifecycle.phase_fields(found, "spawned")
    if spawned.get("launcher") != "t3":
        return None
    data = launcher.receipt(activation)
    exited = lifecycle.phase_fields(found, "exited")
    return {"thread": data.get("t3_thread") or spawned.get("t3_thread") or exited.get("t3_thread"),
            "title": spawned.get("t3_title"), "worktree": spawned.get("t3_worktree"),
            "project_id": spawned.get("t3_project_id"), "pending": data.get("t3_pending"),
            "retried": bool(data.get("t3_retried")), "kind": spawned.get("t3_kind"),
            "dispatcher": data.get("t3_dispatcher")}


def is_t3(node_id: str, activation: str) -> bool:
    return identity(node_id, activation) is not None


_EVIDENCE: dict = {}


def evidence(node_id: str, activation: str) -> dict:
    """Fresh evidence for a T3 activation (memoised for one second within a process, so one status read asks
    T3 once per activation). `pending` while a call's outcome is unknown; else ruling 106's values."""
    ident = identity(node_id, activation) or {}
    if ident.get("pending"):
        return {"state": "pending"}
    cached = _EVIDENCE.get(activation)
    if cached and time.monotonic() - cached[0] < 1.0:
        return cached[1]
    thread = ident.get("thread") or _find_by_title(node_id, activation, ident)
    answer, payload, _ = _show(str(thread or ""))
    if answer == "gone":
        found = {"state": "gone"}
    elif answer == "json":
        found = classify(payload, activation, ident)
        if found["state"] == "no-marker":
            found = {"state": "unknown", "why": "the activation's marker is not in the thread"}
    else:
        found = {"state": "unknown", "why": "thread details unreadable"}
    _EVIDENCE[activation] = (time.monotonic(), found)
    return found


def _find_by_title(node_id: str, activation: str, ident: dict) -> str | None:
    """After a wiped state root, a started thread is found again by its journalled worktree and title
    (ruling 73); exactly one match counts, and its id is recorded."""
    if not ident.get("worktree") or not ident.get("title"):
        return None
    result = run("thread", "list", "--project", str(ident["worktree"]), "--json")
    try:
        threads = json.loads(result.stdout) if result.returncode == 0 else None
    except ValueError:
        threads = None
    matching = [t for t in threads or [] if isinstance(t, dict) and t.get("title") == ident["title"]]
    if len(matching) != 1 or not THREAD_RE.match(str(matching[0].get("threadId") or "")):
        return None
    from . import launcher
    with util.locked():
        launcher.update_receipt(activation, t3_thread=str(matching[0]["threadId"]), node=node_id, launcher="t3")
    return str(matching[0]["threadId"])


def forget(activation: str) -> None:
    _EVIDENCE.pop(activation, None)


def node_thread(node_id: str) -> tuple[str | None, str | None]:
    """(thread id, thread title) of the node's newest T3 activation, from the journal and its record."""
    from . import launcher, lifecycle
    newest = None
    for key, found in lifecycle.all_facts(launcher._programme(node_id)).items():
        spawned = lifecycle.phase_fields(found, "spawned")
        if spawned.get("launcher") == "t3" and any(e.get("node") == node_id for e in found):
            if newest is None or int(key.split("-")[1]) > int(newest[0].split("-")[1]):
                newest = (key, spawned)
    if newest is None:
        return None, None
    key, spawned = newest
    ident = identity(node_id, key) or {}
    thread = ident.get("thread") or _find_by_title(node_id, key, ident)  # after a wipe: found by its title
    return thread, spawned.get("t3_title")


# ---------------------------------------------------------------- messages (ruling 105, B30)

def marker(activation: str, message: str) -> str:
    return "[ygg " + activation + " " + message + "]"


def prompt_file(activation: str, prompt: str) -> Path:
    path = state.root() / "launch" / activation / "prompt.md"
    util.ensure_dir(path.parent)
    util.atomic_write(path, prompt.rstrip("\n") + "\n", mode=0o600)
    os.chmod(path, 0o600)
    return path


def message(activation: str, msg_id: str, node_id: str, prompt_path: Path) -> str:
    return (marker(activation, msg_id) + "\nThis is a Yggdrasil-managed session. Read your instructions in " +
            str(prompt_path) + " and your messages with `ygg inbox " + node_id + "`.")


def outcome_of(result: subprocess.CompletedProcess) -> tuple[str, dict]:
    try:
        payload = json.loads(result.stdout) if (result.stdout or "").strip() else None
    except ValueError:
        payload = None
    if result.returncode == 0 and isinstance(payload, dict):
        return "confirmed", payload
    if result.returncode not in (0, HELD) and isinstance(payload, dict) and payload.get("error"):
        return "failed", payload
    return "uncertain", payload or {}


def _log_call(node_id: str, activation: str, action: str, thread, outcome: str) -> None:
    util.append_line(state.root() / "sends.jsonl", {"ts": util.iso(), "node": node_id, "activation": activation,
                                                    "action": action, "thread": thread, "outcome": outcome})


def _provider_args(model: str) -> list[str]:
    provider = ((config.profile() or {}).get("pools") or {}).get("launched_claude")
    return (["--provider", str(provider)] if provider else []) + ["--model", model]


# ---------------------------------------------------------------- launch

def plan(node_id: str) -> tuple[str, str | None, str | None]:
    """How the next activation reaches T3: (`start`, None, None) or (`send`, thread, title). A thread that
    T3 reports gone gets a new thread; one whose state cannot be read refuses the launch (exit 8)."""
    thread, title = node_thread(node_id)
    if not thread:
        return "start", None, None
    answer, payload, stderr = _show(thread)
    if answer == "gone":
        return "start", None, None
    if answer != "json":
        raise YggError(util.EXIT_TRACKER, "t3: the state of thread " + str(thread) + " of " + node_id +
                       " cannot be read (" + (stderr or "no answer").strip()[:120] + "); launch held")
    why = _reuse_problem(node_id, payload)
    if why:
        raise YggError(util.EXIT_TRACKER, "t3: thread " + str(thread) + " of " + node_id + " is not ready for "
                       "another activation (" + why + "); launch held")
    return "send", thread, title


_SHOWN: dict = {}


def reuse_hold(node_id: str) -> str | None:
    """Why the node's next activation is held by its T3 thread right now, or None (derived on every read,
    never stored, so it clears as soon as T3 reports the thread's run ended). A status read asks T3 at most
    once a second per thread."""
    if binary() is None:
        return None
    thread, _ = node_thread(node_id)
    if not thread:
        return None
    cached = _SHOWN.get(thread)
    if cached and time.monotonic() - cached[0] < 1.0:
        answer, payload = cached[1]
    else:
        answer, payload, _ = _show(thread)
        _SHOWN[thread] = (time.monotonic(), (answer, payload))
    if answer == "gone":
        return None
    if answer != "json":
        return "T3 thread " + str(thread) + " cannot be read; its next activation is held"
    why = _reuse_problem(node_id, payload)
    return ("T3 thread " + str(thread) + " is not ready for another activation (" + why + ")") if why else None


def _reuse_problem(node_id: str, payload: dict) -> str | None:
    """Why the thread may not take another activation now, or None. Fresh details must show it is this node's
    (title, project) and that the run of the activation whose marker is its last message has ended (ruling 106):
    a running, active, interrupted or otherwise unknown thread is held."""
    try:
        markers = _markers(payload)
    except (KeyError, TypeError, AttributeError):
        return "its details are malformed"
    if not markers:
        return "no Yggdrasil activation has a message in it"
    last = markers[-1][0]
    from . import launcher, lifecycle
    found = lifecycle.facts(launcher._programme(node_id), last)
    ident = identity(node_id, last) if found and all(e.get("node") == node_id for e in found) else None
    if not ident:
        return "its last message belongs to no activation of " + node_id
    seen = classify(payload, last, ident)
    if seen["state"] == "ended":
        return None
    return "its run is still going" if seen["state"] == "alive" else str(seen.get("why") or "its state is unknown")


def launch(node: dict, activation: str, cwd: Path, model: str, previous: dict | None, route: tuple,
           project_id: str | None = None) -> dict:
    """The T3 half of `launcher.launch`, after the reservation, preparation and hold check."""
    from . import launcher, lifecycle
    node_id = node["id"]
    kind, thread, thread_title = route
    title = node_id + " " + activation if kind == "start" else str(thread_title or "")
    data = launcher.receipt(activation)
    messages = list((data.get("consumed") or {}).get("messages") or [])
    msg_id = messages[0] if messages else "launch"
    prompt_path = prompt_file(activation, launcher.prompt_for(node, activation))
    text = message(activation, msg_id, node_id, prompt_path)
    me = lifecycle.own_identity()
    # Gate: the reservation is this node's newest and unsettled; `spawned` names the T3 identity.
    with util.locked():
        found = lifecycle.facts(launcher._programme(node_id), activation)
        if lifecycle.terminal_of(found) is not None or launcher._newest_reservation(node_id) != activation:
            raise launcher.abort_error(node_id, activation, "superseded")
        if not lifecycle.commit(node_id, activation, "spawned", launcher="t3", t3_kind=kind, t3_title=title,
                                t3_thread=thread, t3_worktree=str(cwd), t3_project_id=project_id,
                                supervisor_pid=me["pid"], supervisor_started_at=me["started_at"]):
            raise launcher.abort_error(node_id, activation, "superseded")
        # The dispatching process is named at the gate: reconciliation never retries a call it still owns.
        launcher.update_receipt(activation, launcher="t3", phase="dispatching", t3_thread=thread, t3_title=title,
                                t3_worktree=str(cwd), t3_pending=kind, process="t3", lifecycle=True,
                                t3_dispatcher={"pid": me["pid"], "started_at": me["started_at"]})
        runtime = state.runtime(node_id)
        if runtime.get("activation") == activation:
            runtime.update(launcher="t3", t3_thread=thread)
            state.save_runtime(node_id, runtime)
    return dispatch(node_id, activation, kind, title=title, worktree=str(cwd), thread=thread, model=model,
                    text=text, node=node, previous=previous)


def _no_agent(node_id: str, activation: str, why: str, previous: dict | None = None) -> YggError:
    """Nothing was sent: the no-agent record is already on disk; the journal step aborts the activation."""
    from . import launcher, lifecycle
    with util.locked():
        if launcher.commit_aborted(node_id, activation, "final-check:" + why):
            lifecycle.no_agent_path(activation).unlink(missing_ok=True)
        launcher.update_receipt(activation, t3_dispatcher=None, t3_pending=None)
        launcher.abort_activation(node_id, activation, launcher._abort_reason(why), previous)
    return launcher.abort_error(node_id, activation, launcher._abort_reason(why))


def dispatch(node_id: str, activation: str, kind: str, *, title: str, worktree: str, thread: str | None,
             model: str, text: str, node: dict | None = None, previous: dict | None = None) -> dict:
    """One `thread start` or `thread send` for a spawned activation, and its recorded outcome.

    Admission is atomic with stop and pause: under the creation lock, which stop and pause take to publish
    their markers, the markers and the revocation record are read and, only if none applies, the `t3`
    process is started. The lock covers local reads and the process start only; the wait for T3's answer
    happens after it is released. A stop that returns before admission therefore always prevents the call."""
    from . import launcher, lifecycle
    node = node or state.all_nodes().get(node_id) or {"id": node_id}
    with util.locked():
        launcher.update_receipt(activation, t3_text=text, t3_model=model)
    problem = settings_problem(worktree, launcher.receipt(activation).get("t3_settings_sha"))
    if problem:
        tampered(node_id, activation, problem)
        lifecycle.write_local(lifecycle.no_agent_path(activation), {"activation": activation,
                                                                    "reason": "settings-tampered", "at": util.iso()})
        raise _no_agent(node_id, activation, "stopped", previous)
    usable = host_usable()  # the version gate, before the lock (it may run `t3 --version`)
    if kind == "start":
        args = ("thread", "start", "--json", "--title", title, *_provider_args(model), "--", worktree, text)
    else:
        args = ("thread", "send", "--json", "--", str(thread), text)
    with util.locked(util.CREATION_LOCK):
        refusal = launcher._local_refusal(node, activation)
        if refusal:
            lifecycle.write_local(lifecycle.no_agent_path(activation), {"activation": activation, "reason": refusal,
                                                                        "at": util.iso()})
            started = None
        else:
            started = _spawn_gated(activation, args) if usable else \
                subprocess.CompletedProcess(args, HELD, "", "t3 call held: T3 versions are not usable")
    if refusal:
        raise _no_agent(node_id, activation, refusal, previous)
    result = _finish(started)
    with util.locked():
        launcher.update_receipt(activation, t3_dispatcher=None)
    outcome, payload = outcome_of(result)
    _log_call(node_id, activation, kind, thread or payload.get("threadId"), outcome)
    forget(activation)
    if outcome == "uncertain":
        launcher.hold_uncertain(node_id, activation, "t3 thread " + kind + " gave no answer: " +
                                (result.stderr or "").strip()[:120])
        return {"node": node_id, "activation": activation, "outcome": "uncertain", "started": True}
    if outcome == "failed":
        with util.locked():
            launcher.update_receipt(activation, exit_code=1, exited_at=util.iso(), spawn_failed=True, t3_pending=None,
                                    stderr_tail=str(payload.get("error") or result.stderr or "")[-500:])
            lifecycle.commit(node_id, activation, "exited", exit_code=1, spawn_failed=True, capacity=False,
                             t3_thread=thread)
        launcher.finalize(node_id, activation, "failed", resolving=("pending", "uncertain"))
        _settle_runtime(node_id, activation)
        return {"node": node_id, "activation": activation, "outcome": "failed", "started": False}
    found = payload.get("threadId") if kind == "start" else thread
    if not THREAD_RE.match(str(found or "")):
        launcher.hold_uncertain(node_id, activation, "t3 thread " + kind + " returned no usable thread id")
        return {"node": node_id, "activation": activation, "outcome": "uncertain", "started": True}
    _confirm(node_id, activation, str(found))
    return {"node": node_id, "activation": activation, "outcome": "confirmed", "started": True}


def _confirm(node_id: str, activation: str, thread: str) -> None:
    from . import launcher
    with util.locked():
        launcher.update_receipt(activation, t3_thread=thread, t3_pending=None, started_at=util.iso())
        runtime = state.runtime(node_id)
        if runtime.get("activation") == activation:
            runtime.update(t3_thread=thread, launcher="t3")
            state.save_runtime(node_id, runtime)
    launcher.finalize(node_id, activation, "confirmed", resolving=("pending", "uncertain"))
    with util.locked():
        data = launcher.receipt(activation)
        if data.get("outcome") == "uncertain":
            launcher.update_receipt(activation, outcome="confirmed", uncertain_reason=None)
        runtime = state.runtime(node_id)
        if runtime.get("activation") == activation and runtime.get("state") != "stopped":
            runtime.update(state="running", outcome="confirmed")
            state.save_runtime(node_id, runtime)


def _settle_runtime(node_id: str, activation: str) -> None:
    """An ended T3 activation leaves its node idle (no local process), so work owed from its exit fact, a
    retry included, can launch at once."""
    with util.locked():
        runtime = state.runtime(node_id)
        if runtime.get("activation") == activation and runtime.get("state") != "stopped":
            runtime.update(state="idle", outcome="confirmed", last_exit_at=util.iso())
            state.save_runtime(node_id, runtime)


# ---------------------------------------------------------------- reconciliation (every tick)

def reconcile(node_id: str, activation: str) -> None:
    """Resolve an unsettled T3 activation from T3's answer: find the thread or marker of a call whose outcome
    was unknown (retrying it once inside the activation when T3 answers without it), then commit the exit of
    an ended run, or hold a gone thread as uncertain."""
    from . import launcher, lifecycle
    ident = identity(node_id, activation)
    if ident is None:
        return
    owner = ident.get("dispatcher") or {}
    if ident.get("pending") and owner and \
            lifecycle.evidence(owner.get("pid"), owner.get("started_at")) in ("alive", "unknown"):
        return  # the call is still in flight in the process that made it: never retried behind its back
    if ident.get("pending") and call_in_flight(activation):
        return  # its t3 process outlived the dispatcher and may still reach T3: wait for it to end
    if ident.get("pending") == "start":
        result = run("thread", "list", "--project", str(ident.get("worktree")), "--json")
        try:
            threads = json.loads(result.stdout) if result.returncode == 0 else None
        except ValueError:
            threads = None
        if not isinstance(threads, list):
            return  # unreadable: still uncertain, held
        if not all(isinstance(t, dict) for t in threads):
            return
        matching = [t for t in threads if t.get("title") == ident.get("title")]
        if any(t.get("projectId") != ident.get("project_id") for t in matching):
            return  # a thread with this title outside the activation's project: unknown, held
        if len(matching) == 1 and THREAD_RE.match(str(matching[0].get("threadId") or "")):
            _confirm(node_id, activation, str(matching[0]["threadId"]))
        elif not matching and not ident.get("retried"):
            _retry(node_id, activation, "start", ident)
        return
    if ident.get("pending") == "send":
        answer, payload, _ = _show(str(ident.get("thread") or ""))
        if answer != "json":
            return
        # The same identity and field checks as evidence: a marker is trusted, and its absence retried,
        # only in the activation's own thread with complete details.
        found = classify(payload, activation, ident)
        if found["state"] == "no-marker":
            if not ident.get("retried"):
                _retry(node_id, activation, "send", ident)
        elif found["state"] in ("alive", "ended"):
            _confirm(node_id, activation, str(ident["thread"]))
        return
    seen = evidence(node_id, activation)
    if seen["state"] == "ended":
        _commit_exit(node_id, activation, seen)
    elif seen["state"] == "gone":
        launcher.mark_uncertain(node_id, activation, "the T3 thread is no longer available", allow_confirmed=True)
    elif seen["state"] == "alive":
        with util.locked():
            runtime = state.runtime(node_id)
            if runtime.get("activation") == activation and runtime.get("state") not in ("running", "stopped"):
                runtime.update(state="running")
                state.save_runtime(node_id, runtime)


def _retry(node_id: str, activation: str, kind: str, ident: dict) -> None:
    """The one permitted retry, claimed under the journal lock: a second reconciler, or the original
    dispatcher, never makes it twice."""
    from . import launcher, lifecycle
    me = lifecycle.own_identity()
    with util.locked():
        data = launcher.receipt(activation)
        owner = data.get("t3_dispatcher") or {}
        if data.get("t3_retried") or not data.get("t3_pending") or not data.get("t3_text") or \
                (owner and lifecycle.evidence(owner.get("pid"), owner.get("started_at")) != "dead") or \
                call_in_flight(activation):
            return
        launcher.update_receipt(activation, t3_retried=True,
                                t3_dispatcher={"pid": me["pid"], "started_at": me["started_at"]})
    try:
        dispatch(node_id, activation, kind, title=str(ident.get("title") or ""), worktree=str(ident.get("worktree")),
                 thread=ident.get("thread"), model=str(data.get("t3_model") or data.get("model") or ""),
                 text=str(data["t3_text"]))
    except YggError:
        pass  # refused (a tampered worktree, a stop): the activation is settled by its own record


def _commit_exit(node_id: str, activation: str, seen: dict) -> None:
    from . import launcher, lifecycle
    with util.locked():
        record = launcher.receipt(activation)
        record.update(exit_code=seen["exit_code"], exited_at=util.iso(), stderr_tail=str(seen.get("text") or "")[-2000:])
        launcher.save_receipt(activation, record)
        lifecycle.commit(node_id, activation, "exited", exit_code=seen["exit_code"],
                         t3_thread=record.get("t3_thread"), **launcher.exit_fields(record, activation))
    launcher.finalize(node_id, activation, launcher.classify(launcher.receipt(activation)) or "failed",
                      resolving=("pending", "uncertain", "confirmed"))
    _settle_runtime(node_id, activation)
    forget(activation)


# ---------------------------------------------------------------- live-thread delivery (AC-36, fallback-v1.1)

LIVE_SENDS = "t3-live-sends.jsonl"


def live_send(message: dict) -> bool:
    """Send an urgent message into the recipient's running T3 thread as well as its inbox (AC-36), once per
    message, so ticks never repeat it. Behind the `t3_live_send` release switch: switched off, delivery stays the
    inbox and the notice only. The body goes inside a fence, as data. Returns whether a send was made. The
    message send path (lane 2) calls it after the inbox write; a failed send leaves the inbox delivery as it is."""
    from . import launcher, release
    if not release.on("t3_live_send") or not message.get("urgent"):
        return False
    recipient, key = str(message.get("node") or ""), str(message.get("msg_id") or "")
    if not recipient or not key:
        return False
    runtime = state.runtime(recipient)
    if runtime.get("launcher") != "t3" or runtime.get("state") != "running":
        return False
    activation = str(runtime.get("activation") or "")
    thread = runtime.get("t3_thread") or launcher.receipt(activation).get("t3_thread") if activation else \
        runtime.get("t3_thread")
    if not thread:
        return False
    path = state.root() / LIVE_SENDS
    with util.locked():
        if any(entry.get("msg_id") == key for entry in util.read_lines(path)):
            return False
        text = ("Message " + key + " from " + str(message.get("sender") or "") + " (urgent):\n" +
                launcher._fenced(str(message.get("body") or "")))
        result = run("thread", "send", "--json", "--", str(thread), text)
        if result.returncode != 0:
            return False
        util.append_line(path, {"msg_id": key, "node": recipient, "thread": str(thread), "at": util.iso()})
    return True
