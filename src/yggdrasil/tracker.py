"""Files tracker (Norns): markdown and JSON records under YGG_FILES_ROOT."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from . import util
from .util import STANDARD_VERSION, YggError

COUNTER_PREFIX = {
    "decision": ("D-", 4), "exception": ("E-", 4), "morning-go": ("MG-", 4),
    "envelope": ("EN-", 4), "issue": ("I-", 4), "message": ("m-", 4),
    "activation": ("act-", 4), "task": ("T-", 4),
}


def root() -> Path:
    from . import config
    selected = config.get("tracker", "files")
    if selected == "files":
        return util.files_root()
    if selected == "linear":
        # The local cache of the Linear tracker, read exactly like the files tracker.
        from .linear import adapter
        return adapter.cache_root()
    from . import plugins
    if selected not in plugins.BUILTINS["trackers"]:
        # A discovered plugin that keeps records in the files-tracker format names its folder.
        plugin = plugins.load("trackers", str(selected))
        locate = getattr(plugin, "root", None)
        if callable(locate):
            return Path(locate()).expanduser()
        raise YggError(util.EXIT_TRACKER, "tracker plugin '" + str(selected) + "' has no root(); v1 runs "
                       "trackers that keep files-tracker records, so the plugin must name their folder.")
    raise YggError(util.EXIT_TRACKER, "tracker '" + str(selected) + "' is not available in this build; "
                   "the files and linear trackers are implemented.")


def selected() -> str:
    from . import config
    return str(config.get("tracker", "files"))


def pull() -> None:
    """Rebuild a remote tracker's local cache from the tracker alone (no-op for the files tracker)."""
    if selected() == "linear":
        from .linear import adapter
        adapter.pull()


def refresh() -> None:
    """Bring the local cache of a remote tracker up to its current revision (no-op for the files tracker)."""
    if selected() == "linear":
        from .linear import adapter
        adapter.refresh()


def corrupt(path: Path, detail: str) -> YggError:
    from . import state
    message = "corrupt tracker record " + str(path) + ": " + detail
    try:
        state.alert("ygg-tracker-corrupt", message, path=str(path))
    except OSError:
        pass
    return YggError(util.EXIT_TRACKER, message)


def read_record(path: Path) -> dict:
    """Read a JSON tracker record, failing loudly (exit 8 plus an alert) when it is malformed."""
    import json
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise corrupt(path, str(error))
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        raise corrupt(path, "expected an object with an id")
    return data


def ensure_writable() -> None:
    base = root()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise YggError(util.EXIT_TRACKER, "tracker unreachable: " + str(error))
    if not os.access(base, os.W_OK):
        raise YggError(util.EXIT_TRACKER, "tracker unreachable: " + str(base) + " is not writable")


def inside_root(path: Path) -> Path:
    base = root().resolve()
    resolved = Path(os.path.abspath(path))
    try:
        real = resolved.parent.resolve() / resolved.name
    except OSError:
        real = resolved
    if not (real == base or real.is_relative_to(base)):
        raise util.usage("refusing a tracker path outside the tracker root: " + str(path))
    return path


def write_text(path: Path, text: str) -> None:
    inside_root(path)
    if selected() == "linear":
        # Linear first, then the local cache, then the cache's synced revision: a Linear failure (exit 8)
        # leaves nothing written locally, and a crash before the revision is recorded leaves the next
        # refresh to adopt what Linear holds.
        import os
        from .linear import adapter
        after = adapter.push(Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root()))).as_posix(),
                             text)
        try:
            util.atomic_write(path, text)
        except OSError as error:
            raise YggError(util.EXIT_TRACKER, "tracker write failed: " + str(error))
        adapter.synced(after)
        return
    try:
        util.atomic_write(path, text)
    except OSError as error:
        raise YggError(util.EXIT_TRACKER, "tracker write failed: " + str(error))


