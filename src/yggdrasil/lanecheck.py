"""Yggdrasil's lane check (ruling 130; A11 AC-15, AC-16, AC-31, AC-64, AC-66 to AC-70, AC-93; lane 1).

`ygg lane-check <hook> [args]` is what every file in Yggdrasil's lane hooks folder runs, from the installed
package. The home's own hooks never run on a lane commit: its own checks run before the merge into main.

- `pre-commit` and `pre-merge-commit`: the staged index (only what is staged) against the profile's
  `leak_denylist` (one term per line, `#` comments, case ignored) and the secret shapes Yggdrasil redacts,
  in content and names; credential files; CI workflow files and hook files unless the approved PID's repo
  entry allows them (`workflow_changes`, `hook_changes`); a file of an accepted package (the freeze, from any
  lane of the project); and no commit at all while a package of the lane is being checked.
- `commit-msg <file>`: the message against the denylist and secret shapes, and no `Co-Authored-By` trailer
  when the profile's `commit_identity.trailers` is `none`.
- `reference-transaction prepared`: the lane branch may not move to a commit that doesn't contain the newest
  accepted commit of the lane (amend, reset, rebase).
- `pre-push <remote> <url>`: the push guard.

Exit 1 with the reasons on standard error, 0 when clean. The trusted re-check (`recheck_problems`, D5) runs
the same rules with Yggdrasil's own git over a package's commits, at `ygg wp ready` and `ygg merge lane`.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys

from . import config, util

SECRET = re.compile(
    r"sk-(?:ant|proj|svcacct|admin)-[A-Za-z0-9_-]{8,}"
    r"|github_pat_[A-Za-z0-9_]{8,}"
    r"|gh[opsur]_[A-Za-z0-9]{20,}"
    r"|lin_(?:api|oauth)_[A-Za-z0-9]{8,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
FULL_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
TRAILER = re.compile(r"^\s*co-authored-by\s*:", re.I | re.M)
CI_PREFIXES = (".github/workflows/", ".circleci/", ".buildkite/")
CI_FILES = (".gitlab-ci.yml", "azure-pipelines.yml", "Jenkinsfile")
KEY_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
KEY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk")
# A11 AC-67, the signed patterns: .env, .env.*, *.pem, *.key, *.p12, id_rsa*, id_ed25519*, credentials.json,
# .npmrc and .netrc (a .npmrc is refused whatever it holds), plus the stores below.
CREDENTIAL_FILES = (".env", ".netrc", "_netrc", ".pypirc", ".git-credentials", ".htpasswd", "credentials.tfrc.json",
                    "credentials.json", ".npmrc")
# The standard credential stores, by their path (matched at the end of the touched path).
CREDENTIAL_PATHS = (".aws/credentials", ".kube/config", ".config/gcloud/application_default_credentials.json",
                    ".config/gh/hosts.yml", ".terraform.d/credentials.tfrc.json", ".ssh/authorized_keys")
# Configurations that are credentials only when they carry authentication (checked on their added content).
# npm (`key=value`) and yarn 1 (`key value` or `key: value`, keys and values often quoted), registry-scoped or
# not, with a value.
NPM_AUTH = re.compile(r"_(?:authToken|auth|password)[\"']?(?:[ \t]*[=:][ \t]*|[ \t]+)[\"']?[^\s\"']")
AUTH_CONFIGS = {".npmrc": NPM_AUTH, ".yarnrc": NPM_AUTH, ".yarnrc.yml": re.compile(r"npmAuthToken|npmAuthIdent")}
DOCKER_CONFIG = ".docker/config.json"
DOCKER_AUTH_KEYS = {"auth", "identitytoken", "registrytoken", "password"}
DOCKER_AUTH_RAW = re.compile(r'"(?:auth|identitytoken|registrytoken|password)"\s*:\s*"[^"]', re.I)
CREDENTIAL_CONTENT = re.compile(r"aws_secret_access_key\s*=", re.I)
SAMPLE_SUFFIXES = (".example", ".sample", ".template")


def _ends_with(path: str, endings) -> bool:
    return any(path == ending or path.endswith("/" + ending) for ending in endings)


def _auth_key(key) -> bool:
    """A Docker config key Docker would read as an auth field. Its Go decoder matches field names with Unicode
    case folding (the long s folds to s, the Kelvin sign to k), so keys are casefolded, and any key that is not
    plain ASCII counts (fail closed)."""
    text = str(key)
    return text.casefold() in DOCKER_AUTH_KEYS or not text.isascii()


def _docker_auth(data: str) -> bool:
    """Whether a Docker config carries authentication. The JSON is decoded first, so an escaped key
    (\\u0061uth) counts; one that does not decode is treated as carrying it (fail closed)."""
    import json
    if DOCKER_AUTH_RAW.search(data):
        return True
    try:
        # Every key-value pair, duplicates included (json.loads alone keeps only the last of a repeated key).
        parsed = json.loads(data, object_pairs_hook=lambda pairs: ("pairs", pairs))
    except ValueError:
        return True

    def found(value) -> bool:
        if isinstance(value, tuple) and value[:1] == ("pairs",):
            return any((_auth_key(key) and isinstance(item, str) and item != "") or found(item)
                       for key, item in value[1])
        if isinstance(value, list):
            return any(found(item) for item in value)
        return False
    return found(parsed)


class CheckError(Exception):
    pass


ZERO = "0" * 40
LANE_BRANCH = re.compile(r"^(?:refs/heads/)?ygg/([A-Za-z0-9_.-]+)/lane/([A-Za-z0-9_.-]+)$")
HOOK_PREFIXES = (".husky/", ".githooks/")
HOOK_FILES = (".pre-commit-config.yaml",)
TRAILERS_MESSAGE = "trailers are not allowed by the profile"
CI_MESSAGE = "CI workflow files are refused on lanes"
HOOKS_MESSAGE = "hook files are refused on lanes"
HOOK_NAMES = ("pre-commit", "commit-msg", "pre-merge-commit", "pre-push", "reference-transaction")


def hook_scripts(ygg: str) -> dict:
    """The lane hooks folder's files: each calls the installed ygg's lane check, nothing else (AC-16)."""
    quoted = shlex.quote(ygg)
    scripts = {}
    for name in HOOK_NAMES:
        if name == "reference-transaction":
            # Only the prepared state can refuse; the others need no check (and no ygg start-up).
            scripts[name] = ('#!/bin/sh\n[ "$1" = prepared ] || exit 0\nexec ' + quoted +
                             ' lane-check reference-transaction "$@"\n')
        else:
            scripts[name] = "#!/bin/sh\nexec " + quoted + " lane-check " + name + ' "$@"\n'
    return scripts


def _git(*args, binary: bool = False, check: bool = True):
    # Yggdrasil's own git safety, keeping GIT_INDEX_FILE: a commit hook scans the index git hands it.
    environment = util.git_env()
    if os.environ.get("GIT_INDEX_FILE"):
        environment["GIT_INDEX_FILE"] = os.path.abspath(os.environ["GIT_INDEX_FILE"])
    result = subprocess.run(["git", *util.GIT_SAFETY, *map(str, args)], capture_output=True, text=not binary,
                            env=environment)
    if check and result.returncode != 0:
        detail = result.stderr if not binary else result.stderr.decode("utf-8", "replace")
        raise CheckError("git " + " ".join(map(str, args[:2])) + " failed: " + detail.strip())
    return result.stdout if check else result


def _ok(*args) -> bool:
    return _git(*args, check=False).returncode == 0


def _read(path) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def denylist() -> list[str]:
    """The profile's `leak_denylist` terms, case-folded. Never `YGG_LEAK_DENYLIST` or git config (AC-31)."""
    path = config.get("leak_denylist")
    if not path:
        return []
    path = os.path.expanduser(str(path))
    try:
        lines = _read(path).splitlines()
    except OSError:
        raise CheckError("cannot read the leak denylist " + path)
    return [line.strip().casefold() for line in lines if line.strip() and not line.strip().startswith("#")]


