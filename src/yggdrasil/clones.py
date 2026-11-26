"""Lane clones for Codex builders (v1.1.1): the registry, assembly without a transport, the runner that
starts the sandboxed tender, tending at launch, the receive step and the feed at lane sync.

Invariant T: once a box exists, nothing here opens, reads, writes, lists or runs git inside it. Trusted code
lists the lane folder (no agent can write it), checks a box by descriptor (`open_box`), renames only a folder no
agent has run in, and reads the pipes of the tender (`run_tender`), which does everything inside the box.

Interfaces other modules use:
- `is_clone_lane(project, lane)`, `read(project, name)`, `record_path(project, name)`, `lane_folder(home, project)`
- `lane_checkout(node, home, project, tl, activation)`: the launch's working folder for a Codex team lead or worker
- `run_tender(record, mode, spec, ...)`, `open_box(record)`, `report(record, lane_head)`
- `quarantine(project, tl, reason=..., next_step=..., odd_check=..., at=...)` and `do_next(project, tl)`
- `receive(project, lane, from_node=None)` (the checks) and `receive_commit(...)` (the final move of the lane)
- `sync_precheck(project, lane, home)` and `feed(project, lane, home)` around today's `gitops.lane_sync`
- `check_codex(prefix)`: the version pin before a helper run; `codexlaunch` holds the pin itself
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import codexlaunch, faults, state, util

GENERATION_MAX = 999999
GENERATION_RE = r"^{}\.g([0-9]{{6}})$"
PACK_LIMIT = 512 * 1024 * 1024
ASSEMBLY_SECONDS = 300
REPORT_SECONDS = 60
PACK_SECONDS = 600
OUTPUT_LIMIT = 64 * 1024
HEX = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
SANDBOX_KEYS = ("sandbox_workspace_write.writable_roots=[]", "sandbox_workspace_write.network_access=false",
                "sandbox_workspace_write.exclude_slash_tmp=true",
                "sandbox_workspace_write.exclude_tmpdir_env_var=true")
LACKS_NEWEST = "re-check refused: the clone lacks the lane's newest commits; the next launch brings them"
IN_TIME = "re-check refused: the clone could not be read in time"
REPLACE_SUFFIX = "; the next launch replaces the clone and keeps this one for you"


def lane_name(project: str, tl: str) -> str:
    return "ygg/" + project + "/lane/" + tl


def lane_folder(home: str, project: str) -> Path:
    return Path(home) / ".ygg-worktrees" / project / "lane"


def box_name(tl: str, generation: int) -> str:
    return tl + ".g" + str(int(generation)).zfill(6)


def registry_dir(project: str) -> Path:
    return state.root() / "lane-clones" / project


def record_path(project: str, name: str) -> Path:
    util.check_id(project, "project")
    return registry_dir(project) / (name + ".json")


def quarantine_dir(project: str) -> Path:
    return registry_dir(project) / "_quarantine"


def read(project: str, name: str) -> dict | None:
    value = util.read_json(record_path(project, name), None)
    return value if isinstance(value, dict) else None


def write(project: str, name: str, record: dict) -> None:
    util.write_json(record_path(project, name), record)


def is_clone_lane(project: str | None, lane: str | None) -> bool:
    if not project or not lane:
        return False
    record = read(project, lane)
    return bool(record and record.get("kind") == "lane")


def registered_checkout(project: str | None, path) -> bool:
    """Whether `path` is, as a string, the checkout of a lane clone the registry names (never resolved)."""
    if not project or not path:
        return False
    folder = registry_dir(project)
    if not folder.is_dir():
        return False
    for entry in sorted(folder.glob("*.json")):
        value = util.read_json(entry, None)
        if isinstance(value, dict) and value.get("kind") == "lane" and value.get("path") == str(path):
            return True
    return False


CLONE_PATH = re.compile(r"/\.ygg-worktrees/[^/]+/(lane|test|_removing)/")


def clone_path(path) -> bool:
    """Whether `path`, as a string and never resolved, lies in a lane, test or `_removing` folder: trusted git is
    never run there, whatever the registry says now (invariant T)."""
    return bool(path) and bool(CLONE_PATH.search(str(path).rstrip("/") + "/"))


def pin(record: dict, project: str | None = None, name: str | None = None) -> dict:
    """The clone identity a launch records in its receipt: the registry name, kind, generation and nonce, and the
    box's path, device and inode. The supervisor checks it before it builds the command line (`pin_refusal`)."""
    kind = record.get("kind")
    if kind == "test":
        project = project or record.get("project")
        name = name or test_record_name(str(record["node"]), str(record["activation"]))
    elif not (project and name):
        _, project, _, name = str(record["branch"]).split("/", 3)
    return {"project": project, "name": name, "kind": kind, "generation": record.get("generation"),
            "nonce": record.get("nonce"), "box": record.get("box"), "path": record.get("path"),
            "box_device": record.get("box_device"), "box_inode": record.get("box_inode")}


def pin_refusal(pinned: dict) -> str | None:
    """None when the pinned clone is still the registry's live one, `ready`, with the same generation, nonce, path
    and box identity (the box's own entry checked by `open_box`, nothing inside it read); otherwise the refusal."""
    project, name = pinned.get("project"), pinned.get("name")
    what = ("the test clone " + str(name) if pinned.get("kind") == "test" else
            "the clone of lane " + lane_name(str(project), str(name)))
    generation = pinned.get("generation")
    where = what + (" at generation " + str(generation) if generation is not None else "")
    try:
        record = read(str(project), str(name))
    except util.YggError:
        record = None
    if not record:
        return "launch precondition: " + where + " has no record; this activation does not start"
    if record.get("generation") != generation or record.get("nonce") != pinned.get("nonce"):
        return ("launch precondition: " + where + " was replaced by generation " + str(record.get("generation")) +
                "; this activation does not start")
    if record.get("state") != "ready":
        return "launch precondition: " + where + " is " + str(record.get("state")) + "; this activation does not start"
    keys = ("box", "path", "box_device", "box_inode")
    if any(record.get(key) != pinned.get(key) for key in keys):
        return "launch precondition: " + str(pinned.get("box")) + " is not the box this activation was given"
    try:
        os.close(open_box(pinned))
    except BoxOdd:
        return "launch precondition: " + str(pinned.get("box")) + " is not the box this activation was given"
    return None


def quarantine_records(project: str, tl: str) -> list[dict]:
    folder = quarantine_dir(project)
    found = []
    if not folder.is_dir():
        return found
    pattern = re.compile(GENERATION_RE.format(re.escape(tl)).replace("$", r"\.json$"))
    for path in sorted(folder.iterdir()):
        if pattern.match(path.name):
            value = util.read_json(path, None)
            if isinstance(value, dict):
                found.append(value)
    return found


def next_generation(project: str, tl: str, home: str) -> int:
    """One more than the largest generation of the record (a tombstone too), the quarantine records and the
    lane folder's entries that match `<tl>.g<6 digits>`, all read under the journal lock."""
    numbers = [0]
    record = read(project, tl)
    if record and isinstance(record.get("generation"), int):
        numbers.append(record["generation"])
    numbers += [int(r.get("generation") or 0) for r in quarantine_records(project, tl)]
    folder = lane_folder(home, project)
    pattern = re.compile(GENERATION_RE.format(re.escape(tl)))
    if folder.is_dir():
        for name in os.listdir(folder):
            match = pattern.match(name)
            if match:
                numbers.append(int(match.group(1)))
    return max(numbers) + 1


def git(*args, check=False):
    return util.trusted_git(*args, check=check)


def lane_lock(project: str, tl: str):
    """One lock per lane, held across an assembly, a recovery, a launch's tending and a feed, so a second launch
    never takes a running assembly for an interrupted one. Taken before the journal lock, never inside it; a lane
    lock this thread already holds is re-entered, since that keeps the order (a cancel holds it across its assembly)."""
    util.check_id(project, "project")
    util.check_id(tl, "lane")
    name = "lane-clone-" + project + "-" + tl
    if util.lock_held("journal") and not util.lock_held(name):
        raise RuntimeError("lock order: the lane lock is taken before the journal lock")
    return util.locked(name)


class _Superseded(Exception):
    pass


def _owned(project: str, tl: str, nonce: str) -> dict:
    """The record, when this assembly's nonce still owns it; otherwise the assembly stops."""
    current = read(project, tl)
    if not current or current.get("nonce") != nonce or current.get("state") != "creating":
        raise util.refuse("launch precondition: the assembly of lane " + lane_name(project, tl) + " is no longer "
                          "the lane's own; try again")
    return current


def _rev(home: str, ref: str) -> str:
    return git("-C", home, "rev-parse", "--verify", "--quiet", ref + "^{commit}").stdout.strip()


# ---------------------------------------------------------------- version pin

def check_codex(prefix: str = "re-check refused") -> dict:
    """The version pin before a helper run. `codexlaunch` is its single source: the probed pairs, the
    exact-line match and the refusal texts. Outside test mode this is its strict probe, which resolves the binary,
    records its identity and runs `--version`. Under test mode the helper never starts the binary, so it is neither
    resolved nor run: `YGG_TEST_CODEX_VERSION` stands for its output and `path` is None."""
    lead = prefix + ": "
    if not util.test_mode():
        return codexlaunch.probe(strict=True, prefix=lead)
    platform = codexlaunch.current_platform()
    codexlaunch.require_platform(platform, lead)
    text = os.environ.get("YGG_TEST_CODEX_VERSION")
    if text is None:
        return {"path": None, "identity": None, "version": "not checked (test mode)"}
    line = codexlaunch.version_line(text)
    if not codexlaunch.is_probed(line, platform):
        raise util.refuse(lead + codexlaunch.version_refusal(line, platform))
    return {"path": None, "identity": None, "version": line}


