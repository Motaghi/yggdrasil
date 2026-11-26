"""The tender: Yggdrasil's own small program that does its work inside a lane box (`ygg _tend <mode> <spec>`).

It runs inside the Codex sandbox with the agent's own write rights (the runner in `clones` starts it from the box
it verified by descriptor), so it may read and change the box; Yggdrasil's trusted code never does (invariant T).
Everything it prints is read by the runner from its pipes, with byte and time limits.

Output: the walk's result is the first line, flushed before any git runs; the result of the mode is the last
line, a JSON object. `pack` streams a pack on standard output instead of the result line.

Modes: `prepare` (make a new or replaced clone ready at launch), `report` (what the box holds), `pack` (stream the
new commits), `ingest` (take in a pack the home sends at lane sync). The spec argument is
one JSON object written by trusted code: `branch`, `format`, `given_head`, `given_ref`, `feeding`, and per mode
`config` (prepare), `lane_head` (prepare, report), `sole` (prepare), `heads`/`negatives` (pack), `pack`/`head`
(ingest).
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import subprocess
import sys

MAX_ITEMS = 10
FAULT_EXIT = 97
HEX = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
FORMAT_RE = r"^(core\.repositoryformatversion|extensions\..*)$"
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK

# The same overrides as util.GIT_SAFETY (hooks, fsmonitor, signing, editors, pagers, ssh, credentials and
# transports off), plus no commit-graph, which an agent could plant to change the parents git reads.
GIT_FORCED = {"core.hooksPath": "/dev/null", "core.fsmonitor": "false", "commit.gpgSign": "false",
              "tag.gpgSign": "false", "gpg.program": "false", "gpg.ssh.program": "false",
              "gpg.x509.program": "false", "core.editor": "false", "sequence.editor": "false",
              "core.pager": "cat", "core.sshCommand": "false", "core.askPass": "false", "credential.helper": "",
              "protocol.allow": "never", "core.commitGraph": "false", "uploadpack.packObjectsHook": "",
              "core.alternateRefsCommand": "", "core.untrackedCache": "false", "gc.auto": "0",
              "maintenance.auto": "false"}
GIT_SELECTION = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_REPLACE_REF_BASE",
                 "GIT_CONFIG", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
                 "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM")
GIT_PROGRAMS = ("GIT_EXTERNAL_DIFF", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_PAGER", "GIT_SSH", "GIT_SSH_COMMAND",
                "GIT_ASKPASS", "GIT_PROXY_COMMAND", "GIT_EXEC_PATH", "GIT_TEMPLATE_DIR")


class GitFailed(Exception):
    """A git command the tender needs failed: the run ends as check 5, never as an empty or zero answer."""

    def __init__(self, args, code: int):
        super().__init__(" ".join(map(str, args[:2])))
        self.args_, self.code = args, code


class WalkError(Exception):
    def __init__(self, path: str, code: int):
        super().__init__(path)
        self.path, self.code = path, code


def git_env(global_config: str | None = None) -> dict:
    """Write-free: the filtering of util.git_env, and a global config that is read only when it already exists
    (never `trusted_global_config`, which writes it)."""
    environment = {k: v for k, v in os.environ.items()
                   if k not in GIT_SELECTION and k not in GIT_PROGRAMS and k != "GIT_CONFIG_GLOBAL"
                   and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = global_config if global_config and os.path.isfile(global_config) \
        else "/dev/null"
    environment["GIT_ALLOW_PROTOCOL"] = ""
    environment["GIT_PROTOCOL_FROM_USER"] = "0"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment.pop("GIT_TRACE", None)
    return environment


# ---------------------------------------------------------------- the walk

def _open_dir(name: str, parent: int, shown: str) -> int:
    """lstat the name from its parent, open it without following anything, and require the same object."""
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as error:
        raise WalkError(shown, error.errno)
    if not stat.S_ISDIR(before.st_mode):
        raise WalkError(shown, errno.ENOTDIR)
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    except OSError as error:
        raise WalkError(shown, error.errno)
    after = os.fstat(fd)
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        os.close(fd)
        raise WalkError(shown, errno.ESTALE)
    return fd


def _read_small(name: str, parent: int) -> list[str]:
    """At most 4 KiB and 10 lines of a small regular text file, opened without following a link."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except OSError:
        return []
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return []
        data = os.read(fd, 4096)
    finally:
        os.close(fd)
    return data.decode("utf-8", errors="replace").splitlines()[:MAX_ITEMS]