def _terms_in(text: str, terms: list[str]) -> list[str]:
    folded = text.casefold()
    return [term for term in terms if term in folded]


def _secret(text: str) -> bool:
    return bool(SECRET.search(text) or util.SECRET_RE.search(text))


def touched_problem(path: str, allow_ci: bool = False, allow_hooks: bool = False,
                    hooks_path: str | None = None) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if path.startswith(CI_PREFIXES) or path in CI_FILES:
        return None if allow_ci else CI_MESSAGE + " (" + path + ")"
    if not allow_hooks and (path.startswith(HOOK_PREFIXES) or path in HOOK_FILES or
                            (hooks_path and (path + "/").startswith(hooks_path.rstrip("/") + "/"))):
        return HOOKS_MESSAGE + " (" + path + ")"
    if name == ".env" or name.startswith(".env."):
        return "credential file " + path + " is refused"  # the signed `.env.*` has no sample exemption
    if name.endswith(SAMPLE_SUFFIXES):
        return None
    if name in CREDENTIAL_FILES or _ends_with(path, CREDENTIAL_PATHS) or \
            (name.startswith(KEY_PREFIXES) and not name.endswith(".pub")) or name.endswith(KEY_SUFFIXES):
        return "credential file " + path + " is refused"
    return None


def content_problem(path: str, data: str) -> str | None:
    """A configuration that carries authentication (npm, yarn, Docker), or AWS secret keys in any file."""
    for ending, pattern in AUTH_CONFIGS.items():
        if _ends_with(path, (ending,)) and pattern.search(data):
            return "credential file " + path + " is refused (it carries authentication)"
    if _ends_with(path, (DOCKER_CONFIG,)) and _docker_auth(data):
        return "credential file " + path + " is refused (it carries authentication)"
    if CREDENTIAL_CONTENT.search(data):
        return "credential file " + path + " is refused (it holds an AWS secret key)"
    return None