# ---------------------------------------------------------------- the box check (a box is opened only by descriptor)

class BoxOdd(Exception):
    def __init__(self, check: str, path: str):
        super().__init__(path)
        self.check, self.path = check, path


def open_box(record: dict) -> int:
    """Open the box with O_DIRECTORY | O_NOFOLLOW, compare device and inode with the record, and fstatat the two
    children `checkout` and `checkout/.git` without following them. Raises BoxOdd(check, path)."""
    box = str(record["box"])
    try:
        entry = os.lstat(box)
    except FileNotFoundError:
        raise BoxOdd("missing", box)
    if not stat.S_ISDIR(entry.st_mode):
        raise BoxOdd("1", box)
    try:
        fd = os.open(box, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise BoxOdd("1", box)
    found = os.fstat(fd)
    if [found.st_dev, found.st_ino] != [record.get("box_device"), record.get("box_inode")]:
        os.close(fd)
        raise BoxOdd("1", box)
    checkout = None
    try:
        # `checkout` is opened without following a link and `.git` is looked at from that descriptor, so a
        # checkout swapped for a link after its stat is never walked through.
        for child, parent in (("checkout", "box"), (".git", "checkout")):
            name = "checkout/.git" if child == ".git" else child
            where = fd if parent == "box" else checkout
            try:
                entry = os.stat(child, dir_fd=where, follow_symlinks=False)
            except OSError:
                raise BoxOdd("2", name)
            if stat.S_ISLNK(entry.st_mode) or (child == ".git" and stat.S_ISREG(entry.st_mode)):
                raise BoxOdd("3a", name)
            if not stat.S_ISDIR(entry.st_mode):
                raise BoxOdd("2", name)
            if child == "checkout":
                try:
                    checkout = os.open(child, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                       dir_fd=fd)
                except OSError:
                    raise BoxOdd("3a", name)
                opened = os.fstat(checkout)
                if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
                    raise BoxOdd("3a", name)
    except BaseException:
        os.close(fd)
        raise
    finally:
        if checkout is not None:
            os.close(checkout)
    return fd


# ---------------------------------------------------------------- the runner

TRAMPOLINE = ("import os,sys\nfd=int(sys.argv[1])\nos.fchdir(fd)\nos.close(fd)\n"
              "os.execv(sys.argv[2],sys.argv[2:])\n")


class TenderRun:
    def __init__(self, code, lines, stdout, stderr, timed_out):
        self.code, self.lines, self.stdout, self.stderr, self.timed_out = code, lines, stdout, stderr, timed_out

    def walk(self) -> dict | None:
        return next((line["walk"] for line in self.lines if "walk" in line), None)

    def result(self) -> dict | None:
        return next((line for line in reversed(self.lines) if line.get("result_line")), None)


def helper_codex_home() -> Path:
    """A static Yggdrasil-written hermetic CODEX_HOME for helpers: only a config of fixed settings."""
    home = util.ensure_dir(state.root() / "codex-helper-home")
    os.chmod(home, 0o700)
    config = home / "config.toml"
    text = ('approval_policy = "never"\nsandbox_mode = "workspace-write"\nweb_search = "disabled"\n'
            "[sandbox_workspace_write]\nwritable_roots = []\nnetwork_access = false\nexclude_slash_tmp = true\n"
            "exclude_tmpdir_env_var = true\n[agents]\nenabled = false\n[skills]\ninclude_instructions = false\n")
    if util.read_text(config) != text:
        util.atomic_write(config, text, mode=0o600)
    return home


def _global_config() -> str:
    return str(state.root() / "git" / "global.gitconfig")


def tender_argv(mode: str, spec_text: str, sandbox_mode: str, codex: str | None) -> list[str]:
    own = [sys.executable, "-I", "-m", "yggdrasil", "_tend", mode, spec_text]
    if util.test_mode():
        return own
    argv = [codex, "sandbox", "-c", 'sandbox_mode="' + sandbox_mode + '"']
    for key in SANDBOX_KEYS:
        argv += ["-c", key]
    return argv + ["--"] + own


def tender_env(record: dict) -> dict:
    """The sandboxed helper's whole environment: named values only, so no key (CODEX_API_KEY, OPENAI_API_KEY,
    CODEX_ACCESS_TOKEN, a provider's key) ever reaches it."""
    environment = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    environment.update(TMPDIR=str(record["box"]) + "/tmp", PYTHONDONTWRITEBYTECODE="1",
                       YGG_STATE_ROOT=str(state.root()))
    if util.test_mode():
        environment["YGG_TEST_MODE"] = "1"
        if os.environ.get("YGG_FAULT_AT"):
            environment["YGG_FAULT_AT"] = os.environ["YGG_FAULT_AT"]
    else:
        environment["CODEX_HOME"] = str(helper_codex_home())
    return environment


def run_tender(record: dict, mode: str, spec: dict, *, timeout: float, pack_sink=None) -> TenderRun:
    """Check the version, open the box by descriptor, and start the tender in it from that descriptor, with a
    minimal environment, its own process group and wall-clock and output limits.
    `pack_sink` (pack mode): a callable given the tender's standard output stream; its output is not parsed."""
    pinned = check_codex("re-check refused")
    fd = open_box(record)
    try:
        spec = dict(spec, global_config=_global_config())
        sandbox_mode = "read-only" if mode in ("report", "pack") else "workspace-write"
        argv = tender_argv(mode, json.dumps(spec, sort_keys=True), sandbox_mode, pinned.get("path"))
        if pinned.get("path") and not codexlaunch.same_binary(pinned):
            raise util.refuse("re-check refused: " + codexlaunch.version_refusal(
                codexlaunch.UNKNOWN_VERSION, codexlaunch.current_platform()))
        environment = tender_env(record)
        process = subprocess.Popen([sys.executable, "-I", "-c", TRAMPOLINE, str(fd), *argv],
                                   cwd=str(state.root()), env=environment, pass_fds=(fd,),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
    finally:
        os.close(fd)
    return _collect(process, timeout, pack_sink)


def _kill_group(process) -> None:
    try:
        os.killpg(process.pid, 9)
    except (ProcessLookupError, PermissionError):
        pass


def _collect(process, timeout: float, pack_sink) -> TenderRun:
    chunks = {"out": [], "err": []}
    sizes = {"out": 0, "err": 0}

    def drain(stream, key, limit):
        while True:
            data = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not data:
                return
            if sizes[key] < limit:
                chunks[key].append(data[:limit - sizes[key]])
            sizes[key] += len(data)

    readers = [threading.Thread(target=drain, args=(process.stderr, "err", OUTPUT_LIMIT), daemon=True)]
    sink_error = []
    if pack_sink is None:
        readers.append(threading.Thread(target=drain, args=(process.stdout, "out", OUTPUT_LIMIT), daemon=True))
    else:
        def sink():
            try:
                pack_sink(process.stdout)
            except BaseException as error:  # noqa: BLE001  (reported to the caller)
                sink_error.append(error)
                _kill_group(process)
        readers.append(threading.Thread(target=sink, daemon=True))
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    # The tender's process group ends with it: a process it left behind is killed too.
    _kill_group(process)
    if timed_out:
        process.wait()
    for reader in readers:
        reader.join(timeout=5)
    if sink_error and not timed_out:
        raise sink_error[0]
    stdout = b"".join(chunks["out"]).decode("utf-8", errors="replace")
    lines = []
    for line in stdout.splitlines():
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                lines.append(value)
    stderr = b"".join(chunks["err"]).decode("utf-8", errors="replace")
    if pack_sink is not None:
        for line in stderr.splitlines():
            if line.startswith("{"):
                try:
                    lines.append(json.loads(line))
                except ValueError:
                    pass
    return TenderRun(process.returncode, lines, stdout, stderr, timed_out)


def base_spec(record: dict) -> dict:
    return {"branch": record["branch"], "format": record.get("format"), "given_head": record.get("given_head"),
            "given_ref": record.get("given_ref"), "feeding": record.get("feeding")}


def odd_text(odd: dict) -> str:
    """The receive and sync refusal for an odd clone. The path can be a file name from the box, so it is shown
    escaped and cannot start a line of its own."""
    kind, path = odd.get("kind"), _shown(odd.get("path") or "")
    if kind == "special":
        text = "re-check refused: the clone's git folder holds links or special files (" + path + ")"
    elif kind == "unchecked":
        text = "re-check refused: the clone's git folder could not be checked (" + path + ")"
    elif kind == "ancestry":
        text = "re-check refused: the clone's git folder overrides its history (" + path + ")"
    elif kind == "format":
        text = "re-check refused: the clone's repository format is not the one Yggdrasil made (" + path + ")"
    elif kind == "outside":
        text = "re-check refused: the clone depends on something outside it (" + path + ")"
    elif kind == "unnamed":
        text = "re-check refused: the clone could not be read (" + path + ")"
    else:
        text = "re-check refused: the clone is not as Yggdrasil left it (" + path + ")"
    return text + REPLACE_SUFFIX


def odd_reason(odd: dict) -> str:
    """The quarantine reason of an odd clone: `odd: <the first failing check and path>`."""
    labels = {"special": "links or special files", "unchecked": "could not be checked", "ancestry":
              "overrides its history", "format": "repository format", "outside": "depends on something outside it",
              "tmp": "temporary folder is not a plain folder", "given": "given commit", "box": "not a folder"}
    path = str(odd.get("path") or "")
    if odd.get("kind") == "unchecked" and path.startswith("could not be checked: "):
        detail = path
    else:
        detail = labels.get(str(odd.get("kind")), str(odd.get("kind") or "")) + " (" + path + ")"
    return "odd: check " + str(odd.get("check")) + ": " + detail


def _box_odd(error: BoxOdd) -> dict:
    return {"result": "odd", "check": error.check if error.check != "missing" else "1", "kind": "box",
            "path": error.path}


def note_timeout(project: str, lane: str) -> None:
    """A tender run on the lane that ended as check 5: its time in the record's `timeouts` (last 24 hours)."""
    with util.locked():
        record = read(project, lane)
        if not record:
            return
        cutoff = util.now().timestamp() - 86400  # Yggdrasil's clock, as `at` and `timeouts` are written
        kept = [t for t in record.get("timeouts") or [] if util.parse_time(str(t)).timestamp() > cutoff]
        record["timeouts"] = kept + [util.iso()]
        write(project, lane, record)
    from . import quarantine
    quarantine.timeout_alert(project, lane, len(record["timeouts"]))  # every third in 24 hours


def tend(record: dict, mode: str, spec: dict, timeout: float) -> dict:
    """Run one tender mode and return its result: the mode's last line, `odd` from the walk line when the
    result did not arrive, or check 5 (`unnamed`) with `timed_out`."""
    try:
        run = run_tender(record, mode, dict(base_spec(record), **spec), timeout=timeout)
    except BoxOdd as error:
        return _box_odd(error)
    result = run.result()
    if result is not None and not run.timed_out:
        return result
    missing = [line for line in run.stdout.splitlines() if line.startswith("missing: ")]
    if run.code == 3 and missing and not run.timed_out:
        return {"result": "missing", "commit": missing[0].split()[1]}
    if run.code == 97:
        return {"result": "fault"}
    return {"result": "odd", "check": "5", "kind": "unnamed", "timed_out": run.timed_out,
            "path": "the tender " + ("ran out of time" if run.timed_out else "exited " + str(run.code))}


# ---------------------------------------------------------------- assembly

def clone_config(project: str, record: dict, home: str, format_text: str, hooks: Path | None = None) -> str:
    """The clone's config, written whole by Yggdrasil: the repository format assembly found, Yggdrasil's hooks
    folder (a test clone: `hooks`, an empty Yggdrasil-owned folder), the profile identity, no signing or editors,
    the home and the approved remote URL."""
    from . import config, launcher
    lines = {"core": [], "extensions": []}
    for line in str(format_text).splitlines():
        key, _, value = line.strip().partition(" ")
        section, _, name = key.partition(".")
        if section in lines and name:
            lines[section].append((name, value))
    if not any(name == "repositoryformatversion" for name, _ in lines["core"]):
        lines["core"].insert(0, ("repositoryformatversion", "0"))
    lines["core"] += [("bare", "false"), ("logallrefupdates", "true"), ("hooksPath", str(hooks or launcher.hooks_root()))]
    sections = [("core", None, lines["core"])]
    if lines["extensions"]:
        sections.append(("extensions", None, lines["extensions"]))
    identity = config.get("commit_identity") or {}
    user = [(k, identity[k]) for k in ("name", "email") if isinstance(identity, dict) and identity.get(k)]
    if user:
        sections.append(("user", None, user))
    sections += [("commit", None, [("gpgsign", "false")]), ("core", None, [("editor", "true")]),
                 ("sequence", None, [("editor", "true")]), ("ygg", None, [("home", str(home)), ("clone", "true")])]
    url = approved_remote(project, home)
    if url:
        sections.append(("remote", "origin", [("url", url)]))
    text = ""
    for section, sub, values in sections:
        text += "[" + section + (' "' + sub + '"' if sub else "") + "]\n"
        for name, value in values:
            text += "\t" + name + " = " + _quote(str(value)) + "\n"
    return text


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'


def approved_remote(project: str, home: str) -> str | None:
    from . import gitops
    recorded = gitops._recorded_for(project, home, "remote_at_approval") or {}
    urls = recorded.get("origin") if isinstance(recorded, dict) else None
    return str(urls[0]) if urls else None


def _pipe_pack(home: str, clone: Path, revisions: str, deadline: float) -> None:
    """Trusted `pack-objects` in the home piped into `index-pack --strict --stdin` in the new clone; both must
    exit 0 within the limit. No file is named, no transport is used."""
    environment = util.git_env()
    packer = subprocess.Popen(["git", *util.GIT_SAFETY, "-C", home, "pack-objects", "-q", "--revs", "--stdout",
                               "--delta-base-offset"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=environment, start_new_session=True)
    indexer = subprocess.Popen(["git", *util.GIT_SAFETY, "-c", "core.commitGraph=false", "-C", str(clone),
                                "index-pack", "--strict", "--stdin"], stdin=packer.stdout,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=environment,
                               start_new_session=True)
    packer.stdout.close()
    try:
        packer.stdin.write(revisions.encode())
        packer.stdin.close()
    except BrokenPipeError:
        pass
    statuses = []
    for process in (packer, indexer):
        try:
            statuses.append(process.wait(timeout=max(1.0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            for other in (packer, indexer):
                _kill_group(other)
            statuses.append(None)
    for process in (packer, indexer):
        process.stderr.close()
    if statuses != [0, 0]:
        raise _AssemblyFailed()


class _AssemblyFailed(Exception):
    pass


def _clone_git(clone: Path, *args) -> subprocess.CompletedProcess:
    return git("-c", "core.commitGraph=false", "-C", clone, *args)


def _ensure_lane_branch(home: str, project: str, tl: str) -> str:
    """Under the journal lock: the lane branch exists in the home (made from the recorded creation point or the
    integration branch, as `ensure_worktree` makes it); its head."""
    from . import launcher
    branch = lane_name(project, tl)
    head = _rev(home, "refs/heads/" + branch)
    if head:
        return head
    base = "ygg/" + project + "/_integration"
    if not launcher.branch_exists(home, base):
        base = "main"
    intent = launcher._lane_creation_intent(branch)
    if intent and _rev(home, intent):
        base = intent
    made_from = _rev(home, base)
    if not made_from:
        raise util.refuse("launch precondition: cannot create lane " + branch + ": " + base + " does not exist")
    result = git("-C", home, "update-ref", "refs/heads/" + branch, made_from, "")
    if result.returncode != 0:
        raise util.refuse("launch precondition: cannot create lane " + branch + ": " + result.stderr.strip())
    launcher._record_lane_creation(branch, made_from)
    return made_from


def _assembly_hold() -> None:
    folder = os.environ.get("YGG_TEST_ASSEMBLY_HOLD") if util.test_mode() else None
    if not folder:
        return
    try:
        fd = os.open(os.path.join(folder, "entered-assembly"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        pass
    while not os.path.exists(os.path.join(folder, "release-assembly")):
        time.sleep(0.1)


def assemble(project: str, tl: str, home: str, *, at: str | None = None) -> dict:
    """A new box at the next generation. `at`: the drop point of a cancel.
    Under the lane lock; every record write after the first checks that this assembly's nonce owns it."""
    with lane_lock(project, tl):
        return _assemble(project, tl, home, at=at)


def _assemble(project: str, tl: str, home: str, *, at: str | None = None) -> dict:
    from . import launcher
    launcher.exclude_worktrees(home)
    branch = lane_name(project, tl)
    folder = util.ensure_dir(lane_folder(home, project))
    with util.locked():
        head = at or _ensure_lane_branch(home, project, tl)
        generation = next_generation(project, tl, home)
        if generation > GENERATION_MAX:
            raise util.refuse("launch precondition: lane " + branch + " has used every box generation")
        nonce = uuid.uuid4().hex
        previous = read(project, tl) or {}
        temporary = folder / (".creating-" + nonce)
        record = {"kind": "lane", "generation": generation, "box": str(temporary),
                  "path": str(temporary / "checkout"), "home": str(home), "branch": branch, "state": "creating",
                  "next": None, "reason": None, "at": None, "lane_head": None, "report": None,
                  "given_head": None, "given_ref": None, "feeding": None, "format": None,
                  "timeouts": list(previous.get("timeouts") or []), "nonce": nonce, "box_device": None,
                  "box_inode": None, "made_from": head, "created_at": util.iso(),
                  "replaced": previous.get("state") == "quarantining" or bool(previous.get("replaced_pending"))}
        write(project, tl, record)
    deadline = time.monotonic() + ASSEMBLY_SECONDS
    in_time = "launch precondition: the clone of lane " + branch + " could not be assembled in time"
    os.mkdir(temporary, 0o700)
    os.mkdir(temporary / ".git", 0o700)
    os.mkdir(temporary / "tmp", 0o700)
    os.mkdir(temporary / "tmp" / "cache", 0o700)
    clone = temporary / "checkout"
    made = git("init", "--template=", "-q", clone)
    if made.returncode != 0:
        raise util.refuse("launch precondition: cannot assemble the clone of lane " + branch + ": " +
                          made.stderr.strip())
    try:
        _pipe_pack(home, clone, head + "\n", deadline)
    except _AssemblyFailed:
        raise util.refuse(in_time)
    format_text = git("config", "--file", clone / ".git" / "config", "--get-regexp",
                      r"^(core\.repositoryformatversion|extensions\..*)$").stdout
    for args in (("update-ref", "refs/heads/" + branch, head), ("update-ref", "refs/ygg/given", head),
                 ("symbolic-ref", "HEAD", "refs/heads/" + branch)):
        result = _clone_git(clone, *args)
        if result.returncode != 0:
            raise util.refuse("launch precondition: cannot assemble the clone of lane " + branch + ": " +
                              result.stderr.strip())
    util.atomic_write(clone / ".git" / "config", clone_config(project, record, home, format_text))
    checked = _clone_git(clone, "read-tree", "-m", "-u", "HEAD")
    if checked.returncode != 0:
        raise util.refuse("launch precondition: cannot check out the clone of lane " + branch + ": " +
                          checked.stderr.strip())
    os.chmod(temporary / ".git", 0o500)  # the anchor: an empty folder, read-only
    found = os.lstat(temporary)
    with util.locked():
        record = _owned(project, tl, nonce)
        record.update(box_device=found.st_dev, box_inode=found.st_ino)
        write(project, tl, record)
    _assembly_hold()
    for attempt in range(4):
        with util.locked():
            now = head if at else _rev(home, "refs/heads/" + branch)
            if now == head:
                faults.boundary("clone-before-rename")
                final = folder / box_name(tl, generation)
                record = _owned(project, tl, nonce)
                os.rename(temporary, final)
                record.update(box=str(final), path=str(final / "checkout"))
                write(project, tl, record)
                break
        if attempt == 3:
            raise util.refuse("launch precondition: lane " + branch + " kept moving while its clone was assembled; "
                              "try again")
        try:
            _pipe_pack(home, clone, now + "\n--not\n" + head + "\n", deadline)
        except _AssemblyFailed:
            raise util.refuse(in_time)
        if _clone_git(clone, "rev-list", "--objects", "--quiet", now, "--not", head).returncode != 0:
            raise util.refuse(in_time)
        for args in (("update-ref", "refs/heads/" + branch, now), ("update-ref", "refs/ygg/given", now),
                     ("read-tree", "-m", "-u", head, now)):
            result = _clone_git(clone, *args)
            if result.returncode != 0:
                raise util.refuse("launch precondition: cannot assemble the clone of lane " + branch + ": " +
                                  result.stderr.strip())
        head = now
    faults.boundary("clone-after-rename")
    with util.locked():
        record = _owned(project, tl, nonce)
        record.update(state="ready", given_head=head, given_ref=head, feeding=None, format=format_text,
                      next=None, reason=None, at=None, lane_head=None, report=None)
        record.pop("replaced_pending", None)
        write(project, tl, record)
    return record


# ---------------------------------------------------------------- quarantine (the primitive; see the docstring)

def lane_nodes(tl: str) -> list[str]:
    nodes = state.all_nodes()
    return [tl] + sorted(n for n, v in nodes.items() if v.get("role") == "worker" and v.get("parent") == tl)


def lane_alive(tl: str, besides: str | None = None) -> bool:
    from . import launcher
    return any(launcher.live_activations(node) for node in lane_nodes(tl) if node != besides)


def report(record: dict, lane_head: str | None) -> dict:
    """The tender's `report` (read-only), best effort: `unavailable` on a failure or limit, `comparison commit
    missing` when the box lacks L."""
    result = tend(record, "report", {"lane_head": lane_head}, REPORT_SECONDS)
    return result


def _stored_report(result: dict) -> dict | str:
    if result.get("result") == "missing":
        return "comparison commit missing"
    if result.get("result") in ("ok",) or (result.get("result") == "odd" and result.get("counts")):
        return {k: result.get(k) for k in ("counts", "metadata", "size", "head", "lane_branch", "detached")}
    if result.get("result") == "odd" and result.get("walk"):
        return {"walk": result.get("walk"), "stopped": result.get("check")}
    return "unavailable"


def _same_box(project: str, tl: str, snapshot: dict, states: tuple) -> dict:
    """Under the journal lock: the live record when it is still the snapshot's box (the same generation and nonce)
    in one of `states`. Otherwise the step stops, so a quarantine or a recovery never acts on a newer generation."""
    current = read(project, tl)
    if not current or (current.get("generation"), current.get("nonce")) != \
            (snapshot.get("generation"), snapshot.get("nonce")) or current.get("state") not in states:
        raise util.refuse("the clone record of lane " + lane_name(project, tl) + " changed while it was being "
                          "quarantined; try again")
    return current


def quarantine(project: str, tl: str, *, reason: str, next_step: str, odd_check: str | None = None,
               at: str | None = None) -> None:
    """Quarantine in place: the box stays where it is; the tender reports what it saw; the live record becomes
    `quarantining`; the quarantine record is written create-only. The caller has checked that no activation of the
    lane runs. Then `do_next`."""
    record = read(project, tl)
    if not record:
        return
    home = str(record["home"])
    if record.get("state") != "quarantining":
        head = _rev(home, "refs/heads/" + record["branch"]) or None
        seen = {"result": "unavailable"}
        if record.get("box_device") is not None:
            seen = report(record, head)
        stored = _stored_report(seen)
        if seen.get("result") == "odd" and seen.get("check") == "5":
            note_timeout(project, tl)
        with util.locked():
            record = _same_box(project, tl, record, (record.get("state"),))
            now = _rev(home, "refs/heads/" + record["branch"]) or None
            if now != head:
                stored = "unavailable"
            record.update(state="quarantining", next=next_step, reason=reason, lane_head=now, report=stored,
                          at=at, odd_check=odd_check)
            write(project, tl, record)
    faults.boundary("quarantine-before-metadata")
    with util.locked():
        record = _same_box(project, tl, record, ("quarantining",))
        target = quarantine_dir(project) / (box_name(tl, record["generation"]) + ".json")
        if not target.exists():
            util.ensure_dir(target.parent)
            value = {"lane": record["branch"], "project": project, "team_lead": tl,
                     "box": Path(record["box"]).name, "path": record["box"], "generation": record["generation"],
                     "reason": record.get("reason"), "at": util.iso(), "lane_head": record.get("lane_head"),
                     "report": record.get("report"), "odd_check": record.get("odd_check")}
            util.write_json(target, value)
    faults.boundary("quarantine-after-metadata")


def do_next(project: str, tl: str) -> dict | None:
    """After a quarantine: `rebuild` assembles the next generation (at `at` when set); `close` leaves a
    tombstone. Returns the new record for a rebuild."""
    record = read(project, tl)
    if not record or record.get("state") != "quarantining":
        return record
    if record.get("next") == "close":
        with util.locked():
            record = _same_box(project, tl, record, ("quarantining",))
            record.update(state="closed")
            write(project, tl, record)
        return None
    with util.locked():
        record = _same_box(project, tl, record, ("quarantining",))
        record["replaced_pending"] = True
        write(project, tl, record)
    return assemble(project, tl, str(record["home"]), at=record.get("at"))


def missing_box(project: str, tl: str, record: dict) -> None:
    """A `ready` record whose box is gone: a quarantine record that names no folder, then a rebuild."""
    with util.locked():
        record = _same_box(project, tl, record, ("ready",))
        target = quarantine_dir(project) / (box_name(tl, record["generation"]) + ".json")
        if not target.exists():
            util.write_json(target, {"lane": record["branch"], "project": project, "team_lead": tl, "box": None,
                                     "path": None, "generation": record["generation"],
                                     "reason": "the box of lane " + record["branch"] + " was missing",
                                     "at": util.iso(), "lane_head": None, "report": "unavailable",
                                     "odd_check": None})
        record.update(state="quarantining", next="rebuild", reason="the box was missing")
        write(project, tl, record)


def cap_refusal(project: str, tl: str) -> str | None:
    """The rebuild cap: 3 boxes quarantined for checks 1 to 4 (3a included) in the last 24 hours, whose box is
    still in the lane folder."""
    cutoff = util.now().timestamp() - 86400  # Yggdrasil's clock, as `at` and `timeouts` are written
    counted = 0
    for value in quarantine_records(project, tl):
        if str(value.get("odd_check") or "") not in ("1", "2", "3", "3a", "4"):
            continue
        if not value.get("path") or not os.path.lexists(str(value["path"])):
            continue
        try:
            if util.parse_time(str(value.get("at"))).timestamp() <= cutoff:
                continue
        except (TypeError, ValueError):
            pass
        counted += 1
    if counted >= 3:
        from .quarantine import SECURITY_PAGE
        return ("launch precondition: lane " + lane_name(project, tl) + " replaced its clone 3 times in the last "
                "24 hours; it waits until one of those boxes is a day old or moved out of the lane folder; before "
                "you move a box, copy it with cp -RP and check that the copy is complete, as " + SECURITY_PAGE +
                " says; copy, do not move, a box that depends on something outside it (ygg status lists them and "
                "says how)")
    return None


# ---------------------------------------------------------------- tending at launch

def _replace_waits(project: str, tl: str) -> str:
    return ("launch precondition: the clone of lane " + lane_name(project, tl) + " must be replaced, which waits "
            "until no activation of the lane runs")


def _interrupted_cancel(project: str, tl: str):
    try:
        from . import lanedrop
    except ImportError:
        return None
    function = getattr(lanedrop, "interrupted", None)
    return function(project, tl) if function else None


_NOTES: dict = {}


def _note(activation: str | None, line: str, name: str = "clone-prompt.txt") -> None:
    """A line for the activation: `clone-prompt.txt` (read by the supervisor into the prompt) or `fast-forward`,
    written into `launch/<activation>/` by `flush_notes` once the launch folder exists."""
    if activation:
        _NOTES.setdefault(activation, {}).setdefault(name, []).append(line)


def flush_notes(activation: str) -> None:
    for name, lines in (_NOTES.pop(activation, None) or {}).items():
        util.atomic_write(state.root() / "launch" / activation / name, "".join(line + "\n" for line in lines))


def prepare_lane(node: dict, home: str, project: str, tl: str) -> None:
    """Before a launch reserves anything: assemble a missing lane clone and recover an interrupted record or a
    missing box, so a crash in assembly leaves no reservation behind. A `creating` record seen under the lane
    lock is interrupted: a running assembly holds that lock until it ends."""
    with lane_lock(project, tl):
        _prepare_lane(node, home, project, tl)


def _prepare_lane(node: dict, home: str, project: str, tl: str) -> None:
    record = read(project, tl)
    if record is None or record.get("state") == "closed":
        assemble(project, tl, home)
        return
    if record.get("state") in ("creating", "quarantining"):
        _recover(node, project, tl, record)
        return
    try:
        os.close(open_box(record))
    except BoxOdd as error:
        if error.check == "missing":
            missing_box(project, tl, record)
            do_next(project, tl)


def _recover(node: dict, project: str, tl: str, record: dict) -> dict | None:
    if _interrupted_cancel(project, tl):
        raise util.refuse("launch precondition: lane " + lane_name(project, tl) + " has an interrupted "
                          "cancel; finish it with ygg wp cancel " + project + " --finish " + tl)
    if lane_alive(tl, besides=node["id"]):
        raise util.refuse(_replace_waits(project, tl))
    if record["state"] == "creating":
        quarantine(project, tl, reason="assembly was interrupted", next_step="rebuild")
    else:
        quarantine(project, tl, reason=str(record.get("reason")), next_step=str(record.get("next")),
                   odd_check=record.get("odd_check"), at=record.get("at"))
    return do_next(project, tl)


def lane_checkout(node: dict, home: str, project: str, tl: str, activation: str | None = None) -> Path:
    """The working folder of a Codex team lead or worker: `<box>/checkout` of the lane's live box, assembled,
    recovered after a crash, replaced or tended first."""
    with lane_lock(project, tl):
        return _lane_checkout(node, home, project, tl, activation)


def _lane_checkout(node: dict, home: str, project: str, tl: str, activation: str | None) -> Path:
    for _ in range(4):
        record = read(project, tl)
        if record is None or record.get("state") == "closed":
            record = assemble(project, tl, home)
            break
        if record.get("state") in ("creating", "quarantining"):
            record = _recover(node, project, tl, record)
            break
        try:
            fd = open_box(record)
            os.close(fd)
        except BoxOdd as error:
            if error.check == "missing":
                missing_box(project, tl, record)
                record = do_next(project, tl)
                break
            record = _replace(node, project, tl, _box_odd(error), activation)
            break
        record = _tend_ready(node, project, tl, record, activation)
        break
    if record is None:
        raise util.refuse("launch precondition: lane " + lane_name(project, tl) + " is closed")
    if record.get("replaced"):
        _replaced_line(project, tl, record, activation)
    return Path(record["path"])


def _shown(text) -> str:
    """Text that can hold a file name from the box, made safe for a notice line: every character that is not
    printable is shown as its escape (a newline as \\n), so the name cannot start a line of its own."""
    return "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in str(text))


def _replaced_line(project: str, tl: str, record: dict, activation: str | None) -> None:
    previous = [r for r in quarantine_records(project, tl) if int(r.get("generation") or 0) < record["generation"]]
    if not previous or activation is None:
        return
    last = max(previous, key=lambda r: int(r.get("generation") or 0))
    if record.get("told") == activation or record.get("told_generation") == record["generation"]:
        return
    _note(activation, "Yggdrasil replaced your clone because " + _shown(last.get("reason")) + ". The old one is kept "
          "for the user at " + _shown(last.get("path")) + "; commits that ygg wp ready had not received are only "
          "there. Do not run git or other tools in it; tell the user.")
    with util.locked():
        current = read(project, tl) or record
        current["told_generation"] = record["generation"]
        current.pop("replaced", None)
        write(project, tl, current)


def _replace(node: dict, project: str, tl: str, odd: dict, activation: str | None) -> dict:
    if lane_alive(tl, besides=node["id"]):
        raise util.refuse(_replace_waits(project, tl))
    capped = cap_refusal(project, tl)
    if capped:
        raise util.refuse(capped)
    check = str(odd.get("check"))
    from . import quarantine as boxes
    boxes.end_orphans(tl)  # end the lane's leftover processes first, best effort
    quarantine(project, tl, reason=odd_reason(odd), next_step="rebuild",
               odd_check=check if check in ("1", "2", "3", "3a", "4", "5") else None)
    return do_next(project, tl)


def _tend_ready(node: dict, project: str, tl: str, record: dict, activation: str | None) -> dict:
    home = str(record["home"])
    branch = record["branch"]
    remove_feed_files(project, tl)
    sole = not lane_alive(tl, besides=node["id"])
    head = _rev(home, "refs/heads/" + branch)
    config_text = clone_config(project, record, home, record.get("format") or "")
    result = tend(record, "prepare", {"config": config_text, "lane_head": head, "sole": False}, REPORT_SECONDS)
    if result.get("result") == "odd":
        if result.get("check") == "5":
            note_timeout(project, tl)
        return _replace(node, project, tl, result, activation)
    if result.get("feed_complete"):
        complete_feed(project, tl, record, result.get("given"))
        record = read(project, tl) or record
    moved = False
    if head and record.get("given_head") != head:
        if feed(project, tl, home, record=record, new_head=head) is None:
            moved = True
        record = read(project, tl) or record
    if moved:
        _note(activation, _moved_line(head))
        return record
    result = tend(record, "prepare", {"config": config_text, "lane_head": head, "sole": sole}, REPORT_SECONDS)
    if result.get("result") == "odd":
        if result.get("check") == "5":
            note_timeout(project, tl)
        return _replace(node, project, tl, result, activation)
    if head and result.get("fast_forward") != "done":
        _note(activation, _moved_line(head))
        if result.get("fast_forward") == "refused":
            _fast_forward_refused(project, tl, result, activation)
    return record


def complete_feed(project: str, tl: str, snapshot: dict, confirmed: str | None) -> bool:
    """A feed a crash left open: complete it only for the exact head the tender confirmed in the box, and only
    while the record is still the one the tender checked (same generation, nonce and `feeding`)."""
    if not confirmed or confirmed != snapshot.get("feeding"):
        return False
    with util.locked():
        current = read(project, tl) or {}
        if (current.get("generation"), current.get("nonce"), current.get("feeding"), current.get("state")) != \
                (snapshot.get("generation"), snapshot.get("nonce"), confirmed, "ready"):
            return False
        current.update(given_head=confirmed, given_ref=confirmed, feeding=None)
        write(project, tl, current)
    return True


def _moved_line(head: str) -> str:
    return ("The lane moved to " + head[:12] + "; run git merge --ff-only --no-overwrite-ignore " + head +
            " in your checkout before your next ygg wp ready.")


def _fast_forward_refused(project: str, tl: str, result: dict, activation: str | None) -> None:
    paths = list(result.get("ignored") or [])
    if paths:
        shown = ", ".join(_shown(str(path)) for path in paths[:10])
        shown += " and " + str(len(paths) - 10) + " more" if len(paths) > 10 else ""
        line = ("the clone of " + lane_name(project, tl) + " was not fast-forwarded: the lane's new commits track "
                "ignored files in its checkout, which are kept: " + shown)
    else:
        line = ("the clone of " + lane_name(project, tl) + " was not fast-forwarded: git merge exited " +
                str(result.get("status")))
    print(line, file=sys.stderr)
    _note(activation, line, "fast-forward")


# ---------------------------------------------------------------- the feed (lane sync and tending)

def feed_dir(project: str) -> Path:
    return state.root() / "feed" / project


def remove_feed_files(project: str, tl: str) -> None:
    folder = feed_dir(project)
    if not folder.is_dir():
        return
    for name in os.listdir(folder):
        if name.startswith(tl + ".g") and name.endswith(".pack"):
            try:
                os.unlink(folder / name)
            except FileNotFoundError:
                pass


FEED_LINE = "the clone of {} gets the new commits at its next launch"


def feed(project: str, tl: str, home: str, *, record: dict | None = None, new_head: str | None = None) -> str | None:
    """Feed the live box the objects of `<lane head> --not <given_head>`: a trusted pack file made create-only
    under the state root, then the tender's `ingest`. Returns the new head, or None when the feed failed (the
    record keeps `given_head`, `given_ref` and `feeding` as they are). Under the lane lock."""
    with lane_lock(project, tl):
        return _feed(project, tl, home, record=read(project, tl) if record is None else record, new_head=new_head)


def _feed(project: str, tl: str, home: str, *, record: dict | None, new_head: str | None) -> str | None:
    if not record or record.get("state") != "ready":
        return None
    new_head = new_head or _rev(home, "refs/heads/" + record["branch"])
    if not new_head or new_head == record.get("given_head"):
        return new_head
    folder = util.ensure_dir(feed_dir(project))
    os.chmod(folder, 0o700)
    name = box_name(tl, record["generation"]) + "." + uuid.uuid4().hex + ".pack"
    directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    path = folder / name
    try:
        try:
            made = subprocess.run(["git", *util.GIT_SAFETY, "-C", home, "pack-objects", "-q", "--revs", "--stdout",
                                   "--delta-base-offset"],
                                  input=(new_head + "\n--not\n" + record["given_head"] + "\n").encode(),
                                  stdout=fd, stderr=subprocess.PIPE, env=util.git_env(), timeout=PACK_SECONDS,
                                  check=False)
            ok = made.returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        finally:
            os.close(fd)
        if not ok or os.path.getsize(path) > PACK_LIMIT:
            return None
        os.chmod(path, 0o644)
        with util.locked():
            current = read(project, tl) or {}
            if (current.get("generation"), current.get("nonce"), current.get("state")) != \
                    (record.get("generation"), record.get("nonce"), "ready"):
                return None
            current["feeding"] = new_head
            write(project, tl, current)
            record = current
        result = tend(record, "ingest", {"pack": str(path), "head": new_head}, PACK_SECONDS)
        if result.get("result") == "odd" and result.get("check") == "5":
            note_timeout(project, tl)
        if result.get("result") != "ingested":
            return None
        faults.boundary("feed-after-ingest")
        with util.locked():
            current = read(project, tl) or {}
            if (current.get("generation"), current.get("nonce"), current.get("feeding")) != \
                    (record.get("generation"), record.get("nonce"), new_head):
                return None
            current.update(given_head=new_head, given_ref=new_head, feeding=None)
            write(project, tl, current)
        return new_head
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------- the receive step

def _not_made_text(project: str, lane: str) -> str:
    return ("re-check refused: the clone of " + lane_name(project, lane) + " is not a folder Yggdrasil made; the next "
            "launch replaces it when no activation of the lane runs")


def _gates(project: str, lane: str, from_node: str | None, home: str, head: str, check_mail: bool) -> str | None:
    """The receive step's gates, under the journal lock: the home's lane head L0, or a refusal. Returns L0."""
    from . import tracker
    lane0 = _rev(home, "refs/heads/" + lane_name(project, lane))
    if check_mail and from_node:
        unread = [m for m in state.inbox(from_node) if not m.get("acked_at")]
        if unread:
            raise util.refuse("read your inbox first (" + str(len(unread)) + " unread): ygg inbox " + from_node)
    if head == lane0:
        return lane0
    project_record = tracker.project(project) or {}
    packages = [i for i in tracker.issues(project) if i.get("kind") == "work-package" and i.get("lane") == lane]
    # Owed a close: every package of the lane merged and the project ready to merge or closed (t3.owed_closes).
    closing = project_record.get("state") in ("ready-to-merge", "closed") and packages and \
        all(i.get("state") == "merged" for i in packages)
    if closing or project_record.get("state") in ("closed", "cancelled"):
        raise util.refuse("re-check refused: lane " + lane_name(project, lane) + " is merged; new work needs a new "
                          "package")
    for issue in tracker.issues(project):
        if issue.get("kind") == "work-package" and issue.get("lane") == lane and \
                issue.get("state") in ("in-review", "ready-for-test"):
            raise util.refuse("re-check refused: " + issue.id + " is being checked; the lane takes new commits "
                              "after its verdict")
    return lane0


def receive(project: str, lane: str, *, from_node: str | None = None) -> dict | None:
    """The receive step up to the final move: the box check, the tender's report, the gates, the import of the new
    commits, and the checks over them. Returns {"head": C, "lane0": L0, "generation", "nonce"} (the record the checks
    were made against) for the final move (`receive_commit`), or None when there is nothing to receive."""
    record = read(project, lane)
    if not record or record.get("state") != "ready":
        raise util.refuse(_not_made_text(project, lane))
    home = str(record["home"])
    try:
        os.close(open_box(record))
    except BoxOdd:
        raise util.refuse(_not_made_text(project, lane))
    lane0 = _rev(home, "refs/heads/" + record["branch"])
    result = tend(record, "report", {"lane_head": lane0 or None}, REPORT_SECONDS)
    _refuse_report(project, lane, result)
    head = str(result.get("lane_branch") or "")
    if not HEX.match(head):
        raise util.refuse("re-check refused: the clone's lane branch head is not a commit id")
    with util.locked():
        lane0 = _gates(project, lane, from_node, home, head, check_mail=from_node is not None)
    if head == lane0:
        return None
    _import(project, lane, record, home, head, lane0)
    faults.boundary("receive-after-import")
    connected = subprocess.run(["git", *util.GIT_SAFETY, "-C", home, "rev-list", "--objects", "--quiet", head,
                                "--not", lane0], capture_output=True, env=util.git_env(), timeout=300, check=False)
    if connected.returncode != 0:
        raise util.refuse("re-check refused: the clone's new commits are not complete")
    if git("-C", home, "merge-base", "--is-ancestor", lane0, head).returncode != 0:
        raise util.refuse("re-check refused: " + lane_name(project, lane) + " can only move forward; the clone's "
                          "head does not contain " + lane0[:12])
    _lane_check(project, lane, home, head, lane0)
    # The record these checks were made against: the final move lands only while that same record is still `ready`.
    return {"head": head, "lane0": lane0, "generation": record.get("generation"), "nonce": record.get("nonce")}


def _refuse_report(project: str, lane: str, result: dict) -> None:
    if result.get("result") == "missing":
        raise util.refuse(LACKS_NEWEST)
    if result.get("result") == "odd":
        if result.get("check") == "5":
            note_timeout(project, lane)
            if result.get("timed_out"):
                raise util.refuse(IN_TIME)
            raise util.refuse("re-check refused: the clone could not be read (" + _shown(result.get("path")) + ")" +
                              REPLACE_SUFFIX)
        if result.get("kind") == "box":
            raise util.refuse(_not_made_text(project, lane))
        raise util.refuse(odd_text(result))
    if result.get("result") != "ok":
        raise util.refuse(IN_TIME)


def _import(project: str, lane: str, record: dict, home: str, head: str, lane0: str) -> None:
    """The import: the tender's `pack` streams `head --not L0 given_head` into the home's `index-pack`."""
    lock = util.ensure_dir(_safety_locks()) / ("receive-" + project + "-" + lane + ".lock")
    import fcntl
    with open(lock, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        negatives = [lane0] + ([record["given_head"]] if record.get("given_head") and
                               record["given_head"] != lane0 else [])
        indexer = subprocess.Popen(["git", *util.GIT_SAFETY, "-C", home, "index-pack", "--stdin", "--strict",
                                    "--max-input-size=" + str(PACK_LIMIT)], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=util.git_env(),
                                   start_new_session=True)
        counted = {"bytes": 0, "over": False}

        def sink(stream):
            while True:
                data = stream.read(65536)
                if not data:
                    break
                counted["bytes"] += len(data)
                if counted["bytes"] > PACK_LIMIT:
                    counted["over"] = True
                    raise util.refuse("re-check refused: the new commits are larger than 512 MiB")
                indexer.stdin.write(data)
        try:
            run = run_tender(record, "pack", dict(base_spec(record), head=head, negatives=negatives),
                             timeout=PACK_SECONDS, pack_sink=sink)
        except BoxOdd:
            _kill_group(indexer)
            raise util.refuse(_not_made_text(project, lane))
        finally:
            try:
                indexer.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        if counted["over"]:
            _kill_group(indexer)
            raise util.refuse("re-check refused: the new commits are larger than 512 MiB")
        try:
            status = indexer.wait(timeout=PACK_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_group(indexer)
            status = None
        indexer.stderr.close()
        if run.code == 3:
            raise util.refuse(LACKS_NEWEST)
        odd = next((line for line in run.lines if line.get("result") == "odd"), None)
        if odd:
            raise util.refuse(odd_text(odd))
        if run.timed_out or status is None:
            note_timeout(project, lane)
            raise util.refuse(IN_TIME)
        if run.code != 0 or status != 0:
            raise util.refuse("re-check refused: the clone's new commits could not be imported")


def _safety_locks() -> Path:
    from . import safety
    return safety.layout() / "locks"


def _lane_check(project: str, lane: str, home: str, head: str, lane0: str) -> None:
    """The lane check run again in the home over the commits in C not reachable from L0 or main, plus the
    `.lfsconfig` refusal."""
    from . import lanecheck
    try:
        problems = lanecheck.recheck_problems(project, lane, home, _lane_packages(project, lane), head,
                                              since=lane0)
    except lanecheck.CheckError as error:
        problems = [str(error)]
    main = _rev(home, "refs/heads/main")
    listed = git("-C", home, "rev-list", head, "^" + lane0, *(["^" + main] if main else [])).stdout.split()
    for sha in listed:
        changed = git("-C", home, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root",
                      "-m", sha).stdout.split("\0")
        if any(path.rsplit("/", 1)[-1].casefold() == ".lfsconfig" for path in changed if path):
            problems.append(sha[:12] + ": .lfsconfig files are refused on Codex lanes")
    if problems:
        raise util.refuse(lanecheck.RECHECK + ": " + "; ".join(problems))


def _lane_packages(project: str, lane: str) -> list[str]:
    from . import tracker
    return [issue.id for issue in tracker.issues(project)
            if issue.get("kind") == "work-package" and issue.get("lane") == lane]


def receive_commit(project: str, lane: str, found: dict, *, from_node: str | None = None) -> None:
    """The final move, under the lane lock and then the journal lock the caller holds: the record the checks were made
    against must still be the lane's `ready` record (a cancel, cleanup or replacement in between builds a new
    generation at the same lane head), then the lane head and the gates are read again, then the compare-and-swap
    of the one lane ref, and `given_head` becomes C."""
    if not util.lock_held("lane-clone-" + project + "-" + lane) or not util.lock_held("journal"):
        raise RuntimeError("the receive step's final hold takes the lane lock, then the journal lock")
    record = read(project, lane) or {}
    if (record.get("generation"), record.get("nonce"), record.get("state")) != \
            (found.get("generation"), found.get("nonce"), "ready"):
        raise util.refuse("re-check refused: the clone of " + lane_name(project, lane) + " was replaced while its "
                          "new commits were checked; the next launch brings the lane's current head")
    home = str(record.get("home"))
    faults.boundary("receive-before-update")
    now = _rev(home, "refs/heads/" + lane_name(project, lane))
    if now != found["lane0"]:
        raise util.refuse("re-check refused: the lane moved while its new commits were checked; run it again")
    _gates(project, lane, from_node, home, found["head"], check_mail=from_node is not None)
    moved = git("-C", home, "update-ref", "refs/heads/" + lane_name(project, lane), found["head"], found["lane0"])
    if moved.returncode != 0:
        raise util.refuse("re-check refused: the lane moved while its new commits were checked; run it again")
    record["given_head"] = found["head"]
    write(project, lane, record)


# ---------------------------------------------------------------- lane sync

def sync_precheck(project: str, lane: str) -> None:
    """Before today's sync on a clone lane: no activation of the lane runs; the clone is not odd, holds the
    comparison commit and has no uncommitted change; then the receive step."""
    if lane_alive(lane):
        raise util.refuse("lane sync waits: an activation of lane " + lane_name(project, lane) + " is running")
    record = read(project, lane)
    if not record or record.get("state") != "ready":
        raise util.refuse(_not_made_text(project, lane))
    try:
        os.close(open_box(record))
    except BoxOdd:
        raise util.refuse(_not_made_text(project, lane))
    lane0 = _rev(str(record["home"]), "refs/heads/" + record["branch"])
    result = tend(record, "report", {"lane_head": lane0 or None}, REPORT_SECONDS)
    _refuse_report(project, lane, result)
    counts = result.get("counts") or {}
    if (counts.get("changed") or 0) > 0 or (counts.get("untracked") or 0) > 0:
        raise util.refuse("lane sync waits: the clone of " + lane_name(project, lane) + " has uncommitted changes")
    found = receive(project, lane)
    if found:
        with lane_lock(project, lane), util.locked():
            receive_commit(project, lane, found)


def sync_feed(project: str, lane: str) -> str:
    """After today's sync: feed the box; a failed feed adds the feed line and the sync still succeeds."""
    record = read(project, lane)
    if not record:
        return ""
    home = str(record["home"])
    if feed(project, lane, home, record=record) is None:
        return FEED_LINE.format(lane_name(project, lane)) + "\n"
    return ""


# ---------------------------------------------------------------- test clones (a testing Codex senior user)
#
# A senior user with a package in test works in a fresh test clone of the lane head, made for its activation and
# never received: `<home>/.ygg-worktrees/<project>/test/<senior user>.<activation>`, record
# `test-<senior user>.<activation>` (states `creating`, `ready`, `removing`). Its commits stay in the box, which
# is renamed into `<home>/.ygg-worktrees/<project>/_removing/` and deleted once the activation has settled
# without following links. Nothing here opens, reads or runs git in a box an agent has run in.

TEST_PREFIX = "test-"


def test_folder(home: str, project: str) -> Path:
    return Path(home) / ".ygg-worktrees" / project / "test"


def removing_folder(home: str, project: str) -> Path:
    return Path(home) / ".ygg-worktrees" / project / "_removing"


def test_box_name(su: str, activation: str) -> str:
    util.check_id(su, "node")
    util.check_id(activation, "activation")
    return su + "." + activation


def test_record_name(su: str, activation: str) -> str:
    return TEST_PREFIX + test_box_name(su, activation)


def empty_hooks_root() -> Path:
    """The empty Yggdrasil-owned folder a test clone's `core.hooksPath` names: no hook runs in a test clone."""
    from . import safety
    return safety.root() / "empty-hooks"


def _empty_hooks() -> Path:
    folder = empty_hooks_root()
    util.ensure_dir(folder.parent)
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass
    found = os.lstat(folder)
    if not stat.S_ISDIR(found.st_mode) or os.listdir(folder):
        raise util.refuse("launch precondition: " + str(folder) + " must be an empty folder")
    return folder


def test_records(project: str) -> list[tuple[str, dict]]:
    """(name, record) of every test-clone record of the project."""
    folder = registry_dir(project)
    found = []
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob(TEST_PREFIX + "*.json")):
        value = util.read_json(path, None)
        if isinstance(value, dict) and value.get("kind") == "test":
            found.append((path.name[:-len(".json")], value))
    return found


def test_box(node: dict, nodes: dict, activation: str, target: dict) -> dict:
    """The work area of a testing Codex senior user (the launcher's TEST_BOX slot): a fresh test clone of
    `target["branch"]`, detached at its head in the home."""
    from . import tracker
    programme = tracker.programme(node["programme"])
    home = str(tracker.programme_home(programme))
    record = assemble_test(node["project"], node["id"], activation, home, str(target["branch"]),
                           programme=node["programme"])
    return box_fields(record, test_prompt(record, target))


def box_fields(record: dict, prompt: list[str], project: str | None = None, name: str | None = None) -> dict:
    """The work-area dict of the launcher's slots; `pin` is what the launch records in the receipt as `clone`."""
    return {"box": record["box"], "checkout": record["path"], "box_device": record["box_device"],
            "box_inode": record["box_inode"], "prompt": list(prompt), "kind": record["kind"],
            "pin": pin(record, project, name)}


def test_prompt(record: dict, target: dict | None = None) -> list[str]:
    package = (target or {}).get("package")
    return ["Your test checkout is " + str(record["path"]) + ", detached at " + str(record["made_from"])[:12] +
            (" (the lane head of " + str(package) + ")" if package else "") + ". It is yours for this activation "
            "only: nothing you commit or change there ever reaches the project, and it is deleted when this "
            "activation ends. Your verdict reaches the project only through ygg acceptance record."]


def _test_owned(project: str, name: str, nonce: str) -> dict:
    current = read(project, name)
    if not current or current.get("nonce") != nonce or current.get("state") != "creating":
        raise util.refuse("launch precondition: the test clone " + name + " is no longer this launch's own")
    return current


def assemble_test(project: str, su: str, activation: str, home: str, branch: str, *, programme=None) -> dict:
    """A test clone at the head of `branch`, assembled the same way as a lane clone, with
    `core.hooksPath` set to an empty Yggdrasil-owned folder and HEAD detached. No transport, no alternates."""
    from . import launcher
    name = test_record_name(su, activation)
    launcher.exclude_worktrees(home)
    folder = util.ensure_dir(test_folder(home, project))
    hooks = _empty_hooks()
    with util.locked():
        if read(project, name) is not None:
            raise util.refuse("launch precondition: activation " + activation + " already has a test clone")
        head = _rev(home, "refs/heads/" + branch)
        if not head:
            raise util.refuse("launch precondition: cannot make a test clone of " + branch + ": it does not exist")
        nonce = uuid.uuid4().hex
        temporary = folder / (".creating-" + nonce)
        record = {"kind": "test", "node": su, "activation": activation, "programme": programme,
                  "project": project, "box": str(temporary), "path": str(temporary / "checkout"),
                  "home": str(home), "branch": branch,
                  "state": "creating", "nonce": nonce, "box_device": None, "box_inode": None, "format": None,
                  "made_from": head, "created_at": util.iso()}
        write(project, name, record)
    refused = "launch precondition: cannot make the test clone of " + branch + ": "
    os.mkdir(temporary, 0o700)
    os.mkdir(temporary / ".git", 0o700)
    os.mkdir(temporary / "tmp", 0o700)
    os.mkdir(temporary / "tmp" / "cache", 0o700)
    clone = temporary / "checkout"
    made = git("init", "--template=", "-q", clone)
    if made.returncode != 0:
        raise util.refuse(refused + made.stderr.strip())
    try:
        _pipe_pack(home, clone, head + "\n", time.monotonic() + ASSEMBLY_SECONDS)
    except _AssemblyFailed:
        raise util.refuse("launch precondition: the test clone of " + branch + " could not be assembled in time")
    format_text = git("config", "--file", clone / ".git" / "config", "--get-regexp",
                      r"^(core\.repositoryformatversion|extensions\..*)$").stdout
    result = _clone_git(clone, "update-ref", "--no-deref", "HEAD", head)
    if result.returncode != 0:
        raise util.refuse(refused + result.stderr.strip())
    util.atomic_write(clone / ".git" / "config", clone_config(project, record, home, format_text, hooks=hooks))
    checked = _clone_git(clone, "read-tree", "-m", "-u", "HEAD")
    if checked.returncode != 0:
        raise util.refuse(refused + checked.stderr.strip())
    os.chmod(temporary / ".git", 0o500)  # the anchor: an empty folder, read-only
    found = os.lstat(temporary)
    with util.locked():
        record = _test_owned(project, name, nonce)
        record.update(box_device=found.st_dev, box_inode=found.st_ino, format=format_text)
        write(project, name, record)
        faults.boundary("test-clone-before-rename")
        final = folder / test_box_name(su, activation)
        os.rename(temporary, final)
        record.update(box=str(final), path=str(final / "checkout"), state="ready")
        write(project, name, record)
    return record


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | \
    getattr(os, "O_NONBLOCK", 0)


def _open_up_at(dir_fd: int, name: str) -> None:
    """Give the folder `name` under the open folder `dir_fd`, and every folder below it, mode 0o700, by
    descriptor: each folder's mode is changed through its parent's descriptor without following a link, then it is
    opened without following a link and checked to be the folder that was changed, and only then is it read. A
    link is never followed, so nothing outside the tree is changed."""
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        return
    os.chmod(name, 0o700, dir_fd=dir_fd, follow_symlinks=False)
    fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise OSError(errno.ESTALE, "a folder changed while it was opened up", name)
        os.fchmod(fd, 0o700)
        with os.scandir(fd) as entries:
            folders = [entry.name for entry in entries if entry.is_dir(follow_symlinks=False)]
        for child in folders:
            _open_up_at(fd, child)
    finally:
        os.close(fd)


def _rmtree(path: Path) -> bool:
    """Remove a folder inside a `_removing` folder without following any link; a host whose rmtree does not
    avoid symlink attacks holds the folder instead (invariant T rule 3). An agent can leave folders it made
    unreadable or read-only, so every folder of the tree is first given mode 0o700 by descriptor (the `_removing`
    folder is ours, and no link is followed). Any failure holds the folder: False, never an exception."""
    try:
        if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            return False
        if os.chmod in os.supports_dir_fd and os.chmod in os.supports_follow_symlinks:
            parent = os.open(path.parent, _DIR_FLAGS)
            try:
                _open_up_at(parent, path.name)
            except FileNotFoundError:
                pass
            finally:
                os.close(parent)
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        return not _present(path)  # an access error is a failure, never read as "gone"
    except Exception:  # noqa: BLE001
        return False


def _own_entry(path: Path, record: dict, temporary: bool) -> bool:
    """Whether the entry at `path` is the folder the record names: a real folder (lstat, never followed) with the
    recorded device and inode; an assembly's temporary folder, named by the record's nonce, also before any
    identity was recorded."""
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(found.st_mode):
        return False
    if record.get("box_device") is None and record.get("box_inode") is None:
        return temporary
    return (found.st_dev, found.st_ino) == (record.get("box_device"), record.get("box_inode"))


NONCE = re.compile(r"^[0-9a-f]{32}$")


def _test_home(record: dict) -> str | None:
    """The home a test-clone record's paths are built from: the programme's registered home, never the record's
    own text, and only when the record's ids and nonce are well formed (defence in depth: no Codex agent can
    write the state root, and still nothing here trusts a path read from it)."""
    from . import tracker
    try:
        util.check_id(str(record.get("node")), "node")
        util.check_id(str(record.get("activation")), "activation")
        programme = tracker.programme(str(record.get("programme")))
        home = str(tracker.programme_home(programme))
    except (util.YggError, KeyError, TypeError):
        return None
    if not NONCE.match(str(record.get("nonce") or "")) or home != str(record.get("home")):
        return None
    return home


HELD_LEFT = "the folder at the box's name is not the test clone Yggdrasil made; left in place"
HELD_STUCK = "could not be removed from "
HELD_UNMOVED = "could not be moved into "
HELD_PENDING = "removal started but not finished; it is tried again on every tick"
HELD_UNCHECKED = "could not be checked in "


def _present(path) -> bool:
    """Whether an entry exists at `path`, without following a link. Only a missing entry counts as absent: an
    access error is raised, so a removal never mistakes a folder it cannot search for an empty one."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _hold(project: str, name: str, record: dict, target: Path | None, unmoved: bool = False,
          unchecked: tuple | None = None) -> None:
    """Record why a test clone's removal is held, for `ygg status`: a folder left in place (target None), a box
    that could not be renamed into the `_removing` folder (unmoved), or the `_removing` folder a removal could not
    delete. The text names only Yggdrasil's own paths and words."""
    if unchecked:
        reason = HELD_UNCHECKED + " or ".join(str(path) for path in unchecked) + "; it is tried again on every tick"
    elif target is None:
        reason = HELD_LEFT
    elif unmoved:
        reason = HELD_UNMOVED + str(target.parent) + "; it is tried again on every tick"
    else:
        reason = HELD_STUCK + str(target) + "; it is tried again on every tick"
    with util.locked():
        current = read(project, name)
        if current and current.get("state") == "removing" and current.get("nonce") == record.get("nonce") and \
                (target is not None or unchecked or not current.get("reason")):
            if current.get("reason") != reason:
                current["reason"] = reason
                write(project, name, current)


def held_test_clones(project: str) -> list[tuple[str, dict]]:
    """(name, record) of each test clone of the project whose removal is held, with its reason. A record left
    `removing` with no reason (a removal that stopped before it could record one) is shown too, as pending."""
    held = []
    for name, record in test_records(project):
        if record.get("state") != "removing":
            continue
        if not record.get("reason"):
            record = dict(record, reason=HELD_PENDING)
        held.append((name, record))
    return held


def remove_test_clone(project: str, name: str) -> bool:
    """Rename a test clone's box (and a temporary folder an interrupted assembly left) into `_removing/`, delete
    it there, then the record. True when the record is gone. A folder at the box's name that is not the recorded
    one is left where it is (`ygg status` shows it as an unknown folder) and the record stays `removing`."""
    with util.locked():
        record = read(project, name)
        if not record or record.get("kind") != "test":
            return True
        if record.get("state") != "removing":
            record["state"] = "removing"
            write(project, name, record)
    home = _test_home(record)
    if home is None:
        return False  # a record that names no trusted home is never acted on
    su, activation = str(record["node"]), str(record["activation"])
    folder = test_folder(home, project)
    removing = removing_folder(home, project)
    entries = [(test_box_name(su, activation), False), (".creating-" + str(record["nonce"]), True)]
    kept = False
    for entry, temporary in entries:
        source = folder / entry
        target = removing / entry
        try:
            target_present = _present(target)
            source_present = _present(source)
        except OSError:
            _hold(project, name, record, None, unchecked=(folder, removing))
            return False
        if target_present and not _rmtree(target):
            _hold(project, name, record, target)  # an earlier removal stopped after its rename
            return False
        if not source_present:
            continue
        try:
            own = _own_entry(source, record, temporary)
        except OSError:
            _hold(project, name, record, None, unchecked=(folder,))
            return False
        if not own:
            kept = True
            continue
        try:
            util.ensure_dir(removing)
            faults.boundary("test-clone-before-removing")
            os.rename(source, target)
        except OSError:
            _hold(project, name, record, target, unmoved=True)
            return False
        if not _rmtree(target):
            _hold(project, name, record, target)
            return False
    if kept:
        _hold(project, name, record, None)
        return False
    with util.locked():
        current = read(project, name)
        if current and current.get("nonce") == record.get("nonce"):
            record_path(project, name).unlink(missing_ok=True)
    return True


def _settled(record: dict) -> bool:
    from . import lifecycle
    activation = record.get("activation")
    if not activation:
        return False
    return lifecycle.terminal_of(lifecycle.facts(record.get("programme"), str(activation))) is not None


def sweep_test_clones() -> list[str]:
    """The tick's step: every test clone whose activation has settled is removed with its folders, whatever its
    state. An activation with no lifecycle fact at all is left alone."""
    removed = []
    root = state.root() / "lane-clones"
    if not root.is_dir():
        return removed
    try:
        folders = [folder for folder in sorted(root.iterdir())
                   if folder.is_dir() and not folder.name.startswith("_")]
    except OSError:
        return removed
    for folder in folders:
        # One clone that cannot be removed is held (and shown by `ygg status`); it never stops the tick.
        try:
            records = test_records(folder.name)
        except Exception:  # noqa: BLE001
            continue
        for name, record in records:
            try:
                if _settled(record) and remove_test_clone(folder.name, name):
                    removed.append(name)
            except Exception:  # noqa: BLE001
                continue
    return removed


def settle_test_clone(data: dict) -> None:
    """At an activation's settlement, once its agent's process group is gone (the supervisor's end, beside track
    B's `remove_scratch`): remove its test clone, named by the receipt's `clone` pin. The tick's sweep covers a
    supervisor that never got here."""
    pinned = data.get("clone") or {}
    if pinned.get("kind") != "test" or not pinned.get("project") or not pinned.get("name"):
        return
    try:
        remove_test_clone(str(pinned["project"]), str(pinned["name"]))
    except Exception:  # noqa: BLE001
        pass  # held; the tick's sweep tries again, and the supervisor's exit record is never lost to it


def test_checkout_of(project: str, su: str, activation: str) -> Path | None:
    record = read(project, test_record_name(su, activation))
    return Path(record["path"]) if record and record.get("kind") == "test" else None