class Walk:
    """Every entry under `checkout/.git`, by descriptor, never following a link."""

    def __init__(self):
        self.special, self.ancestry, self.outside = [], [], []
        self.error: str | None = None
        self.size = 0
        self.size_complete = True

    def _add(self, kind: list, item: dict) -> None:
        if len(kind) < MAX_ITEMS:
            kind.append(item)
        elif len(kind) == MAX_ITEMS:
            kind.append({"path": "and more"})

    def classify(self, rel: str, mode: int, name: str, parent: int) -> None:
        is_link = stat.S_ISLNK(mode)
        special = is_link or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode)
        top = rel.split("/", 1)[0]
        item = {"path": rel, "kind": _kind(mode)}
        if is_link:
            try:
                item["target"] = os.readlink(name, dir_fd=parent)
            except OSError:
                pass
        if rel in ("shallow", "info/grafts") or rel.startswith("refs/replace/"):
            self._add(self.ancestry, item)
        outside = False
        if rel in ("objects/info/alternates", "objects/info/http-alternates"):
            outside = True
            if stat.S_ISREG(mode):
                item["lines"] = _read_small(name, parent)
        elif is_link and (rel == "objects" or rel.startswith("objects/")):
            outside = True
        elif rel == "commondir" and (is_link or stat.S_ISREG(mode)):
            outside = True
            if stat.S_ISREG(mode):
                item["lines"] = _read_small(name, parent)
        elif top in ("worktrees", "modules") and "/" in rel:
            outside = True
            if top == "worktrees" and rel.count("/") == 2 and rel.endswith("/gitdir") and stat.S_ISREG(mode):
                item["lines"] = _read_small(name, parent)
        if outside:
            self._add(self.outside, item)
        elif special:
            self._add(self.special, item)

    def git_folder(self, box: int) -> None:
        """Open `checkout` from the box descriptor, then `.git` from it, and list everything below."""
        checkout = _open_dir("checkout", box, "checkout")
        try:
            git = _open_dir(".git", checkout, ".git")
        finally:
            os.close(checkout)
        try:
            self._below(git, "")
        finally:
            os.close(git)

    def _below(self, folder: int, prefix: str) -> None:
        try:
            entries = [entry.name for entry in os.scandir(folder)]
        except OSError as error:
            raise WalkError(prefix.rstrip("/") or ".", error.errno)
        for name in sorted(entries):
            rel = prefix + name
            try:
                found = os.stat(name, dir_fd=folder, follow_symlinks=False)
            except OSError as error:
                if error.errno == errno.ENOENT:
                    continue  # vanished since the listing (a lock file of another activation's git)
                raise WalkError(rel, error.errno)
            self.size += found.st_size
            self.classify(rel, found.st_mode, name, folder)
            if stat.S_ISDIR(found.st_mode):
                try:
                    child = _open_dir(name, folder, rel)
                except WalkError as error:
                    if error.code == errno.ENOENT:
                        continue
                    raise
                try:
                    self._below(child, rel + "/")
                finally:
                    os.close(child)

    def rest_of_box(self, box: int) -> None:
        """`report`: the disk use of the rest of the box, following nothing; an error marks it incomplete."""
        try:
            self._sizes(box, "", skip="checkout/.git")
        except (OSError, WalkError):
            self.size_complete = False

    def _sizes(self, folder: int, prefix: str, skip: str) -> None:
        for entry in list(os.scandir(folder)):
            rel = prefix + entry.name
            if rel == skip:
                continue
            try:
                found = os.stat(entry.name, dir_fd=folder, follow_symlinks=False)
            except FileNotFoundError:
                continue
            self.size += found.st_size
            if stat.S_ISDIR(found.st_mode):
                child = _open_dir(entry.name, folder, rel)
                try:
                    self._sizes(child, rel + "/", skip)
                finally:
                    os.close(child)

    def result(self) -> dict:
        return {"special": self.special, "ancestry": self.ancestry, "outside": self.outside,
                "error": self.error}


def _kind(mode: int) -> str:
    for test, name in ((stat.S_ISLNK, "link"), (stat.S_ISFIFO, "fifo"), (stat.S_ISSOCK, "socket"),
                       (stat.S_ISCHR, "device"), (stat.S_ISBLK, "device"), (stat.S_ISDIR, "folder"),
                       (stat.S_ISREG, "file")):
        if test(mode):
            return name
    return "other"


# ---------------------------------------------------------------- git in the clone