class Rules:
    """What the lane check needs to know about one repository and project."""

    def __init__(self, repo: str, project: str | None, lane: str | None):
        self.repo, self.project, self.lane = repo, project, lane
        self.terms = denylist()
        entry = _repo_entry(project, repo) if project else None
        self.allow_ci = bool((entry or {}).get("workflow_changes"))
        self.allow_hooks = bool((entry or {}).get("hook_changes"))
        self.hooks_path = home_hooks_path(repo)
        self._frozen = None

    def frozen(self) -> dict:
        if self._frozen is None:
            self._frozen = frozen_files(self.project, self.repo) if self.project else {}
        return self._frozen


def _repo_entry(project: str | None, repo: str):
    try:
        from . import gitops
        return gitops._repo_entry(str(project), repo)
    except (util.YggError, OSError, ValueError):
        return None


def home_hooks_path(repo: str) -> str | None:
    """The home's own `core.hooksPath` (its shared config, never a worktree's), relative to the checkout."""
    common = _git("-C", repo, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if common.returncode != 0:
        return None
    common_dir = common.stdout.strip()
    found = _git("config", "--file", os.path.join(common_dir, "config"), "--get", "core.hooksPath", check=False)
    value = found.stdout.strip() if found.returncode == 0 else ""
    if not value:
        return None
    home = os.path.dirname(common_dir) if os.path.basename(common_dir) == ".git" else common_dir
    value = os.path.expanduser(value)
    absolute = os.path.realpath(value if os.path.isabs(value) else os.path.join(home, value))
    relative = os.path.relpath(absolute, os.path.realpath(home))
    return None if relative.startswith("..") else relative


def frozen_message(path: str, package: str) -> str:
    return path + " belongs to accepted " + package + "; ask the PM to reopen " + package


def entry_problems(repo: str, raw: str, rules: Rules, exclude=(), owns=None) -> list[str]:
    """Problems of `git diff --raw -z` entries: touched paths, names, added content and the freeze."""
    problems = []
    fields = [f for f in raw.split("\0") if f]
    index = 0
    frozen = rules.frozen()
    while index < len(fields) - 1:
        meta, name = fields[index].split(), fields[index + 1]
        index += 2
        if not meta or not meta[0].startswith(":"):
            index -= 1  # a commit id line from diff-tree; skip it
            continue
        # A path shaped like a secret is never repeated in a message (stderr and logs).
        shown = "(a path shaped like a secret)" if _secret(name) else name
        owner = frozen.get(name)
        # `owns(owner)`: the commit under check is part of the owner's own accepted history (re-check only).
        if owner and owner not in exclude and not (owns and owns(owner)):
            problems.append(frozen_message(shown, owner))
        touched = touched_problem(name, rules.allow_ci, rules.allow_hooks, rules.hooks_path)
        if touched:
            problems.append(touched.replace(name, shown))
        for term in _terms_in(name, rules.terms):
            problems.append("a path matches the denylist term " + term)
        if _secret(name):
            problems.append("a path holds a secret-shaped token")
        if len(meta) < 5 or meta[4] == "D" or meta[1] == "160000":
            continue  # deleted, or a submodule commit: no content here
        if not FULL_ID.fullmatch(meta[3]):
            raise CheckError("unexpected object id " + meta[3])
        data = _git("-C", repo, "cat-file", "blob", meta[3], binary=True).decode("utf-8", "replace")
        for term in _terms_in(data, rules.terms):
            problems.append(shown + ": content matches the denylist term " + term)
        if _secret(data):
            problems.append(shown + ": content holds a secret-shaped token")
        credential = None if touched else content_problem(name, data)
        if credential:
            problems.append(credential.replace(name, shown))
    return problems


def text_problems(text: str, terms: list[str]) -> list[str]:
    problems = []
    for term in _terms_in(text, terms):
        problems.append("the commit message matches the denylist term " + term)
    if _secret(text):
        problems.append("the commit message holds a secret-shaped token")
    identity = config.get("commit_identity")
    if isinstance(identity, dict) and identity.get("trailers") == "none" and TRAILER.search(text):
        problems.append(TRAILERS_MESSAGE)
    return problems


# ---------------------------------------------------------------- packages, the freeze and the lane

def _packages(project: str):
    from . import tracker
    return [issue for issue in tracker.issues(project) if issue.get("kind") == "work-package"]


def _accepted_sha(issue) -> str | None:
    found = [r for r in issue.records if r.get("kind") == "acceptance"]
    if found and found[-1].get("verdict") == "accept" and found[-1].get("sha"):
        return str(found[-1]["sha"])
    return None


def _review_base(issue) -> str | None:
    for record in reversed(issue.records):
        if record.get("kind") == "acceptance":
            continue
        if record.get("base") and re.fullmatch(r"[0-9a-f]{7,64}", str(record.get("base"))):
            return str(record["base"])
    return None


def _main_ref(repo: str) -> str | None:
    found = _git("-C", repo, "rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}", check=False)
    return found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else None


def _is_ancestor(repo: str, older: str, newer: str) -> bool:
    return _ok("-C", repo, "merge-base", "--is-ancestor", older, newer)


def _base_for(repo: str, issue, sha: str, others: list[str]) -> str | None:
    """A package's review base: the recorded one, else the newest of main's merge base and the lane's earlier
    accepted commits below it."""
    recorded = _review_base(issue)
    if recorded and _ok("-C", repo, "cat-file", "-e", recorded + "^{commit}"):
        return recorded
    main = _main_ref(repo)
    candidates = []
    if main:
        found = _git("-C", repo, "merge-base", sha, main, check=False)
        if found.returncode == 0 and found.stdout.strip():
            candidates.append(found.stdout.strip())
    candidates += [other for other in others if other != sha and _is_ancestor(repo, other, sha)]
    best = None
    for candidate in candidates:
        if best is None or _is_ancestor(repo, best, candidate):
            best = candidate
    return best


def accepted_packages(project: str, repo: str) -> list[tuple[str, str | None, str, object]]:
    """(package, lane, accepted sha, issue) of every package accepted now whose commit is in this repo."""
    found = []
    for issue in _packages(project):
        if issue.get("state") != "accepted":
            continue
        sha = _accepted_sha(issue)
        if sha and _ok("-C", repo, "cat-file", "-e", sha + "^{commit}"):
            found.append((issue.id, issue.get("lane"), sha, issue))
    return found


def _commit_entries(repo: str, sha: str) -> str:
    """The raw `-z` diff of one commit against its first parent (a root commit against the empty tree). A
    merge is compared with its first parent explicitly: plain diff-tree shows nothing for a merge, which would
    let a merge resolution carry a leak, a forbidden path or a frozen file past the re-check."""
    parents = _git("-C", repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]
    common = ("-r", "--raw", "-z", "--no-renames", "--no-ext-diff", "--full-index", "--no-abbrev",
              "--diff-filter=ACMTD")
    if not parents:
        return _git("-C", repo, "diff-tree", "--root", *common, sha)
    return _git("-C", repo, "diff-tree", *common, parents[0], sha)


def _raw_names(raw: str) -> list[str]:
    """The exact paths of `git diff --raw -z` entries (no quoting, no stripping)."""
    names, fields, index = [], raw.split("\0"), 0
    while index < len(fields) - 1:
        if fields[index].startswith(":"):
            names.append(fields[index + 1])
            index += 2
        else:
            index += 1
    return [name for name in names if name]


def package_files(project: str, repo: str, base: str | None, sha: str) -> set:
    """A package's own changes (A11 AC-15). Let M be the unique greatest main side among the recorded lane syncs
    between the review base B and the accepted commit: the main-side commit that every other in-range main side
    is an ancestor of (B when there are no such syncs). It is exactly the main state the lane has absorbed. If
    there is no unique greatest main side, nothing is left out. A path the package's own commits touched is frozen unless its full tree entry (mode and
    object, or absence) in the accepted commit equals its entry in M. One exception keeps a lane-only file
    frozen: a path B holds that main never had (absent from M and from M's fork point with B) can't be left out
    by matching M's absence (the package deleted a file of the lane's own)."""
    if base is None:
        return set(_raw_names(_commit_entries(repo, sha)))
    syncs = recorded_syncs(project)
    in_range = [merge for merge in syncs
                if _ok("-C", repo, "cat-file", "-e", merge + "^{commit}") and _is_ancestor(repo, merge, sha)
                and not _is_ancestor(repo, merge, base)]
    main_sides = {merge: _git("-C", repo, "rev-list", "--parents", "-n", "1", merge).split()[2:3]
                  for merge in in_range}
    touched = set()
    excluded = ["^" + base] + ["^" + side[0] for side in main_sides.values() if side]
    for commit in _git("-C", repo, "rev-list", sha, *excluded).split():
        if commit not in syncs:
            touched.update(_raw_names(_commit_entries(repo, commit)))  # the lane's own commits
    if not touched:
        return set()
    # M is the unique greatest main side: every other in-range main side is an ancestor of it. It is chosen by
    # the main-side commits, never by the sync commits (a non-sync merge can make those incomparable) or by
    # record order. Incomparable main sides are fine when one later main side holds them all (a main merge
    # synced afterwards); if there is no unique greatest main side, nothing is left out.
    sides = sorted({side[0] for side in main_sides.values() if side})
    absorbed = base
    if sides:
        top = [side for side in sides if all(other == side or _is_ancestor(repo, other, side) for other in sides)]
        if len(top) != 1:
            return touched
        absorbed = top[0]
    fork = _git("-C", repo, "merge-base", absorbed, base, check=False).stdout.strip() if absorbed != base else base
    accepted, in_main = _entries(repo, sha, touched), _entries(repo, absorbed, touched)
    at_base, at_fork = _entries(repo, base, touched), (_entries(repo, fork, touched) if fork else {})
    own = set()
    for path in touched:
        lane_only = at_base.get(path) is not None and in_main.get(path) is None and at_fork.get(path) is None
        if accepted.get(path) != in_main.get(path) or lane_only:
            own.add(path)
    return own


def _entries(repo: str, rev: str, paths) -> dict:
    """path -> (mode, object id) at `rev`; a path missing there maps to None."""
    found = {path: None for path in paths}
    if not paths:
        return found
    listed = _git("--literal-pathspecs", "-C", repo, "ls-tree", "-r", "-z", "--full-tree", rev, "--",
                  *sorted(paths), check=False)
    for record in (listed.stdout.split("\0") if listed.returncode == 0 else []):
        if "\t" not in record:
            continue
        meta, path = record.split("\t", 1)
        parts = meta.split()
        if path in found and len(parts) == 3:
            found[path] = (parts[0], parts[2])
    return found


def _acceptance(issue) -> dict:
    found = [r for r in issue.records if r.get("kind") == "acceptance"]
    return found[-1] if found else {}


def _package_number(identifier: str) -> int:
    match = re.search(r"(\d+)$", str(identifier))
    return int(match.group(1)) if match else 0


def frozen_owners(project: str, repo: str) -> dict:
    """path -> (package, accepted sha) for every accepted package's file set (A11 AC-15). A path in two file
    sets belongs to the package accepted first: by its acceptance record's time, then its `seq` when the
    record carries one, then the commits below the accepted commit, then the package number (the tracker keeps
    no finer order within one second)."""
    accepted = accepted_packages(project, repo)
    shas = [sha for _, _, sha, _ in accepted]

    def order(entry):
        # A strict total order, as one sort key (so it can't cycle): the acceptance record's time, its seq when it
        # carries one, then the number of commits below the accepted commit (on one history an earlier commit
        # has fewer, which settles same-second acceptances such as A11 AC-85 row 3's seeded one), then the
        # package number.
        record = _acceptance(entry[3])
        seq = record.get("seq")
        counted = _git("-C", repo, "rev-list", "--count", entry[2], check=False)
        depth = int(counted.stdout.strip() or 0) if counted.returncode == 0 else 0
        return (str(record.get("at") or ""), seq is None, int(seq) if str(seq or "").isdigit() else 0,
                depth, _package_number(entry[0]), str(entry[0]))

    owners = {}
    for package, _, sha, issue in sorted(accepted, key=order):
        base = _base_for(repo, issue, sha, shas)
        for name in sorted(package_files(project, repo, base, sha)):
            owners.setdefault(name, (package, sha))
    return owners


def frozen_files(project: str, repo: str) -> dict:
    """path -> package for every accepted package's file set (see frozen_owners)."""
    return {path: owner[0] for path, owner in frozen_owners(project, repo).items()}


def newest_accepted(project: str, lane: str, repo: str) -> tuple[str, str] | None:
    """(package, sha) of the lane's accepted commit that every other accepted commit of the lane is below."""
    best = None
    for package, owner, sha, _ in accepted_packages(project, repo):
        if owner != lane:
            continue
        if best is None or _is_ancestor(repo, best[1], sha):
            best = (package, sha)
    return best


def checked_packages(project: str, lane: str) -> list[str]:
    """Packages of the lane being checked (in review or testing): the lane holds still meanwhile (AC-66)."""
    return [issue.id for issue in _packages(project)
            if issue.get("lane") == lane and issue.get("state") in ("in-review", "ready-for-test")]


def _where(repo: str = ".") -> tuple[str, str | None, str | None]:
    """(worktree top, project, lane) of the hook's checkout: the lane from its branch, else the environment."""
    top = _git("-C", repo, "rev-parse", "--show-toplevel", check=False)
    root = top.stdout.strip() if top.returncode == 0 and top.stdout.strip() else os.path.abspath(repo)
    branch = _git("-C", root, "symbolic-ref", "--quiet", "HEAD", check=False).stdout.strip()
    match = LANE_BRANCH.match(branch)
    if match:
        return root, match[1], match[2]
    return root, os.environ.get("YGG_PROJECT") or None, None


# ---------------------------------------------------------------- the hooks

def _parent_amends() -> bool:
    """Whether the git process running this hook is a `git commit --amend` (any accepted abbreviation). Only
    an earlier, plainer refusal: the reference-transaction hook is what holds the accepted commit."""
    try:
        found = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(os.getppid())], capture_output=True,
                               text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    words = found.stdout.split()
    return any(len(word) >= len("--am") and "--amend".startswith(word) for word in words)