def write_json(path: Path, data) -> None:
    import json
    write_text(path, json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------- counters

def counters_path() -> Path:
    return root() / "_ygg" / "counters.json"


def _read_counters() -> dict:
    """Counters, recomputed from existing records when the file is unreadable (never reset to zero)."""
    import json
    path = counters_path()
    if not path.exists():
        return recompute_counters()
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not all(isinstance(v, int) for v in data.values()):
            raise ValueError("counters must map kinds to integers")
        return data
    except (OSError, ValueError):
        from . import state
        state.alert("ygg-tracker-corrupt", "counter file unreadable; recomputed from existing records",
                    path=str(path))
        return recompute_counters()


def recompute_counters() -> dict:
    import re as _re
    highest: dict = {}
    by_prefix = {prefix: kind for kind, (prefix, _) in COUNTER_PREFIX.items()}

    def note(name: str) -> None:
        match = _re.match(r"^(D-|E-|MG-|EN-|I-|m-|act-|T-)([0-9]+)", name)
        if match:
            kind = by_prefix[match[1]]
            highest[kind] = max(highest.get(kind, 0), int(match[2]))

    base = root()
    for pattern in ("projects/*/issues/*.md", "initiatives/*/morning-go/*.json", "projects/*/envelopes/*.json",
                    "initiatives/*/envelopes/*.json"):
        for path in base.glob(pattern):
            note(path.stem)
    for path in (util.state_root() / "activations").glob("act-*.json"):
        note(path.stem)
    for line in util.read_lines(util.state_root() / "messages.jsonl"):
        note(str(line.get("msg_id") or ""))
    return highest


def _taken(kind: str, identifier: str) -> bool:
    base = root()
    if kind in ("decision", "exception", "issue", "task"):
        return any(base.glob("projects/*/issues/" + identifier + ".md"))
    if kind == "morning-go":
        return any(base.glob("initiatives/*/morning-go/" + identifier + ".json"))
    if kind == "envelope":
        return any(base.glob("*/*/envelopes/" + identifier + ".json"))
    if kind == "activation":
        return (util.state_root() / "activations" / (identifier + ".json")).exists()
    return False


def allocate(kind: str) -> str:
    prefix, width = COUNTER_PREFIX[kind]
    with util.locked():
        counters = _read_counters()
        value = int(counters.get(kind, 0)) + 1
        while _taken(kind, prefix + str(value).zfill(width)):
            value += 1
        counters[kind] = value
        write_json(counters_path(), counters)
    return prefix + str(value).zfill(width)


def set_next(kind: str, number: int) -> None:
    if kind not in COUNTER_PREFIX:
        raise util.usage("unknown counter kind '" + kind + "'")
    with util.locked():
        counters = _read_counters()
        current = int(counters.get(kind, 0))
        if number - 1 < current:
            raise util.usage("cannot lower counter " + kind + " below " + str(current + 1))
        counters[kind] = number - 1
        write_json(counters_path(), counters)


# ---------------------------------------------------------------- programmes and projects

def programme_path(name: str) -> Path:
    return root() / "initiatives" / name / "initiative.json"


def programme(name: str) -> dict | None:
    path = programme_path(name)
    return _checked(path, read_record(path), PROGRAMME_IDS) if path.exists() else None


def programmes() -> list[dict]:
    base = root() / "initiatives"
    if not base.is_dir():
        return []
    found = []
    for path in sorted(base.glob("*/initiative.json")):
        found.append(_checked(path, read_record(path), PROGRAMME_IDS))
    return found


def save_programme(data: dict) -> None:
    write_json(programme_path(data["id"]), data)


def project_dir(project: str) -> Path:
    return root() / "projects" / project


def project(name: str) -> dict | None:
    path = project_dir(name) / "project.json"
    return _checked(path, read_record(path), PROJECT_IDS) if path.exists() else None


def projects() -> list[dict]:
    base = root() / "projects"
    if not base.is_dir():
        return []
    found = []
    for path in sorted(base.glob("*/project.json")):
        found.append(_checked(path, read_record(path), PROJECT_IDS))
    return found


def owe(record: dict, node_id: str | None, reason: str) -> None:
    """Stamp an obligation onto a record (a project record or an issue header) before it is saved, so the
    record and the launch it owes are one write. Each stamp has its own identity; it stays owed until an
    activation of that node given it is spawned and does not end aborted (derived by the reducer)."""
    import uuid
    if not node_id:
        return
    stamps = record.setdefault("owed", [])
    stamps.append({"id": "o-" + uuid.uuid4().hex[:12], "node": node_id, "reason": reason, "at": util.iso()})


def save_project(data: dict) -> None:
    write_json(project_dir(data["id"]) / "project.json", data)


def nodes_file(project: str | None = None, programme_name: str | None = None) -> Path:
    if project:
        return project_dir(project) / "nodes.json"
    return root() / "initiatives" / programme_name / "nodes.json"


NODE_ROLES = ("tl", "su", "worker", "reviewer")
# A project's nodes file holds its team; reviewers belong to the programme's nodes file, parented to it.
PROJECT_ROLES = ("tl", "su", "worker", "reviewer")  # a project reviewer (`<pm>rv1`)
PROGRAMME_ROLES = ("reviewer",)


def _valid_id(value) -> bool:
    return isinstance(value, str) and util.ID_RE.fullmatch(value) is not None


def node_entries(path: Path, roles: tuple = PROJECT_ROLES, default_role: str | None = None) -> list[dict]:
    """Registered nodes of one file. Ids, roles, parents and pairings are checked when read, so a malformed
    entry can never pick a prompt file or claim a delegation."""
    import json
    if not Path(path).exists():
        return []
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise corrupt(path, str(error))
    nodes = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(nodes, list) or not all(isinstance(n, dict) and _valid_id(n.get("id")) for n in nodes):
        raise corrupt(path, "expected {\"nodes\": [{\"id\": ...}]}")
    for entry in nodes:
        # Project nodes must name their role; a programme's nodes file may omit it for its reviewer.
        role = entry.get("role", default_role)
        if role not in roles:
            raise corrupt(path, "node " + entry["id"] + " has role " + str(role) + ", which this file cannot hold")
        if "parent" in entry and not _valid_id(entry.get("parent")):
            raise corrupt(path, "node " + entry["id"] + " has an invalid parent id")
        pairs = entry.get("pairs_with", [])
        if pairs is None:
            pairs = []
        if not isinstance(pairs, list) or not all(_valid_id(p) for p in pairs):
            raise corrupt(path, "node " + entry["id"] + " has an invalid pairs_with list")
    return list(nodes)


def _checked(path: Path, data: dict, fields: tuple) -> dict:
    """A project or programme record whose required id fields are present and valid ids; anything else is a
    corrupt record."""
    for field in fields:
        if not _valid_id(data.get(field)):
            raise corrupt(path, "field " + field + " is not a valid id: " + repr(data.get(field)))
    return data


PROJECT_IDS = ("id", "pm", "programme")
PROGRAMME_IDS = ("id",)


def programme_home(programme: dict) -> str | None:
    """A programme's home folder as an absolute path. A relative home resolves against the folder that holds
    the tracker root, never against the current folder."""
    home = programme.get("home") if programme else None
    if not home:
        return None
    text = os.path.expanduser(str(home))
    if not os.path.isabs(text):
        text = os.path.join(str(root().resolve().parent), text)
    return os.path.realpath(text)


def add_node_entry(path: Path, entry: dict) -> None:
    data = util.read_json(path, {}) or {}
    data.setdefault("schema", "ygg.nodes.v1")
    nodes = [n for n in data.get("nodes", []) if n.get("id") != entry["id"]]
    nodes.append(entry)
    data["nodes"] = nodes
    write_json(path, data)


# ---------------------------------------------------------------- milestones

def milestones(project: str) -> dict:
    data = util.read_json(project_dir(project) / "milestones.json")
    if not isinstance(data, dict):
        data = {"schema": "ygg.milestones.v1", "standard_version": STANDARD_VERSION, "stages": []}
    return data


def save_milestones(project: str, data: dict) -> None:
    write_json(project_dir(project) / "milestones.json", data)


# ---------------------------------------------------------------- documents

def document_path(project: str, slug: str) -> Path:
    return project_dir(project) / "documents" / (slug + ".md")


def read_document(project: str, slug: str) -> str:
    return util.read_text(document_path(project, slug))


def write_document(project: str, slug: str, text: str) -> Path:
    path = document_path(project, slug)
    write_text(path, text)
    return path


def updates_dir(project: str) -> Path:
    return project_dir(project) / "updates"


def write_update(project: str, suffix: str, text: str) -> Path:
    base = updates_dir(project)
    stamp = util.compact()
    path = base / (stamp + suffix + ".md")
    counter = 1
    while path.exists():
        counter += 1
        path = base / (stamp + "-" + str(counter) + suffix + ".md")
    write_text(path, text)
    return path


# ---------------------------------------------------------------- issues

class Issue:
    def __init__(self, project: str, identifier: str, header: dict, records=None, comments=None, body=""):
        self.project = project
        self.id = identifier
        self.header = header
        self.records = records or []
        self.comments = comments or []
        self.body = body

    @property
    def path(self) -> Path:
        return issue_path(self.project, self.id)

    def get(self, key, default=None):
        return self.header.get(key, default)

    def render(self) -> str:
        text = "---\n" + util.dump_yaml(self.header) + "---\n"
        if self.body.strip():
            text += "\n" + util.escape_sections(self.body.strip("\n")) + "\n"
        text += "\n## Records\n\n"
        for record in self.records:
            text += "```yaml\n" + util.dump_yaml(record) + "```\n\n"
        text += "## Comments\n\n"
        for comment in self.comments:
            text += "```yaml\n" + util.dump_yaml(comment) + "```\n\n"
        return text

    def save(self) -> None:
        write_text(self.path, self.render())

    def labels(self) -> list:
        value = self.header.get("labels") or []
        return list(value) if isinstance(value, list) else [value]

    def add_label(self, label: str) -> None:
        labels = self.labels()
        if label not in labels:
            labels.append(label)
        self.header["labels"] = labels


def issue_path(project: str, identifier: str) -> Path:
    return project_dir(project) / "issues" / (identifier + ".md")


_BLOCK = re.compile(r"```yaml[ \t]*\n(.*?)^```", re.S | re.M)


def parse_issue(project: str, identifier: str, text: str) -> Issue:
    """Records and comments live only under the exact section lines that render() emits."""
    header, rest = util.split_frontmatter(util.normalise_newlines(text))
    header = header or {}
    records, comments, body_parts = [], [], []
    section = None
    last = 0
    parts = []
    for match in util.SECTION_LINE.finditer(rest):
        parts.append((section, rest[last:match.start()]))
        section = match[1]
        last = match.end()
    parts.append((section, rest[last:]))
    for name, chunk in parts:
        if name is None:
            body_parts.append(chunk)
            continue
        target = records if name == "Records" else comments
        for block in _BLOCK.findall(chunk):
            try:
                value = yaml.safe_load(block)
            except yaml.YAMLError:
                continue
            if isinstance(value, dict):
                target.append(value)
    return Issue(project, identifier, header, records, comments, "".join(body_parts))


def read_issue(project: str, identifier: str) -> Issue | None:
    path = issue_path(project, identifier)
    if not path.exists():
        return None
    return parse_issue(project, identifier, path.read_text())


def issues(project: str) -> list[Issue]:
    base = project_dir(project) / "issues"
    if not base.is_dir():
        return []
    return [parse_issue(project, path.stem, path.read_text()) for path in sorted(base.glob("*.md"))]


def find_package(identifier: str, project_name: str | None = None) -> Issue | None:
    candidates = [project_name] if project_name else [p["id"] for p in projects()]
    for name in candidates:
        if not name:
            continue
        issue = read_issue(name, identifier)
        if issue is not None:
            return issue
    return None


def new_issue(project: str, identifier: str, kind: str, labels: list, node: str | None,
              actor: str = "ygg-app", **fields) -> Issue:
    from . import standard
    header = {"id": identifier, "kind": kind, "labels": labels, "standard_version": standard.current(),
              "actor": actor}
    if node:
        header["node"] = node
    header["created_at"] = util.iso()
    header.update(fields)
    return Issue(project, identifier, header)


def record(kind: str, node: str | None, actor: str = "ygg-app", **fields) -> dict:
    entry = {"kind": kind, "at": util.iso()}
    if node:
        entry["node"] = node
    entry["actor"] = actor
    entry["standard_version"] = STANDARD_VERSION
    entry.update(fields)
    return entry