class Tender:
    def __init__(self, spec: dict):
        self.spec = spec
        self.env = git_env(spec.get("global_config"))
        self.filters: list[str] = []

    def git(self, *args, input=None, stdin=None, stdout=subprocess.PIPE, text=True, timeout=None):
        argv = ["git"]
        for key, value in GIT_FORCED.items():
            argv += ["-c", key + "=" + value]
        argv += self.filters + ["-C", "checkout", *map(str, args)]
        return subprocess.run(argv, input=input, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE,
                              text=text, env=self.env, timeout=timeout, check=False)

    def ok(self, *args) -> bool:
        return self.git(*args).returncode == 0

    def out(self, *args) -> str:
        result = self.git(*args)
        return result.stdout if result.returncode == 0 else ""

    def need(self, *args) -> str:
        """The output of a git command that must succeed."""
        result = self.git(*args)
        if result.returncode != 0:
            raise GitFailed(args, result.returncode)
        return result.stdout

    def ref(self, name: str) -> str | None:
        """A ref's commit, None when it does not exist (exit 1), and a failure for anything else."""
        result = self.git("rev-parse", "--verify", "--quiet", name)
        if result.returncode == 0:
            return result.stdout.strip() or None
        if result.returncode == 1:
            return None
        raise GitFailed(("rev-parse", name), result.returncode)

    def empty_filters(self, git_fd: int) -> None:
        """Every filter driver the clone's config names (read as text) is emptied for every git call."""
        names = set()
        for line in _read_text(git_fd, "config", 1 << 20).splitlines():
            match = re.match(r'^\s*\[\s*filter\s+"((?:[^"\\]|\\.)*)"\s*\]', line)
            if match:
                names.add(match.group(1).replace('\\"', '"').replace("\\\\", "\\"))
        for name in sorted(names):
            for key in ("clean", "smudge", "process"):
                self.filters += ["-c", "filter." + name + "." + key + "="]
            self.filters += ["-c", "filter." + name + ".required=false"]


def _read_text(parent: int, name: str, limit: int) -> str:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except OSError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        return os.read(fd, limit).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


_OUT = {"stream": None}


def _emit(value: dict) -> None:
    """One JSON line on standard output (standard error in `pack`, whose standard output is the pack)."""
    stream = _OUT["stream"] or sys.stdout
    stream.write(json.dumps(value, sort_keys=True) + "\n")
    stream.flush()


def _odd(check: str, kind: str, path: str, detail: dict | None = None) -> dict:
    """An odd clone: the check (`2`, `3`, `3a`, `4`), what failed (`kind`) and its path or detail."""
    return dict({"result": "odd", "check": check, "kind": kind, "path": path}, **(detail or {}))


def _fault(name: str) -> None:
    if os.environ.get("YGG_TEST_MODE") == "1" and os.environ.get("YGG_FAULT_AT") == "boundary:" + name:
        sys.stdout.flush()
        os._exit(FAULT_EXIT)


def _check_box(box: int) -> dict | None:
    """Check 2 inside the box: `tmp` and `tmp/cache`, when they exist, are plain folders."""
    for name, parent_name in (("tmp", None), ("cache", "tmp")):
        parent = box
        opened = None
        if parent_name:
            try:
                opened = os.open(parent_name, DIR_FLAGS, dir_fd=box)
            except OSError:
                return None  # tmp is missing (prepare makes it) or was found odd above
            parent = opened
        try:
            found = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            found = None
        finally:
            if opened is not None:
                os.close(opened)
        if found is not None and not stat.S_ISDIR(found.st_mode):
            return _odd("2", "tmp", (parent_name + "/" if parent_name else "") + name)
    return None