def staged_problems(repo: str = ".") -> list[str]:
    """The pre-commit check. The scan of what is staged always runs, so a leak is named even when the
    lane's packages can't be read (for example after the state root is emptied, AC-96 row 2); a failed
    package lookup is then one more reason to refuse (fail closed), never a reason to skip the scan."""
    root, project, lane = _where(repo)
    context = []
    if project and lane:
        try:
            held = checked_packages(project, lane)
            if held:
                return [held[0] + " is being checked; commit after its verdict"]
            newest = newest_accepted(project, lane, root) if _parent_amends() else None
            if newest is not None and not _ok("-C", root, "merge-base", "--is-ancestor", newest[1], "HEAD^"):
                return ["the lane must keep " + newest[0] + "'s accepted commit"]
        except (CheckError, util.YggError, OSError, ValueError) as error:
            context.append("the lane's packages can't be read: " + str(getattr(error, "message", error)))
    rules = Rules(root, project, lane)
    try:
        rules.frozen()
    except (CheckError, util.YggError, OSError, ValueError) as error:
        rules._frozen = {}
        context.append("the accepted packages can't be read: " + str(getattr(error, "message", error)))
    raw = _git("-C", root, "diff", "--cached", "--raw", "-z", "--no-renames", "--no-ext-diff", "--full-index",
               "--no-abbrev", "--diff-filter=ACMTD")
    return entry_problems(root, raw, rules) + context


def message_problems(path: str) -> list[str]:
    try:
        text = _read(path)
    except OSError:
        raise CheckError("cannot read the commit message " + path)
    # Git's own comment lines are not part of the message.
    text = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    return text_problems(text, denylist())


def transaction_problems(lines: str, repo: str = ".") -> list[str]:
    """The lane branch may not move to a commit that doesn't contain the lane's newest accepted commit."""
    root, project, _ = _where(repo)
    problems = []
    for line in lines.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        _, new, ref = parts
        match = LANE_BRANCH.match(ref)
        if not match or not ref.startswith("refs/heads/"):
            continue
        from . import lanedrop
        back = lanedrop.history_problem(match[1], root, new)  # A11.1 AC-12
        if back:
            problems.append(back)
        newest = newest_accepted(match[1], match[2], root)
        if newest is None:
            continue
        package, sha = newest
        if new == ZERO or not _is_ancestor(root, sha, new):
            problems.append("the lane must keep " + package + "'s accepted commit")
    return problems


def range_problems(repo: str, base: str, tip: str, allow_ci: bool = False) -> list[str]:
    """The lane check on every commit in base..tip, for the push guard: a commit whose hooks were skipped
    still can't reach the remote."""
    _, project, lane = _where(repo)
    rules = Rules(repo, project, lane)
    rules.allow_ci = rules.allow_ci or allow_ci
    rules._frozen = {}  # the freeze holds at commit time and at the trusted re-check
    problems = []
    for sha in _git("-C", repo, "rev-list", base + ".." + tip).split():
        raw = _git("-C", repo, "diff-tree", "-r", "-m", "--root", "--raw", "-z", "--no-renames", "--no-ext-diff",
                   "--full-index", "--no-abbrev", "--diff-filter=ACMTD", sha)
        found = entry_problems(repo, raw, rules)
        found += text_problems(_git("-C", repo, "log", "-1", "--format=%B", sha), rules.terms)
        problems += [sha[:12] + ": " + problem for problem in found]
    return problems