def checks(tender: Tender, box: int, *, stop_at_4: bool = True) -> tuple[dict | None, Walk]:
    """The odd-clone checks 2 to 4, in the order 2, 3, 3a, 4. Returns (the first failure or None, the walk)."""
    walk = Walk()
    odd = _check_box(box)
    try:
        walk.git_folder(box)
    except WalkError as error:
        walk.error = "could not be checked: " + json.dumps(error.path) + ": " + errno.errorcode.get(
            error.code, str(error.code))
    _emit({"walk": walk.result()})
    if odd:
        return odd, walk
    if walk.error:
        return _odd("3", "unchecked", walk.error, {"walk": walk.result()}), walk
    if walk.special:
        return _odd("3", "special", walk.special[0]["path"], {"walk": walk.result()}), walk
    try:
        git_fd = os.open("checkout/.git", DIR_FLAGS, dir_fd=box)
    except OSError as error:
        return _odd("3", "unchecked", "could not be checked: \".git\": " + errno.errorcode.get(error.errno, "?")), walk
    try:
        tender.empty_filters(git_fd)
    finally:
        os.close(git_fd)
    # The two listing calls: packed replace refs, and gitlinks in the index.
    replace = tender.need("for-each-ref", "--format=%(refname)", "refs/replace/")
    for ref in replace.split():
        if not any(item.get("path") == ref for item in walk.ancestry):
            walk._add(walk.ancestry, {"path": ref, "kind": "ref"})
    staged = tender.need("ls-files", "--stage", "-z")
    for entry in staged.split("\0"):
        if entry.startswith("160000 "):
            walk._add(walk.outside, {"path": entry.split("\t", 1)[-1], "kind": "gitlink"})
    _emit({"listing": {"ancestry": walk.ancestry, "outside": walk.outside}})
    if walk.ancestry:
        return _odd("3", "ancestry", walk.ancestry[0]["path"], {"walk": walk.result()}), walk
    found = tender.git("config", "--local", "--includes", "--get-regexp", FORMAT_RE)
    expected = tender.spec.get("format")
    if found.returncode not in (0, 1) or (expected is not None and _format(found.stdout) != _format(expected)):
        key = _first_difference(found.stdout if found.returncode in (0, 1) else "", expected or "")
        return _odd("3", "format", key, {"walk": walk.result()}), walk
    if walk.outside:
        return _odd("3a", "outside", walk.outside[0]["path"], {"walk": walk.result()}), walk
    if not stop_at_4:
        return None, walk
    return check_4(tender), walk


def _format(text: str) -> list[str]:
    return sorted(" ".join(line.split()).lower() for line in str(text).splitlines() if line.strip())


def _first_difference(found: str, expected: str) -> str:
    have = {line.split(" ", 1)[0]: line for line in _format(found)}
    want = {line.split(" ", 1)[0]: line for line in _format(expected)}
    for key in sorted(set(have) | set(want)):
        if have.get(key) != want.get(key):
            return key
    return "git could not read the repository's config"


def check_4(tender: Tender) -> dict | None:
    spec = tender.spec
    branch = "refs/heads/" + str(spec["branch"])
    if not tender.ok("rev-parse", "--verify", "--quiet", branch + "^{commit}"):
        return _odd("4", "given", "the clone has no lane branch")
    if not tender.ok("rev-parse", "--verify", "--quiet", "HEAD^{commit}"):
        return _odd("4", "given", "HEAD does not parse")
    given_head = spec.get("given_head")
    if given_head and not tender.ok("cat-file", "-e", given_head + "^{commit}"):
        return _odd("4", "given", "the box does not hold " + given_head)
    given = tender.ref("refs/ygg/given")
    feeding = spec.get("feeding")
    if not given or given not in (spec.get("given_ref"), feeding):
        return _odd("4", "given", "refs/ygg/given " + ("is missing" if not given else "names " + given))
    if feeding and given == feeding and given != spec.get("given_ref"):
        if not tender.ok("cat-file", "-e", feeding + "^{commit}") or \
                not tender.ok("rev-list", "--objects", "--quiet", feeding, "--not", given_head):
            return _odd("4", "given", "refs/ygg/given names the unfinished feed " + feeding + ", which the box lacks")
    return None


# ---------------------------------------------------------------- modes

def _clean(tender: Tender) -> bool:
    differs = tender.git("diff-index", "--quiet", "HEAD", "--")
    if differs.returncode not in (0, 1):
        raise GitFailed(("diff-index", "HEAD"), differs.returncode)
    if differs.returncode == 1 or tender.need("diff", "--cached", "--name-only"):
        return False
    return not tender.need("ls-files", "--others", "--exclude-standard")