# ---------------------------------------------------------------- the trusted re-check (D5)

RECHECK = "re-check refused"


def recorded_syncs(project: str) -> dict:
    """sha -> {lane, main} of every merge Yggdrasil made as a lane sync (AC-66 row 3), kept in the safety
    folder with the drop records (lanedrop)."""
    from . import lanedrop
    return lanedrop.recorded_syncs(project)


def record_sync(project: str, lane: str, sha: str, main: str) -> None:
    from . import lanedrop
    lanedrop.record_sync(project, lane, sha, main)


def _identity_problems(repo: str, sha: str) -> list[str]:
    identity = config.get("commit_identity")
    if not isinstance(identity, dict) or not identity.get("name"):
        return []
    fields = _git("-C", repo, "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce", sha).rstrip("\n").split("\0")
    if len(fields) != 4:
        return ["cannot read the commit identity"]
    wanted = (str(identity.get("name")), str(identity.get("email") or ""))
    problems = []
    if (fields[0], fields[1]) != wanted:
        problems.append("author isn't the profile's commit identity")
    if (fields[2], fields[3]) != wanted:
        problems.append("committer isn't the profile's commit identity")
    return problems


def _sync_extra(repo: str, sha: str) -> str | None:
    """For a recorded sync merge: the raw diff of what it holds beyond re-merging its two parents."""
    parents = _git("-C", repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]
    if len(parents) != 2:
        return None
    merged = _git("-C", repo, "merge-tree", "--write-tree", parents[0], parents[1], check=False)
    tree = merged.stdout.split("\n", 1)[0].strip() if merged.returncode == 0 else ""
    if not tree:
        return None
    return _git("-C", repo, "diff-tree", "-r", "--raw", "-z", "--no-renames", "--no-ext-diff", "--full-index",
                "--no-abbrev", "--diff-filter=ACMTD", tree, sha + "^{tree}")