def prepare(tender: Tender, box: int) -> dict:
    odd, _ = checks(tender, box)
    if odd:
        return odd
    spec = tender.spec
    for name in ("tmp", "tmp/cache"):
        try:
            os.mkdir(name, 0o700, dir_fd=box)
        except FileExistsError:
            pass
    report: dict = {"result": "ok"}
    given = tender.ref("refs/ygg/given")
    report["given"] = given or None
    report["feed_complete"] = bool(spec.get("feeding")) and given == spec.get("feeding")
    git_fd = os.open("checkout/.git", DIR_FLAGS, dir_fd=box)
    try:
        temporary = "config.ygg-new"
        try:
            os.unlink(temporary, dir_fd=git_fd)
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=git_fd)
        with os.fdopen(fd, "w") as handle:
            handle.write(str(spec["config"]))
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, "config", src_dir_fd=git_fd, dst_dir_fd=git_fd)
        report["fast_forward"] = "not tried"
        if spec.get("sole"):
            try:
                found = os.stat("index.lock", dir_fd=git_fd, follow_symlinks=False)
                if stat.S_ISREG(found.st_mode):
                    os.unlink("index.lock", dir_fd=git_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(git_fd)
    lane_head = spec.get("lane_head")
    branch = "refs/heads/" + str(spec["branch"])
    head = tender.out("rev-parse", "--verify", "--quiet", "HEAD").strip()
    report["head"] = head
    on_branch = tender.out("symbolic-ref", "--quiet", "HEAD").strip() == branch
    if lane_head and head == lane_head:
        report["fast_forward"] = "done"
    elif spec.get("sole") and lane_head and on_branch and tender.ok("cat-file", "-e", lane_head + "^{commit}") \
            and _clean(tender) and tender.ok("merge-base", "--is-ancestor", head, lane_head):
        merged = tender.git("merge", "--ff-only", "--no-overwrite-ignore", "--quiet", lane_head)
        if merged.returncode == 0:
            report["fast_forward"] = "done"
        else:
            ignored = set(p for p in tender.need("ls-files", "-z", "--others", "--ignored",
                                                 "--exclude-standard").split("\0") if p)
            added = [p for p in tender.need("diff", "-z", "--name-only", "--no-renames", "--diff-filter=A",
                                            "HEAD", lane_head).split("\0") if p]
            report.update(fast_forward="refused", status=merged.returncode,
                          ignored=[p for p in added if p in ignored])
    report["head"] = tender.out("rev-parse", "--verify", "--quiet", "HEAD").strip()
    return report


def _count(text: str, sep: str = "\n") -> int:
    return len([part for part in text.split(sep) if part])


def report(tender: Tender, box: int) -> dict:
    odd, walk = checks(tender, box, stop_at_4=False)
    if odd and odd["check"] in ("3", "3a"):
        return odd
    early = odd
    spec = tender.spec
    lane_head = spec.get("lane_head")
    if lane_head and not tender.ok("cat-file", "-e", lane_head + "^{commit}"):
        print("missing: " + lane_head, flush=True)
        sys.exit(3)
    odd = early or check_4(tender)
    branch = str(spec["branch"])
    refs = [r for r in tender.need("for-each-ref", "--format=%(refname)").split()
            if r not in ("refs/heads/" + branch, "refs/stash", "refs/ygg/given")]
    stash = 0
    if tender.ref("refs/stash"):
        reflog = tender.git("reflog", "show", "--format=%H", "refs/stash", "--")
        stash = (_count(reflog.stdout) if reflog.returncode == 0 else 0) or 1
    commits = tender.need("rev-list", "--count", "--all", "--reflog", *(["--not", lane_head] if lane_head else []))
    head_ok = tender.ref("HEAD") is not None
    changed = (set(tender.need("diff", "--name-only", "HEAD", "--").split("\n")) if head_ok else set()) | \
        set(tender.need("diff", "--cached", "--name-only").split("\n"))
    untracked = tender.need("ls-files", "-z", "--others", "--exclude-standard")
    ignored = tender.need("ls-files", "-z", "--others", "--ignored", "--exclude-standard")
    metadata = []
    given = tender.ref("refs/ygg/given")
    if not given or given not in (spec.get("given_ref"), spec.get("feeding")):
        metadata.append("given")
    flags = tender.need("ls-files", "-v")
    if any(line[:1].islower() or line[:1] == "S" for line in flags.splitlines()):
        metadata.append("index-flags")
    config = tender.need("config", "--local", "--list").lower()
    if "core.sparsecheckout" in config or "core.sparsecheckoutcone" in config or \
            os.path.lexists("checkout/.git/info/sparse-checkout"):
        metadata.append("sparse")
    if "core.worktree" in config or "extensions.worktreeconfig" in config or \
            os.path.lexists("checkout/.git/config.worktree"):
        metadata.append("worktree-config")
    if "core.untrackedcache" in config or b"UNTR" in _index_extensions():
        metadata.append("untracked-cache")
    walk.rest_of_box(box)
    result = {"result": "odd" if odd else "ok", "lane_branch": tender.out("rev-parse", "--verify", "--quiet",
              "refs/heads/" + branch).strip() or None,
              "head": tender.out("rev-parse", "--verify", "--quiet", "HEAD").strip() or None,
              "detached": not tender.ok("symbolic-ref", "--quiet", "HEAD"),
              "size": walk.size if walk.size_complete else "incomplete",
              "counts": {"refs": len(refs), "stash": stash,
                         "commits": int(commits.strip()),
                         "changed": len([p for p in changed if p]), "untracked": _count(untracked, "\0"),
                         "ignored": _count(ignored, "\0")},
              "metadata": metadata}
    if odd:
        result.update(check=odd["check"], kind=odd["kind"], path=odd["path"])
    return result


def _index_extensions() -> bytes:
    try:
        fd = os.open("checkout/.git/index", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return b""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b""
        return os.read(fd, 64 << 20)
    finally:
        os.close(fd)


def pack(tender: Tender, box: int) -> int:
    spec = tender.spec
    for negative in spec.get("negatives") or []:
        if not tender.ok("cat-file", "-e", negative + "^{commit}"):
            print("missing: " + negative, flush=True)
            return 3
    lines = [spec["head"], "--not", *(spec.get("negatives") or [])]
    framing = "".join(line + "\n" for line in lines)
    sys.stdout.flush()
    argv = ["git"]
    for key, value in GIT_FORCED.items():
        argv += ["-c", key + "=" + value]
    argv += tender.filters + ["-C", "checkout", "pack-objects", "-q", "--revs", "--stdout", "--delta-base-offset"]
    process = subprocess.run(argv, input=framing.encode(), stdout=sys.stdout.fileno(), stderr=subprocess.PIPE,
                             env=tender.env, check=False)
    return 0 if process.returncode == 0 else 1


def ingest(tender: Tender, box: int) -> dict:
    odd, _ = checks(tender, box)
    if odd:
        return odd
    spec = tender.spec
    head = str(spec["head"])
    try:
        fd = os.open(str(spec["pack"]), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        return {"result": "failed", "why": "the feed file could not be opened: " + errno.errorcode.get(error.errno, "?")}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {"result": "failed", "why": "the feed file is not a regular file"}
        _fault("ingest-before-index")
        indexed = tender.git("index-pack", "--strict", "--stdin", stdin=fd, text=False)
    finally:
        os.close(fd)
    if indexed.returncode != 0:
        return {"result": "failed", "why": "index-pack exited " + str(indexed.returncode)}
    _fault("ingest-after-index")
    if not tender.ok("cat-file", "-e", head + "^{commit}") or \
            not tender.ok("rev-list", "--objects", "--quiet", head, "--not", spec["given_head"]):
        return {"result": "failed", "why": "the feed is not connected"}
    if not tender.ok("update-ref", "refs/ygg/given", head):
        return {"result": "failed", "why": "refs/ygg/given could not be set"}
    print("ingested: " + head, flush=True)
    return {"result": "ingested", "head": head}


MODES = {"prepare": prepare, "report": report, "ingest": ingest}


def main(mode: str, spec_text: str) -> int:
    """`ygg _tend <mode> <spec>`; the working folder is the box (the runner changed into its descriptor)."""
    try:
        spec = json.loads(spec_text)
    except ValueError:
        print("tender: the spec is not JSON", file=sys.stderr)
        return 2
    for key in ("given_head", "given_ref", "feeding", "lane_head", "head"):
        if spec.get(key) is not None and not HEX.match(str(spec[key])):
            print("tender: " + key + " is not a commit id", file=sys.stderr)
            return 2
    if any(not HEX.match(str(n)) for n in spec.get("negatives") or []):
        print("tender: a negative is not a commit id", file=sys.stderr)
        return 2
    tender = Tender(spec)
    box = os.open(".", DIR_FLAGS)
    try:
        return _run(mode, tender, box)
    except GitFailed as error:
        _emit({"result_line": True, "result": "odd", "check": "5", "kind": "unnamed",
               "path": "git " + str(error) + " exited " + str(error.code)})
        return 5
    finally:
        os.close(box)


def _run(mode: str, tender: Tender, box: int) -> int:
    if mode == "pack":
        _OUT["stream"] = sys.stderr
        odd, _ = checks(tender, box)
        if odd:
            _emit(odd)
            return 4
        return pack(tender, box)
    if mode not in MODES:
        print("tender: unknown mode " + mode, file=sys.stderr)
        return 2
    result = MODES[mode](tender, box)
    _emit({"result_line": True, **result})
    return 0