def recheck_problems(project: str, lane: str, repo: str, packages, head: str | None = None) -> list[str]:
    """The lane check again, with Yggdrasil's own git, over a package's commits: those from the review base to
    the lane head that main doesn't hold. A recorded sync merge counts only for what it holds beyond its two
    parents, so main's own commits never count against a package (D5)."""
    packages = set(packages)
    branch = "refs/heads/ygg/" + project + "/lane/" + lane
    head = head or _git("-C", repo, "rev-parse", "--verify", "--quiet", branch + "^{commit}", check=False).stdout.strip()
    if not head:
        return []
    rules = Rules(repo, project, lane)
    owners = frozen_owners(project, repo)
    rules._frozen = {path: owner[0] for path, owner in owners.items()}
    owner_shas = {package: sha for package, sha in owners.values()}
    problems = []
    from . import lanedrop
    back = lanedrop.history_problem(project, repo, head)  # a dropped commit back on the lane (A11.1 AC-12)
    if back:
        problems.append(back)
    bases = []
    main = _main_ref(repo)
    for package, owner, sha, _ in accepted_packages(project, repo):
        if owner == lane and package not in packages and _is_ancestor(repo, sha, head):
            bases.append(sha)
    for issue in _packages(project):
        if issue.id in packages:
            recorded = _review_base(issue)
            if recorded and _ok("-C", repo, "cat-file", "-e", recorded + "^{commit}") and \
                    _is_ancestor(repo, recorded, head):
                bases.append(recorded)
    excluded = [main] if main else []
    listed = _git("-C", repo, "rev-list", head, *["^" + base for base in bases + excluded])
    syncs = recorded_syncs(project)
    dropped = lanedrop.dropped_commits(project)
    for sha in listed.split():
        if sha in dropped:
            continue  # already named by the history check above
        if sha in syncs:
            extra = _sync_extra(repo, sha)
            found = entry_problems(repo, extra, rules, owns=lambda owner, sha=sha: _is_ancestor(
                repo, sha, owner_shas[owner])) if extra is not None else \
                ["a recorded sync merge can't be re-merged"]
        else:
            # A change to an accepted package's file counts unless the commit is in that package's accepted
            # history, whichever packages the caller names (AC-85 row 3, AC-93 row 3).
            found = entry_problems(repo, _commit_entries(repo, sha), rules,
                                   owns=lambda owner, sha=sha: _is_ancestor(repo, sha, owner_shas[owner]))
            found += text_problems(_git("-C", repo, "log", "-1", "--format=%B", sha), rules.terms)
            found += _identity_problems(repo, sha)
        problems += [sha[:12] + ": " + problem for problem in found]
    return problems


def recheck(project: str, lane: str, repo: str, packages, head: str | None = None) -> None:
    """Raise the re-check's refusal (exit 3) when the package's commits fail the lane check. Called by
    `ygg wp ready` (lane 2) and `ygg merge lane` (lane 4)."""
    try:
        problems = recheck_problems(project, lane, repo, packages, head)
    except CheckError as error:
        problems = [str(error)]
    if problems:
        raise util.refuse(RECHECK + ": " + "; ".join(problems))


def recheck_package(project: str, package: str) -> None:
    """`recheck` for one package at `ygg wp ready`: its lane and the project's one approved repo."""
    from . import gitops, tracker
    issue = tracker.find_package(package, project)
    lane = issue.get("lane") if issue is not None else None
    repos = gitops.approved_repos(project) or []
    if not lane or not repos or not repos[0].get("path"):
        return  # nothing to re-check: no lane or no git repo (document folders)
    recheck(project, str(lane), gitops.repo_home(str(repos[0]["path"])), [package])


def head_holds_only_syncs(project: str, repo: str, accepted: str, head: str) -> bool:
    """Whether the lane head is the accepted commit, or only recorded sync merges past it (AC-66 row 3)."""
    if accepted == head:
        return True
    if not _is_ancestor(repo, accepted, head):
        return False
    syncs = recorded_syncs(project)
    first_parent = _git("-C", repo, "rev-list", "--first-parent", head, "^" + accepted).split()
    return bool(first_parent) and all(sha in syncs for sha in first_parent)


def _report(problems: list[str]) -> int:
    if not problems:
        return 0
    print("ygg lane check refused the commit:", file=sys.stderr)
    for problem in problems:
        print("- " + problem, file=sys.stderr)
    return 1


def _guarded(check) -> int:
    try:
        return _report(check())
    except (CheckError, util.YggError) as error:
        return _report([str(getattr(error, "message", error))])


def pre_commit() -> int:
    return _guarded(staged_problems)


def commit_msg(path: str) -> int:
    return _guarded(lambda: message_problems(path))


def main(arguments: list[str]) -> int:
    """`ygg lane-check <hook> [args]`, run by the lane hooks folder."""
    if not arguments:
        raise util.usage("ygg lane-check pre-commit | commit-msg <file> | pre-merge-commit | pre-push <remote> "
                         "[<url>] | reference-transaction <state>")
    hook, rest = arguments[0], arguments[1:]
    if hook in ("pre-commit", "pre-merge-commit"):
        return pre_commit()
    if hook == "commit-msg":
        if not rest:
            raise util.usage("ygg lane-check commit-msg <file>")
        return commit_msg(rest[0])
    if hook == "reference-transaction":
        if rest[:1] != ["prepared"]:
            return 0
        text = sys.stdin.read()
        code = _guarded(lambda: transaction_problems(text))
        if code:
            print("ygg lane check refused the branch move.", file=sys.stderr)
        return code
    if hook == "pre-push":
        from . import gitops
        root, _, _ = _where(".")
        code, message = gitops.push_guard(root, rest[0] if rest else "origin", sys.stdin.read(),
                                          rest[1] if len(rest) > 1 else None)
        if message:
            print(message, file=sys.stderr)
        return 1 if code else 0
    raise util.usage("unknown lane hook '" + hook + "'")
