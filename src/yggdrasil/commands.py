"""Registry, tracker write and PRINCE2 flow commands."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from . import ratatoskr, state, tracker, util
from .util import STANDARD_VERSION, YggError
# Authorised, unfinished work for the flow WIP limit (A11: in-review counts, stopped and cancelled don't).
from .states import WIP_STATES  # noqa: F401  (re-exported: status, reducer and lanes import it from here)

KINDS = ("git-repo", "shared-tree", "vault-docs")
AUTHORITY_KINDS = {"decided", "approval", "envelope", "relay-grant", "shape", "pid-approval"}
PM_KINDS = {"pid", "pid-ready", "stage-plan", "work-package", "backlog", "highlight", "product-description",
            "end-stage", "end-project", "lessons", "envelope-report", "brief"}
PROTECTED_KEYS = {"kind", "actor", "node", "standard_version", "created_at", "labels", "chosen", "chosen_by",
                  "state", "approved_by", "resolved_by", "resolution", "shape", "grants"}
WRITE_KINDS = {"brief", "pid", "pid-ready", "stage-plan", "product-description", "work-package", "criteria",
               "worker-task", "checkpoint", "highlight", "lessons", "lessons-report", "end-project",
               "envelope-report", "backlog"}
DOC_KINDS = {"brief", "pid", "stage-plan", "product-description", "end-stage", "end-project", "lessons",
             "northstar", "backlog", "envelope-report"}


# ---------------------------------------------------------------- registry

def programme_create(name, domain, home, kind):
    if state.managed_session():
        raise util.refuse("user-only: 'programme create' cannot run in a managed session")
    util.check_id(name, "programme id")
    util.check_id(domain, "domain")
    if kind not in KINDS:
        raise util.usage("unknown kind '" + str(kind) + "'")
    with util.locked():
        tracker.ensure_writable()
        existing = tracker.programme(name) or {}
        data = {"schema": "ygg.initiative.v1", "id": name, "domain": domain,
                "home": os.path.realpath(str(home)), "kind": kind,
                "standard_version": STANDARD_VERSION, "actor": "user",
                "created_at": existing.get("created_at") or util.iso()}
        for key in ("models", "merge_mode", "config"):
            if key in existing:
                data[key] = existing[key]
        tracker.save_programme(data)
    if kind == "vault-docs":
        # D3 (AC-58 row 1): document folders are a preview in v1.0.
        return ("programme " + name + " created\nDocument folders are a preview in this release: packages get no "
                "per-package review yet.\n")
    return "programme " + name + " created\n"


def project_create(name, programme_name, pm):
    if state.managed_session():
        raise util.refuse("user-only: 'project create' cannot run in a managed session")
    util.check_id(name, "project id")
    util.check_id(pm, "node id")
    with util.locked():
        tracker.ensure_writable()
        if tracker.programme(programme_name) is None:
            raise util.usage("unknown programme '" + str(programme_name) + "'")
        nodes = state.all_nodes()
        if pm in nodes and nodes[pm].get("project") != name:
            raise util.usage("node '" + pm + "' already exists")
        existing = tracker.project(name) or {}
        if existing and (existing.get("programme") != programme_name or existing.get("pm") != pm):
            raise util.refuse("project " + name + " already exists in programme " + str(existing.get("programme")) +
                              " with PM " + str(existing.get("pm")))
        data = {"schema": "ygg.project.v1", "id": name, "programme": programme_name, "pm": pm,
                "state": existing.get("state", "initiating"), "hold": existing.get("hold"),
                "shape": existing.get("shape", "standard"), "standard_version": STANDARD_VERSION,
                "actor": "user", "created_at": existing.get("created_at") or util.iso()}
        if existing.get("owed"):
            data["owed"] = existing["owed"]
        tracker.owe(data, pm, "project-created")
        tracker.save_project(data)
        nodes_path = tracker.nodes_file(name)
        if not nodes_path.exists():
            tracker.write_json(nodes_path, {"schema": "ygg.nodes.v1", "nodes": []})
    return "project " + name + " created with PM " + pm + "\n"


def node_add(node_id, role, parent, pairs_with=None):
    util.check_id(node_id, "node id")
    if role not in ("tl", "su", "worker", "reviewer"):
        raise util.usage("unknown role '" + str(role) + "'")
    if role == "reviewer":
        state.check_user_only("node add --role reviewer")
    with util.locked():
        tracker.ensure_writable()
        nodes = state.all_nodes()
        if node_id in nodes:
            raise util.usage("node '" + node_id + "' already exists")
        entry = {"id": node_id, "role": role, "parent": parent,
                 "pairs_with": [p for p in (pairs_with or "").split(",") if p], "created_at": util.iso()}
        if role == "reviewer":
            if entry["pairs_with"]:
                raise util.usage("--pairs-with is not allowed for a reviewer")
            if tracker.programme(parent) is None:
                raise util.usage("reviewer parent must be a programme: '" + str(parent) + "'")
            tracker.add_node_entry(tracker.nodes_file(programme_name=parent), entry)
            return "node " + node_id + " added\n"
        parent_node = nodes.get(parent)
        if parent_node is None:
            raise util.usage("unknown parent '" + str(parent) + "'")
        project = parent_node["project"]
        caller = util.current_node()
        if caller is not None:
            # The user (or L1) may add any node; a managed caller only beneath itself in its own project.
            caller_node = nodes.get(caller)
            if parent != caller or caller_node is None or caller_node.get("project") != project:
                raise util.refuse("identity: " + caller + " may add nodes only beneath itself in its own project")
            if role == "su":
                # The PID has no field naming team nodes, so appointing a senior user stays with the user.
                raise util.refuse("user-only: only the user appoints senior users")
            if role == "tl" and project_collapsed(project):
                raise util.refuse("shape: a collapsed project has no team lead")
        if role in ("tl", "su") and parent_node["role"] != "pm":
            raise util.refuse("topology: a " + role + " must be parented to a PM")
        if role == "worker":
            if parent_node["role"] == "pm":
                if not project_collapsed(project):
                    raise util.refuse("topology: workers under a PM need a user-written collapsed shape record")
            elif parent_node["role"] not in ("tl", "su"):
                raise util.refuse("topology: a worker must be parented to a team lead or senior user")
        for paired in entry["pairs_with"]:
            target = nodes.get(paired, {})
            if target.get("role") != "tl" or target.get("project") != project:
                raise util.usage("--pairs-with must name team leads of project " + str(project) + ": '" +
                                 paired + "'")
        from . import models
        # D1: Codex builds are planned for a later release (A11 seam, AC-99 row 2).
        models.refuse_codex_builder(dict(entry, project=project, programme=parent_node.get("programme")))
        tracker.add_node_entry(tracker.nodes_file(project), entry)
    return "node " + node_id + " added\n"


def project_collapsed(project: str) -> bool:
    text = tracker.read_document(project, "pid")
    issue = tracker.parse_issue(project, "pid", text) if text else None
    if not issue:
        return False
    actors = authority_actors()
    shapes = [r for r in issue.records if r.get("kind") == "shape" and r.get("actor") in actors]
    return bool(shapes) and shapes[-1].get("shape") == "collapsed"


def authority_actors() -> list[str]:
    from . import config
    return list(config.get("authority", {}).get("actors") or ["user"])


# ---------------------------------------------------------------- tracker write

def _project_for(name):
    util.check_id(name, "project id")
    data = tracker.project(name)
    if data is None:
        raise util.usage("unknown project '" + str(name) + "'")
    return data


PID_READY_REFUSAL = "pid-ready is only for a new, rejected or replanned PID"


def _replan_decided(project_name) -> bool:
    """True when the newest follow-up decision of a rejected exception plan was answered `replan`."""
    # The follow-up is found by the `exception` key that only `exception_decide` writes, never by its question
    # text, and only a user's answer counts: an agent can raise a decision with any question, and inside an
    # envelope a provisional decision is answered by the envelope, not by the user.
    def number(identifier) -> int:
        digits = str(identifier or "").rpartition("-")[2]
        return int(digits) if digits.isdigit() else -1

    issues = tracker.issues(project_name)
    exceptions = [i for i in issues if i.get("kind") == "exception"]
    if not exceptions:
        return False
    newest = max(exceptions, key=lambda i: number(i.id))
    if newest.get("state") != "closed" or newest.get("resolution") != "reject":
        return False
    follow_ups = [i for i in issues if i.get("kind") == "decision" and i.get("exception") == newest.id]
    return any(i.get("chosen") == "replan" and i.get("chosen_by") == "user" for i in follow_ups)


def _check_pid_ready_allowed(project):
    current = project.get("state")
    if current in ("initiating", "pid-rejected"):
        return
    if current == "exception":
        if _replan_decided(project["id"]):
            return
        raise util.refuse(project["id"] + " is in exception with no replan decided: " + PID_READY_REFUSAL)
    raise util.refuse(project["id"] + " is " + str(current) + ": " + PID_READY_REFUSAL)


def _actor_node(as_node, project, nodes):
    node = state.get_node(as_node, nodes)
    if node is None:
        raise util.usage("unknown node '" + str(as_node) + "'")
    if node["project"] != project["id"]:
        raise util.refuse("writer: " + as_node + " is not a node of project " + project["id"])
    return node


def wrap_document(kind, node, body, **extra):
    header, rest = util.split_frontmatter(body)
    if header is None:
        header, rest = {}, body
    meta = {"kind": kind}
    meta.update({k: v for k, v in header.items() if k not in PROTECTED_KEYS})
    meta.update(kind=kind, standard_version=STANDARD_VERSION, actor="ygg-app", node=node,
                created_at=util.iso())
    meta.update(extra)
    return util.render_frontmatter(meta, util.escape_sections(rest))


def tracker_write(as_node, project_name, kind, body, stage=None, package=None, worker=None):
    nodes = state.all_nodes()
    state.check_identity(as_node, nodes)
    if kind in AUTHORITY_KINDS:
        raise util.refuse("authority kind: '" + kind + "' is written only by user verbs")
    if kind not in WRITE_KINDS:
        raise util.usage("unknown tracker kind '" + str(kind) + "'; known: " + ", ".join(sorted(WRITE_KINDS)))
    project = _project_for(project_name)
    node = _actor_node(as_node, project, nodes)
    if node["role"] == "worker":
        raise util.refuse("writer: workers (L4) cannot write to the tracker")
    if node["role"] == "reviewer":
        raise util.refuse("reviewer: reviewers write only the morning go")
    if kind in PM_KINDS and node["role"] != "pm":
        raise util.refuse("writer: only the PM writes '" + kind + "'")
    body = util.redact(body or "")
    stage = util.check_stage(stage)
    if package is not None:
        util.check_record_id(package)
    if worker is not None:
        util.check_id(worker, "worker id")
    pid_name = project["id"]
    with util.locked():
        tracker.ensure_writable()
        if kind == "work-package":
            if not package or not re.match(r"^WP-[0-9]+$", package):
                raise util.usage("--package WP-<n> is required for a work package")
            existing = tracker.read_issue(pid_name, package)
            title = body.strip().splitlines()[0].lstrip("# ").strip() if body.strip() else package
            if existing is None:
                issue = tracker.new_issue(pid_name, package, "work-package", [], as_node, title=title,
                                          lane=None, state="planned", depends_on=[], models={},
                                          milestone=int(stage) if stage else None)
            else:
                issue = existing
                issue.header["title"] = title
                issue.header["updated_at"] = util.iso()  # a rewrite is a change (the blockers line's age, AC-19)
                if stage:
                    issue.header["milestone"] = int(stage)
            issue.body = body
            if issue.get("lane"):
                for su in paired_seniors(issue.get("lane"), nodes):
                    tracker.owe(issue.header, su, "package-written:" + package)
            issue.save()
            return "work package " + package + " written\n"
        if kind == "criteria":
            state.check_self(as_node, "criteria", "written")
            issue = _package(pid_name, package)
            if node["role"] not in ("su", "pm"):
                raise util.refuse("writer: criteria are written by a senior user")
            issue.records.append(tracker.record("criteria", as_node, body=body, caller=state.caller_id()))
            if issue.get("lane"):
                tracker.owe(issue.header, issue.get("lane"), "criteria:" + package)
            elif issue.get("state", "planned") == "planned" and node["role"] == "su":
                # The PM authorises a package once its criteria exist: criteria on an unauthorised package wake the
                # PM, so a pilot whose senior user answers after the PM's activation still reaches authorisation
                # (A11 AC-37 row 1).
                tracker.owe(issue.header, (tracker.project(pid_name) or {}).get("pm"), "criteria:" + package)
            issue.save()
            return "criteria recorded on " + package + "\n"
        if kind == "worker-task":
            if not package:
                raise util.usage("--package is required for a worker task")
            _package(pid_name, package)
            identifier = tracker.allocate("task")
            title = body.strip().splitlines()[0] if body.strip() else identifier
            issue = tracker.new_issue(pid_name, identifier, "worker-task", [], as_node, title=title,
                                      parent=package, worker=worker)
            issue.body = body
            issue.save()
            return "worker task " + identifier + " written\n"
        if kind == "checkpoint" and package:
            issue = _package(pid_name, package)
            issue.comments.append(tracker.record("checkpoint", as_node, body=body))
            issue.save()
            pm = project["pm"]
            if as_node != pm:
                ratatoskr.send(as_node, pm, "checkpoint", body, internal=True)  # registers the batch entry
            return "checkpoint recorded on " + package + "\n"
        if kind in ("highlight", "checkpoint", "lessons-report"):
            header = {"kind": kind, "standard_version": STANDARD_VERSION, "actor": "ygg-app",
                      "node": as_node, "created_at": util.iso()}
            match = re.search(r"health:\s*(onTrack|atRisk|offTrack)", body)
            if kind == "highlight":
                header["health"] = match[1] if match else "onTrack"
            tracker.write_update(pid_name, "-" + kind, util.render_frontmatter(header, body + "\n"))
            if kind == "highlight":
                # Read again under the lock: the copy read before it may be stale, and saving it back would undo a
                # state change made in between.
                fresh = _project_for(pid_name)
                fresh["health"] = header["health"]
                tracker.save_project(fresh)
            return kind + " written\n"
        if kind == "pid-ready":
            # The first step under the lock: read the project again, check that copy and save that copy, so a
            # state change made between the first read and the lock is neither missed nor overwritten.
            fresh = _project_for(pid_name)
            _check_pid_ready_allowed(fresh)
            fresh["state"] = "pid-ready"
            tracker.save_project(fresh)
            state.alert("ygg-pid-ready", "PID ready for approval: " + pid_name, project=pid_name)
            return "pid ready\n"
        slug = kind
        if kind == "envelope-report":
            header_in, _ = util.split_frontmatter(body)
            envelope_id = str((header_in or {}).get("envelope") or "")
            if not re.fullmatch(r"EN-[0-9]{4,9}", envelope_id):
                raise util.usage("an envelope report needs frontmatter 'envelope: EN-<n>'")
            slug = "envelope-report-" + envelope_id
        if kind in ("stage-plan",) and stage:
            slug = kind + "-" + str(stage)
        if kind == "pid":
            old = tracker.read_document(pid_name, "pid")
            new_text = wrap_document(kind, as_node, body)
            approved = approved_pid(pid_name)
            if approved is not None and _project_for(pid_name).get("state") not in ("initiating", "pid-ready",
                                                                                     "pid-rejected"):
                new_header, _ = util.split_frontmatter(new_text)
                snapshot = approved.get("guarded") or {}
                if _guarded(new_header or {}) != {key: snapshot.get(key) for key in GUARDED_PID_KEYS}:
                    raise util.refuse("gate: changing " + ", ".join(GUARDED_PID_KEYS) +
                                      " after PID approval needs a new approval")
            # A PID write never sets or replaces records: any `## Records` section in the written text is dropped and
            # the stored records are carried forward, so an approval record comes only from `ygg pid approve`.
            new_text = re.sub(r"(?ms)^## Records[ \t]*\n.*?(?=^## |\Z)", "", new_text).rstrip("\n") + "\n"
            if old:
                previous = tracker.parse_issue(pid_name, "pid", old)
                if previous.records:
                    new_text = new_text.rstrip("\n") + "\n\n## Records\n\n" + "".join(
                        "```yaml\n" + util.dump_yaml(r) + "```\n\n" for r in previous.records)
            tracker.write_document(pid_name, "pid", new_text)
            return "pid written\n"
        tracker.write_document(pid_name, slug, wrap_document(kind, as_node, body))
        return kind + " written\n"


def _package(project, package):
    if not package:
        raise util.usage("--package is required")
    util.check_record_id(package)
    issue = tracker.read_issue(project, package)
    if issue is None or issue.get("kind") != "work-package":
        raise util.usage("unknown work package '" + str(package) + "'")
    return issue


def paired_seniors(lane, nodes):
    """Senior users paired with a lane, scoped to the lane's own project."""
    project = (nodes.get(lane) or {}).get("project")
    if not project:
        return []
    return [n["id"] for n in nodes.values() if n.get("role") == "su" and n.get("project") == project
            and lane in (n.get("pairs_with") or [])]


ROUTE_SECTIONS = ["Destination", "Frontier", "Blockers", "Decisions so far", "Fog", "Out of scope"]


def managed_input(file) -> Path:
    """The path of a file a command reads in. A managed caller may hand in only files inside the tracker or
    its own work area (symlinks resolved), so it cannot feed Yggdrasil a file from elsewhere."""
    path = Path(file)
    if not path.is_absolute():
        path = Path.cwd() / path
    caller = util.current_node()
    if caller is None:
        return path
    real = os.path.realpath(path)
    nodes = state.all_nodes()
    roots = [tracker.root()]
    if caller in nodes and nodes[caller].get("programme"):
        from . import launcher
        roots.append(launcher.work_area(nodes[caller], nodes))
    for root in roots:
        base = os.path.realpath(root)
        if os.path.commonpath([real, base]) == base:
            return path
    raise util.refuse("report must be inside the tracker or your worktree")


BODY_FILE_LIMIT = 1024 * 1024


BODY_FILE_OUTSIDE = "body file must be inside your worktree"
BODY_FILE_TRACKER = "body file must not be a tracker file"
BODY_FILE_CHANGED = "body file changed while it was read; write it again"
BODY_FILE_LINKS = "body file must have one link; write it again"


def _tracker_root():
    """The tracker root the body-file check keeps a managed session out of (its own function, so a test can name
    another folder without changing where the tracker is read)."""
    return tracker.root()


def _identity(path) -> tuple:
    found = os.stat(path)
    return found.st_dev, found.st_ino


def _within(folder: str, root: tuple) -> bool:
    """Whether `folder`, or a folder above it, is the folder with identity `root` (device and inode): proof against
    case-insensitive spellings and other names for the same folder, which comparing path strings isn't."""
    current = folder
    while True:
        try:
            if _identity(current) == root:
                return True
        except OSError:
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def read_body_file(file) -> str:
    """The text of `--body-file`, a regular UTF-8 text file of at most 1 MB.

    The user (not a managed session) may name any readable file. A managed session may name only a file inside
    its own work area (its worktree or staging folder), and never one inside the tracker root, even where the two
    overlap: the tracker holds other programmes' and projects' records, and a body is sent on. A managed session
    without a node it can be resolved to is refused. The file that is checked is the file that is read: its folder
    is resolved first, the file itself is opened once without following a link (O_NOFOLLOW), its location is
    checked again from what was opened (device and inode against the resolved path, and the folder resolved
    again), and the text is read from that same descriptor."""
    import errno
    import stat as stat_module
    path = Path(file)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.name or path.name in (".", ".."):
        raise util.usage("body file must name a file: " + str(file))
    managed = state.managed_session()
    folder = os.path.realpath(path.parent)
    real = os.path.join(folder, path.name)
    area = None
    if managed:
        caller = util.current_node()
        nodes = state.all_nodes()
        node = nodes.get(caller) if caller else None
        if node is None or not node.get("programme"):
            raise util.refuse("body file: this managed session has no node identity Yggdrasil can resolve")
        from . import launcher
        area = launcher.work_area(node, nodes)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    else:
        real = os.path.realpath(path)  # the user's own file: a link is followed once, here
        flags = os.O_RDONLY
    try:
        descriptor = os.open(real, flags | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        raise util.usage("body file not found: " + str(file)) from None
    except OSError as error:
        if managed and error.errno in (errno.ELOOP, errno.EMLINK):
            raise util.refuse(BODY_FILE_OUTSIDE + ": " + str(file) + " is a symbolic link") from None
        raise util.usage("body file can't be read: " + str(file) + " (" + str(error.strerror) + ")") from None
    try:
        opened = os.fstat(descriptor)
        if not stat_module.S_ISREG(opened.st_mode):
            raise util.usage("body file is not a regular file: " + str(file))
        if opened.st_size > BODY_FILE_LIMIT:
            raise util.usage("body file is larger than 1 MB: " + str(file))
        # One link only, for everyone: a hard link inside the work area to a file elsewhere (a tracker record)
        # would otherwise pass the folder checks.
        if opened.st_nlink != 1:
            raise util.refuse(BODY_FILE_LINKS)
        if managed:
            # The folder could have been swapped for a link between the check and the open: what was opened must
            # still be the checked path, in the checked folder.
            try:
                now = os.lstat(real)
            except OSError:
                raise util.refuse(BODY_FILE_CHANGED) from None
            if (now.st_dev, now.st_ino) != (opened.st_dev, opened.st_ino) or \
                    os.path.realpath(path.parent) != folder:
                raise util.refuse(BODY_FILE_CHANGED)
            # Where the opened file lies, by folder identity rather than by name (case-insensitive volumes): inside
            # its own work area, and never inside the tracker root, even where the two overlap. A false refusal is
            # fine; a false accept isn't.
            try:
                inside_area = _within(folder, _identity(area))
            except OSError:
                inside_area = False
            if not inside_area:
                raise util.refuse(BODY_FILE_OUTSIDE)
            try:
                in_tracker = _within(folder, _identity(_tracker_root()))
            except OSError:
                in_tracker = False  # no tracker folder to be inside of
            if in_tracker:
                raise util.refuse(BODY_FILE_TRACKER)
        chunks, total = [], 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > BODY_FILE_LIMIT:
                raise util.usage("body file is larger than 1 MB: " + str(file))
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise util.usage("body file is not UTF-8 text: " + str(file)) from None
    if not text.strip():
        raise util.usage("body file is empty: " + str(file))
    return text


def routemap_write(as_node, project_name, file):
    nodes = state.all_nodes()
    state.check_identity(as_node, nodes)
    project = _project_for(project_name)
    node = _actor_node(as_node, project, nodes)
    if node["role"] != "pm":
        raise util.refuse("writer: only the PM writes the route map")
    path = managed_input(file)
    text = util.read_text(path)
    if not text:
        raise util.usage("route map file not found: " + str(file))
    header, rest = util.split_frontmatter(text)
    if header is None:
        header, rest = {}, text
    found = re.findall(r"(?m)^## (.+?)\s*$", rest)
    for section in ROUTE_SECTIONS:
        if section not in found:
            raise util.usage("route map is missing section '" + section + "'")
    meta = {k: v for k, v in header.items() if k not in PROTECTED_KEYS}
    meta.update(schema="ygg.routemap.v1", project=project["id"], standard_version=STANDARD_VERSION,
                actor="ygg-app", node=as_node, kind="route-map", written_at=util.iso())
    with util.locked():
        tracker.ensure_writable()
        tracker.write_document(project["id"], "route-map",
                               util.render_frontmatter(meta, util.escape_sections(util.redact(rest))))
    return "route map written\n"


# ---------------------------------------------------------------- PID and stages

def pid_document(project):
    text = tracker.read_document(project, "pid")
    if not text:
        raise util.usage("project " + project + " has no PID document")
    return tracker.parse_issue(project, "pid", text)


def validate_pid(header: dict) -> None:
    for key in ("schema", "project", "programme", "repos", "stages"):
        if key not in header:
            raise util.usage("PID is missing required field '" + key + "'")
    if header.get("draft"):
        raise util.usage("PID is a draft and cannot be approved: field 'draft'")
    stages = header.get("stages")
    if not isinstance(stages, list) or not stages:
        raise util.usage("PID field 'stages' needs at least one stage")
    for index, stage in enumerate(stages, start=1):
        if not isinstance(stage, dict) or stage.get("number") != index or not stage.get("planned_end"):
            raise util.usage("PID field 'stages' entry " + str(index) + " needs number and planned_end")
    tolerances = header.get("tolerances")
    if not isinstance(tolerances, dict) or not tolerances.get("time"):
        raise util.usage("PID is missing required field 'tolerances.time'")
    approach = header.get("delivery_approach")
    if approach is not None and approach not in ("agile", "waterfall", "custom"):
        raise util.usage("PID field 'delivery_approach' is unknown: " + str(approach))
    if approach == "custom" and not header.get("delivery_description"):
        raise util.usage("PID field 'delivery_description' is required when delivery_approach is custom")
    cadence = header.get("cadence")
    if cadence is not None and cadence not in ("flow", "timebox"):
        raise util.usage("PID field 'cadence' is unknown: " + str(cadence))
    if "timebox_hours" in header:
        # An explicit PID value must be valid; whether a duration is required at all is judged on the
        # effective, inherited settings by validate_delivery (a programme may supply it).
        hours = header.get("timebox_hours")
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 336:
            raise util.usage("PID field 'timebox_hours' must be an integer from 1 to 336")
    from .workahead import normalise
    work_ahead = normalise(header.get("work_ahead"))
    if work_ahead is not None and work_ahead not in ("off", "conditional"):
        raise util.usage("PID field 'work_ahead' must be off or conditional, not '" + str(work_ahead) + "'")
    if "business_case" not in header:
        raise util.usage("PID is missing required field 'business_case'")
    case = header.get("business_case")
    if case == "skipped":
        if not header.get("business_case_skip_reason"):
            raise util.usage("PID field 'business_case_skip_reason' is required when business_case is skipped")
    elif isinstance(case, dict):
        for key in ("why", "benefit", "cost", "risks", "stop_if"):
            if key not in case:
                raise util.usage("PID is missing required field 'business_case." + key + "'")
    else:
        raise util.usage("PID field 'business_case' must be an object or 'skipped'")


def validate_delivery(project_name: str) -> None:
    """The effective, inherited delivery settings (programme config, PID, file layers): a timebox cadence
    needs a valid timebox_hours from some layer."""
    from .status import effective_delivery, timebox_hours
    approach, _, cadence = effective_delivery(project_name)
    if approach == "agile" and cadence == "timebox" and timebox_hours(project_name) is None:
        raise util.usage("PID field 'timebox_hours' must be an integer from 1 to 336 (the effective cadence is "
                         "timebox)")


SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def origin_slug(path) -> str | None:
    if not path:
        return None
    result = util.trusted_git("-C", path, "remote", "get-url", "origin")
    match = re.search(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$", result.stdout.strip())
    return match[1] if match else None


def protection_problems(body: str) -> list[str]:
    """What a branch-protection answer fails to prove: admins bound, no force pushes, no deletions, and at
    least one required review. A bare "protection exists" answer proves none of it."""
    import json
    try:
        policy = json.loads(body)
    except ValueError:
        return ["protection answer is not JSON"]
    if not isinstance(policy, dict):
        return ["protection answer is not an object"]

    def enabled(field):
        value = policy.get(field)
        return value.get("enabled") if isinstance(value, dict) else None

    problems = []
    if enabled("enforce_admins") is not True:
        problems.append("enforce_admins is not enabled")
    if enabled("allow_force_pushes") is not False:
        problems.append("allow_force_pushes is not disabled")
    if enabled("allow_deletions") is not False:
        problems.append("allow_deletions is not disabled")
    reviews = policy.get("required_pull_request_reviews")
    count = reviews.get("required_approving_review_count") if isinstance(reviews, dict) else None
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        problems.append("required_pull_request_reviews does not require an approving review")
    return problems


def protection_status(repo: dict) -> tuple[str, str]:
    """Return (status, detail) where status is protected, insufficient, unprotected or unknown."""
    declared = repo.get("github")
    derived = origin_slug(repo.get("path"))
    if declared and derived and str(declared).lower() != derived.lower():
        return "unknown", "declared github " + str(declared) + " does not match origin " + derived
    slug = declared or derived
    if not slug:
        return "unknown", "no GitHub repository named"
    if not SLUG_RE.match(str(slug)):
        return "unknown", "invalid GitHub repository name " + str(slug)
    gh = util.env("YGG_GH_BIN", "gh")
    try:
        result = subprocess.run([gh, "api", "repos/" + str(slug) + "/branches/main/protection"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return "unknown", str(error)
    classic = None
    if result.returncode == 0:
        problems = protection_problems(result.stdout)
        if not problems:
            return "protected", ""
        classic = "; ".join(problems)
    elif "404" not in result.stderr:
        return "unknown", result.stderr.strip()
    # Repository rulesets protect a branch too, and GitHub reports them on their own endpoint.
    status, detail = ruleset_status(gh, str(slug))
    if status == "protected":
        return status, ""
    if status == "unknown":
        return status, detail
    if classic is not None:
        return "insufficient", classic
    if status == "insufficient":
        return status, detail
    return "unprotected", "main is not protected"


def ruleset_status(gh: str, slug: str) -> tuple[str, str]:
    """Whether active rulesets that nobody may bypass (administrators included) protect main the same way:
    no force pushes, no deletion, at least one approving review. (protected | insufficient | none | unknown)"""
    import json

    def api(path: str):
        try:
            run = subprocess.run([gh, "api", path], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            return None, str(error)
        if run.returncode != 0:
            return None, run.stderr.strip()
        try:
            return json.loads(run.stdout), ""
        except ValueError:
            return None, "ruleset answer is not JSON"

    rules, error = api("repos/" + slug + "/rules/branches/main")
    if rules is None:
        return ("none", "") if "404" in error else ("unknown", error)
    if not isinstance(rules, list) or not rules:
        return "none", ""
    binding: dict = {}
    for rule in rules:
        ident = rule.get("ruleset_id") if isinstance(rule, dict) else None
        if ident is None or ident in binding:
            continue
        detail, error = api("repos/" + slug + "/rulesets/" + str(ident))
        if detail is None or not isinstance(detail, dict):
            return "unknown", error or "ruleset answer is not an object"
        bypass = detail.get("bypass_actors")
        if not isinstance(bypass, list):
            # A token that cannot see the bypass list proves nothing: never assume nobody may bypass.
            return "unknown", ("ruleset " + str(ident) + ": the token cannot see its bypass list "
                               "(bypass_actors is missing)")
        binding[ident] = detail.get("enforcement") == "active" and bypass == [] and \
            detail.get("current_user_can_bypass") == "never"
    found = {"non_fast_forward": False, "deletion": False, "review": False}
    for rule in rules:
        if not isinstance(rule, dict) or not binding.get(rule.get("ruleset_id")):
            continue
        kind = rule.get("type")
        if kind in ("non_fast_forward", "deletion"):
            found[kind] = True
        elif kind == "pull_request":
            count = (rule.get("parameters") or {}).get("required_approving_review_count")
            if isinstance(count, int) and not isinstance(count, bool) and count >= 1:
                found["review"] = True
    missing = [{"non_fast_forward": "force pushes are not blocked by a ruleset nobody may bypass",
                "deletion": "deletion is not blocked by a ruleset nobody may bypass",
                "review": "no ruleset nobody may bypass requires an approving review"}[k]
               for k, ok in found.items() if not ok]
    return ("insufficient", "; ".join(missing)) if missing else ("protected", "")


def protection_mode(header: dict, repo: dict) -> str:
    """The first branch_protection value set (repo entry, PID, configuration), else refuse. A value that is
    not one of the three modes (an unquoted YAML boolean included) refuses the approval with the fix."""
    from . import config
    for value in (repo.get("branch_protection"), header.get("branch_protection"), config.get("branch_protection")):
        if value is None:
            continue
        problem = config.protection_problem(value)
        if problem:
            raise util.usage(problem)
        return str(value)
    return "refuse"


def pid_approve(project_name):
    state.check_user_only("pid approve")
    project = _project_for(project_name)
    if project.get("state") not in ("initiating", "pid-ready"):
        raise util.refuse("gate: project " + project_name + " is " + str(project.get("state")))
    issue = pid_document(project_name)
    header = issue.header
    validate_pid(header)
    validate_delivery(project_name)
    from . import models
    models.refuse_codex_project(project_name)  # D1: Codex builds are planned for a later release (A11 seam, AC-99 row 1)
    programme = tracker.programme(project["programme"]) or {}
    try:
        from . import mergecheck  # the merge-check module owns the rule (A11 AC-85 row 1)
        mergecheck.refuse_repo_count(project_name, header)
    except (ImportError, AttributeError):
        pass
    if programme.get("kind") in ("git-repo", "shared-tree"):
        # One repo per git project (A11 AC-85 row 1); more are planned for a later release.
        repos = [r for r in header.get("repos") or [] if isinstance(r, dict)]
        if len(repos) > 1:
            raise util.refuse("projects with more than one repo are not in this release")
        if not repos:
            raise util.refuse("a git project needs one repo")
    warnings = []
    for repo in header.get("repos") or []:
        status, detail = protection_status(repo)
        if status == "protected":
            continue
        mode = protection_mode(header, repo)
        if mode == "off":
            continue
        message = "branch protection on main is " + status + " for " + str(repo.get("path"))
        if detail:
            message += " (" + detail + ")"
        if mode == "warn":
            warnings.append(message)
            continue
        raise util.refuse("gate: " + message)
    with util.locked():
        tracker.ensure_writable()
        issue = pid_document(project_name)
        validate_pid(issue.header)
        validate_delivery(project_name)
        if _guarded(issue.header) != _guarded(header) or issue.header.get("stages") != header.get("stages"):
            raise util.refuse("gate: the PID changed while it was being approved; approve again")
        header = issue.header
        project = _project_for(project_name)
        if project.get("state") not in ("initiating", "pid-ready"):
            raise util.refuse("gate: project " + project_name + " is " + str(project.get("state")))
        # A4.1: the approved frontmatter is stored with the approval; guarded fields are read from it.
        approval = tracker.record("pid-approval", None, actor="user", approved_header=dict(header))
        issue.records.append(approval)
        bases = {}
        for repo in header.get("repos") or []:
            if repo.get("path"):
                result = util.trusted_git("-C", repo["path"], "rev-parse", "--verify", "--quiet", "refs/heads/main")
                bases[os.path.realpath(str(repo["path"]))] = result.stdout.strip() or None
        from .gitops import remote_urls
        remotes = {os.path.realpath(str(repo["path"])): remote_urls(repo["path"])
                   for repo in header.get("repos") or [] if repo.get("path")}
        tracker.write_json(tracker.project_dir(project_name) / "pid-approved.json", {
            "schema": "ygg.pid-approved.v1", "project": project_name, "approved_at": util.iso(),
            "actor": "user", "standard_version": STANDARD_VERSION, "guarded": _guarded(header),
            "main_at_approval": bases, "remote_at_approval": remotes})
        tracker.write_text(tracker.document_path(project_name, "pid"), _render_doc(issue))
        if programme.get("kind") in ("git-repo", "shared-tree"):
            for repo in header.get("repos") or []:
                ensure_branch(repo.get("path"), "ygg/" + project_name + "/_integration", "main")
            _add_project_reviewer(project)
        stages = []
        for stage in header["stages"]:
            stages.append({"number": stage["number"], "name": stage.get("name"), "state": "planned",
                           "planned_end": str(stage.get("planned_end")), "started_at": None,
                           "ended_at": None, "approved_by": None, "approval_record": None,
                           "report": None})
        stages[0]["state"] = "executing"
        stages[0]["started_at"] = util.iso()
        project = _project_for(project_name)
        previous = tracker.milestones(project_name)
        data = {"schema": "ygg.milestones.v1", "standard_version": STANDARD_VERSION, "stages": stages}
        if previous.get("owed"):
            data["owed"] = previous["owed"]
        # The milestone record is authoritative: it carries the PM's obligation and the project transition,
        # so a crash before the project write is completed by recovery (`apply_transitions`).
        transition = _transition(data, "pid-approval", project["pm"], "pid-approved")
        tracker.save_milestones(project_name, data)
        _apply_transition(project, transition)
        for message in warnings:
            state.alert("ygg-protection-missing", message, project=project_name)
    return "PID approved for " + project_name + "\n"


def _add_project_reviewer(project: dict) -> None:
    """Amendment 23 (AC-12 row 1): `<pm>rv1`, role reviewer, parent the PM, the project set; git homes only."""
    reviewer = project["pm"] + "rv1"
    nodes = state.all_nodes()
    if reviewer in nodes:
        return
    tracker.add_node_entry(tracker.nodes_file(project["id"]), {
        "id": reviewer, "role": "reviewer", "parent": project["pm"], "pairs_with": [], "created_at": util.iso()})


GUARDED_PID_KEYS = ("repos", "merge_mode", "branch_protection", "programme", "project", "models", "work_ahead",
                    "merge_check_setup")


def _guarded(header: dict) -> dict:
    from .workahead import normalise
    guarded = {key: header.get(key) for key in GUARDED_PID_KEYS}
    guarded["work_ahead"] = normalise(guarded["work_ahead"])  # unquoted YAML `off` is the documented off
    return guarded


def approved_pid(project_name: str) -> dict | None:
    """The guarded PID fields as the user approved them, or None before approval."""
    data = util.read_json(tracker.project_dir(project_name) / "pid-approved.json")
    if not isinstance(data, dict) or data.get("actor") not in authority_actors():
        return None
    return data


def _render_doc(issue) -> str:
    text = "---\n" + util.dump_yaml(issue.header) + "---\n"
    if issue.body.strip():
        text += issue.body.rstrip("\n") + "\n"
    if issue.records:
        text += "\n## Records\n\n" + "".join("```yaml\n" + util.dump_yaml(r) + "```\n\n" for r in issue.records)
    return text


def ensure_branch(repo_path, branch, base):
    if not repo_path:
        return
    check = util.trusted_git("-C", repo_path, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch)
    if check.returncode == 0:
        return
    util.trusted_git("-C", repo_path, "branch", branch, base)


def pid_reject(project_name, note):
    state.check_user_only("pid reject")
    project = _project_for(project_name)
    with util.locked():
        project["state"] = "pid-rejected"
        tracker.save_project(project)
    ratatoskr.send("l1", project["pm"], "pid-rejected", note or "", internal=True)
    return "PID rejected for " + project_name + "\n"


def current_stage(project_name):
    data = tracker.milestones(project_name)
    for stage in data["stages"]:
        if stage.get("state") in ("executing", "awaiting-approval"):
            return data, stage
    return data, None


def stage_end(project_name, within, report, justified=None):
    with util.locked():
        project = _project_for(project_name)
        caller = util.current_node()
        if caller is not None and caller != project["pm"]:
            raise util.refuse("identity: only the PM ends a stage")
        data, stage = current_stage(project_name)
        if stage is None and project.get("state") == "awaiting-approval" and not project.get("hold"):
            # After `stage reject` the PM reworks the rejected stage and ends it again (A1.3 rescope).
            started = [s for s in data["stages"] if s.get("state") != "planned"]
            if started and started[-1].get("state") == "rejected":
                stage = started[-1]
                stage["state"] = "executing"
                project["state"] = "executing"
        if stage is None or stage.get("state") != "executing" or project.get("state") != "executing":
            raise util.refuse("gate: project " + project_name + " has no executing stage")
        path = managed_input(report)
        text = util.read_text(path)
        if not text:
            raise util.usage("report not found: " + str(report))
        number = stage["number"]
        pid_header = pid_document(project_name).header
        case = pid_header.get("business_case")
        with util.locked():
            tracker.ensure_writable()
            tracker.write_document(project_name, "end-stage-" + str(number),
                                   wrap_document("end-stage", project["pm"], util.redact(text), stage=number))
            stage["report"] = "documents/end-stage-" + str(number) + ".md"
            if case == "skipped":
                stage["justified"] = "not-applicable"
            else:
                stage["justified"] = justified or "unstated"
            if case != "skipped" and justified == "no":
                tracker.save_milestones(project_name, data)
                exception_id = _raise_exception(project, "business justification no longer holds",
                                                "review the business case")
                return "exception " + exception_id + " raised\n"
            from . import envelopes
            if within == "no":
                identifier = tracker.allocate("exception")
                record = tracker.new_issue(project_name, identifier, "exception", ["exception"], project["pm"],
                                           to="user", reason="stage " + str(number) + " ended outside tolerance",
                                           plan="the user decides the next step", state="open")
                record.save()
                state.alert("ygg-exception", "Exception " + identifier + " on " + project_name +
                            ": stage ended outside tolerance", project=project_name, record=identifier)
            # A stage the user rejected always returns to the user; envelopes never self-approve it.
            if within == "yes" and not stage.get("rework") and envelopes.covers(project, number):
                stage["state"] = "approved"
                stage["approved_by"] = "envelope"
                stage["ended_at"] = util.iso()
                _start_next(data, number)
                tracker.save_milestones(project_name, data)
                envelopes.morning_go_item(project, "stage-end", "stage-" + str(number))
                return "stage " + str(number) + " self-approved inside the envelope\n"
            stage["state"] = "awaiting-approval"
            stage["ended_at"] = util.iso()
            tracker.save_milestones(project_name, data)
            project = _project_for(project_name)
            project["state"] = "awaiting-approval"
            tracker.save_project(project)
            _stage_end_alerts(project_name, number)
        return "stage " + str(number) + " awaiting approval\n"

def _stage_end_alerts(project_name, number):
    """A stage end waiting for the user: the class-user `ygg-stage-approval` (A11 alert table), and the frozen
    log-class `ygg-stage-ready` record that earlier suites count, raised only when none was raised yet for this
    stage (the ready-to-close alert counts), so the frozen core counts stay at one ("Stage end alerts")."""
    if not _stage_ready_raised(project_name, number):
        state.alert("ygg-stage-ready", "Stage " + str(number) + " of " + project_name + " is ready for approval",
                    project=project_name, stage=number)
    state.alert("ygg-stage-approval", "Stage " + str(number) + " of " + project_name +
                " is ready for your approval", project=project_name, stage=number)


def _stage_ready_raised(project_name, number) -> bool:
    return any(a.get("kind") == "ygg-stage-ready" and a.get("project") == project_name and
               str(a.get("stage")) == str(number) for a in state.alerts())


def _transition(data: dict, name: str, pm: str, reason: str, *, closing: bool = False) -> dict:
    """Record on the milestone record (before it is saved) the project transition an approval implies and
    the launch it owes the PM, so both are in the approval's own write. The final stage's approval closes
    the project and owes nothing (a closed project's nodes owe no work)."""
    import uuid
    transition = {"id": name + ":" + uuid.uuid4().hex[:8], "state": "closed" if closing else "executing",
                  "hold": None}
    if closing:
        transition.update(closed_at=util.iso(), closed_note="all stages approved")
    data["project_transition"] = transition
    if not closing:
        tracker.owe(data, pm, reason)
    return transition


def _apply_transition(project: dict, transition: dict) -> None:
    project["state"] = transition["state"]
    project["hold"] = transition.get("hold")
    for key in ("closed_at", "closed_note"):
        if transition.get(key):
            project[key] = transition[key]
    if transition["state"] == "closed":
        project["closed_premature"] = False
    project["applied_transition"] = transition["id"]
    tracker.save_project(project)


def apply_transitions() -> None:
    """Recovery: complete a project transition whose approval was saved on the milestone record but whose
    project write was lost (idempotent by transition id)."""
    with util.locked():
        for project in tracker.projects():
            transition = tracker.milestones(project["id"]).get("project_transition")
            if isinstance(transition, dict) and transition.get("id") and \
                    project.get("applied_transition") != transition["id"]:
                _apply_transition(project, transition)


def _start_next(data, number):
    for stage in data["stages"]:
        if stage["number"] == number + 1 and stage.get("state") == "planned":
            stage["state"] = "executing"
            stage["started_at"] = util.iso()
            return stage
    return None


def stage_approve(project_name):
    with util.locked():
        state.check_user_only("stage approve")
        project = _project_for(project_name)
        with util.locked():
            tracker.ensure_writable()
            if project.get("hold") == "envelope-revoked":
                project["hold"] = None
                project["state"] = "executing"
                tracker.owe(project, project["pm"], "hold-lifted")
                tracker.save_project(project)
                return "hold lifted for " + project_name + "\n"
            data, stage = current_stage(project_name)
            if stage is None or stage.get("state") != "awaiting-approval":
                raise util.refuse("gate: project " + project_name + " has no stage awaiting approval")
            number = stage["number"]
            stage["state"] = "approved"
            stage["approved_by"] = "user"
            stage["approval_record"] = "stage-approval:" + str(number)
            following = _start_next(data, number)
            # Approving the final stage ends the project: it closes in the same transition, so its state
            # never reads as in progress with nothing left to do.
            final = following is None and not any(s.get("state") == "planned" for s in data["stages"])
            kind = (tracker.programme(project.get("programme")) or {}).get("kind")
            if final and kind in ("git-repo", "shared-tree"):
                # D4: a git project stays executing after its last approval until every package
                # is merged and the merge check passes; it closes only once main contains its work.
                import uuid
                transition = {"id": "stage-approval:" + str(number) + ":" + uuid.uuid4().hex[:8],
                              "state": "executing", "hold": None}
                data["project_transition"] = transition
                data["all_approved_at"] = util.iso()
                tracker.save_milestones(project_name, data)
                _apply_transition(project, transition)
                return "stage " + str(number) + " approved; that was the final stage, so " + project_name + \
                    " goes into main once every package is merged and checked\n"
            transition = _transition(data, "stage-approval:" + str(number), project["pm"], "stage-approved",
                                     closing=final)
            tracker.save_milestones(project_name, data)
            _apply_transition(project, transition)
        if final:
            return "stage " + str(number) + " approved; that was the final stage, so " + project_name + \
                " is closed\n"
        return "stage " + str(number) + " approved\n"

def stage_reject(project_name, note):
    with util.locked():
        state.check_user_only("stage reject")
        project = _project_for(project_name)
        with util.locked():
            data, stage = current_stage(project_name)
            if stage is None or stage.get("state") != "awaiting-approval":
                raise util.refuse("gate: project " + project_name + " has no stage awaiting approval")
            stage["state"] = "rejected"
            stage["rejected_at"] = util.iso()
            stage["rework"] = True
            tracker.save_milestones(project_name, data)
            project["state"] = "awaiting-approval"
            tracker.save_project(project)
        ratatoskr.send("l1", project["pm"], "stage-rejected", note or "", internal=True)
        return "stage " + str(stage["number"]) + " rejected\n"


    # ---------------------------------------------------------------- work packages

def wp_authorise(project_name, package, lane):
    project = _project_for(project_name)
    caller = util.current_node()
    if caller is not None and caller != project["pm"]:
        raise util.refuse("identity: only the PM of " + project_name + " authorises work packages")
    nodes = state.all_nodes()
    lane_node = nodes.get(lane)
    if not lane_node or lane_node.get("project") != project_name or lane_node.get("role") not in ("tl", "su"):
        raise util.usage("unknown lane '" + str(lane) + "' in project " + project_name)
    from . import models
    models.refuse_codex_builder(lane_node, package)  # D1: Codex builds are planned for a later release (A11 seam, AC-99 row 2)
    with util.locked():
        tracker.ensure_writable()
        issue = _package(project_name, package)
        seniors = paired_seniors(lane, nodes)
        criteria = [r for r in issue.records if r.get("kind") == "criteria"
                    and (r.get("node") in seniors or (lane_node["role"] == "su" and r.get("node") == lane))]
        if not criteria:
            raise util.refuse("no criteria: " + package + " has no criteria record from the paired senior user")
        pid_header = {}
        if tracker.read_document(project_name, "pid"):
            pid_header = pid_document(project_name).header
        from .status import delivery_view, wip_limit
        limit = wip_limit(project_name)
        view = delivery_view(project_name, pid_header)
        if isinstance(limit, int) and view.get("approach") == "agile" and view.get("cadence") == "flow":
            from . import workahead
            # A held later-stage package is an early authorisation, not work in progress.
            active = [i for i in tracker.issues(project_name) if i.get("kind") == "work-package"
                      and i.get("state") in WIP_STATES and i.id != package and workahead.wip_counts(project_name, i)]
            if len(active) >= limit:
                raise util.refuse("wip-limit: " + str(len(active)) + " authorised unfinished packages (the WIP limit "
                                  "is " + str(limit) + "; a package in review counts)")
        from . import standard
        if issue.get("state") not in ("planned", "rejected", "authorised"):
            raise util.refuse("gate: " + package + " is " + str(issue.get("state")) + " and cannot be authorised")
        issue.header["lane"] = lane
        issue.add_label(standard.lane_prefix(int(issue.get("standard_version") or 1)) + lane)
        issue.header["state"] = "authorised"
        for su in paired_seniors(lane, nodes):
            tracker.owe(issue.header, su, "authorised:" + package)
        # The lane's dispatch is owed with the authorisation itself; its message is delivered from that
        # stamp (now, or by recovery), once per stamp.
        tracker.owe(issue.header, lane, ratatoskr.DISPATCH + package)
        issue.save()
    ratatoskr.deliver_dispatches(project_name)
    return "work package " + package + " authorised for " + lane + "\n"


def _find_package_any(package, node_id=None):
    """Find a work package in the acting node's own project (never the first match across projects)."""
    util.check_record_id(package)
    project = (state.all_nodes().get(node_id) or {}).get("project") if node_id else None
    if node_id and not project:
        raise util.refuse("writer: " + str(node_id) + " has no project")
    issue = tracker.find_package(package, project)
    if issue is None or issue.get("kind") != "work-package":
        raise util.usage("unknown work package '" + str(package) + "'")
    return issue


def wp_complete(from_node, package):
    nodes = state.all_nodes()
    state.check_identity(from_node, nodes)
    issue = _find_package_any(package, from_node)
    if from_node != issue.get("lane"):
        raise util.refuse("writer: only the lane team lead completes " + package)
    from . import review
    if review.project_reviewer(issue.project, nodes) is not None:
        return review.ready(from_node, package, complete=True)  # it opens a review (AC-58 row 2)
    with util.locked():
        tracker.ensure_writable()
        issue = _find_package_any(package, from_node)
        if issue.get("state") == "review-exhausted":
            raise util.refuse("review-exhausted: " + package)
        issue.header["state"] = "ready-for-test"
        issue.records.append(tracker.record("wp-complete", from_node))
        tracker.owe(issue.header, (tracker.project(issue.project) or {}).get("pm"), "wp-complete:" + package)
        for su in paired_seniors(issue.get("lane"), nodes):
            tracker.owe(issue.header, su, "ready-for-test:" + package)
        issue.save()
    return "work package " + package + " ready for test\n"


def wp_ready(from_node, package):
    from . import review
    return review.ready(from_node, package)


def _withdraw(issue, node_ids, stamps: bool = True) -> None:
    """Nothing stays owed for a package that waits on the user: its owed stamps for these nodes are settled (unless
    `stamps` is False, as for a reopen, which replaces the package's old notices) and their unread messages naming
    only this package are acknowledged (A11 AC-13 row 3, AC-60). A message that also names another package is left
    alone, so that package's work is never lost (A7 I4)."""
    from . import journal, workahead
    targets = {n for n in node_ids if n}
    if stamps:
        issue.header["owed"] = [s for s in issue.header.get("owed") or []
                                if not (isinstance(s, dict) and s.get("node") in targets)]
    pattern = re.compile(r"(?<![\w-])" + re.escape(issue.id) + r"(?![\w-])")
    for node_id in targets:
        items = state.inbox(node_id)
        cleared = []
        for item in items:
            if set(workahead.message_packages(item)) - {issue.id}:
                continue
            if not item.get("acked_at") and pattern.search(str(item.get("body") or "")):
                item["acked_at"] = util.iso()
                cleared.append(item["msg_id"])
        if not cleared:
            continue
        programme = (state.get_node(node_id) or {}).get("programme")
        journal.clear_where(programme, lambda e: e.get("kind") == "inbox" and e.get("node") == node_id
                            and e.get("msg_id") in cleared)
        state.save_inbox(node_id, items)


def wp_reopen(project_name, package):
    """`ygg wp reopen`: PM-only; an accepted, stopped or merged package goes back to `authorised` and to its lane,
    through a new review; a stage awaiting approval that holds it goes back to executing (AC-65, AC-63)."""
    project = _project_for(project_name)
    caller = util.current_node()
    if caller != project["pm"]:
        raise util.refuse("identity: only the PM of " + project_name + " reopens a package")
    util.check_record_id(package)
    from . import review
    try:
        review.apply_cap_answers()  # a cap answered `stop` takes effect before the reopen reads the state
    except util.YggError:
        pass
    with util.locked():
        tracker.ensure_writable()
        issue = _package(project_name, package)
        if issue.get("state") not in ("accepted", "stopped", "merged", "ready-for-test", "rejected", "review-exhausted",
                                      "suspended"):
            raise util.refuse("gate: " + package + " is " + str(issue.get("state")) + " and cannot be reopened")
        issue.header["state"] = "authorised"
        issue.header.pop("reviewed_head", None)
        issue.records.append(tracker.record("reopened", caller))
        issue.save()
        data, stage = current_stage(project_name)
        if stage is not None and stage.get("state") == "awaiting-approval":
            stage["state"] = "executing"
            stage["ended_at"] = None
            stage.setdefault("reopened", []).append(package)
            tracker.save_milestones(project_name, data)
            project = _project_for(project_name)
            project["state"] = "executing"
            tracker.save_project(project)
        elif project.get("state") == "ready-to-merge":
            project = _project_for(project_name)
            project["state"] = "executing"
            tracker.save_project(project)
    from . import stageclose
    stageclose.settle_close_task(project_name)
    _withdraw(issue, [issue.get("lane")], stamps=False)  # one unread work message for the reopened package (AC-05)
    ratatoskr.send(project["pm"], issue.get("lane"), "work", "Work package " + package + " reopened by the PM: "
                   "work on it again, then mark it ready.", internal=True)
    return "work package " + package + " reopened\n"


def review_record(from_node, package, round_number, verdict):
    from . import config
    nodes = state.all_nodes()
    state.check_identity(from_node, nodes)
    state.check_self(from_node, "review")
    if verdict not in ("pass", "changes"):
        raise util.usage("--verdict must be pass or changes")
    node = nodes.get(from_node)
    if not node or node.get("role") in ("worker", "reviewer"):
        raise util.refuse("writer: " + from_node + " cannot write review records")
    issue = _find_package_any(package, from_node)
    from . import review
    review.check_review_record(issue)  # AC-59 row 3
    with util.locked():
        tracker.ensure_writable()
        issue = _find_package_any(package, from_node)
        review.check_review_record(issue)
        issue.records.append(tracker.record("review", from_node, round=int(round_number), verdict=verdict,
                                            caller=state.caller_id()))
        since = max([n for n, r in enumerate(issue.records) if r.get("kind") == "reopened"], default=-1)
        changes = len({r.get("round") for n, r in enumerate(issue.records) if n > since and r.get("kind") == "review"
                       and r.get("verdict") == "changes"})
        layered = config.explain_all(issue.project, issue.id).get("review_rounds")
        limit = int(layered[0] if layered else config.get("review_rounds", 5))
        exhausted = verdict == "changes" and changes >= limit
        if exhausted:
            issue.header["state"] = "review-exhausted"
        issue.save()
        if exhausted:
            project = tracker.project(issue.project)
            identifier = tracker.allocate("issue")
            record = tracker.new_issue(issue.project, identifier, "issue", ["issue"], from_node,
                                       to=project["pm"], package=package, reason="review-exhausted")
            record.save()
    if exhausted:
        ratatoskr.send(from_node, project["pm"], "issue", package + " review-exhausted", internal=True)
    return "review recorded on " + package + "\n"


def acceptance_record(from_node, package, sha, verdict):
    nodes = state.all_nodes()
    state.check_identity(from_node, nodes)
    if verdict not in ("accept", "reject"):
        raise util.usage("--verdict must be accept or reject")
    state.check_self(from_node, "rejection" if verdict == "reject" else "acceptance")
    from . import review
    try:
        review.apply_cap_answers()  # an `accept as is` answer takes effect before the acceptance reads the state
    except util.YggError:
        pass
    issue = _find_package_any(package, from_node)
    if issue.get("state") == "review-exhausted":
        raise util.refuse("review-exhausted: " + package + " cannot be accepted")
    if issue.get("state") != "ready-for-test":
        raise util.refuse(package + " is not ready for test")  # A11.1 AC-03
    lane = issue.get("lane")
    if from_node not in paired_seniors(lane, nodes):
        raise util.refuse("writer: only the paired senior user of " + str(lane) + " records acceptance")
    from . import review
    review.check_acceptance_sha(issue, sha)  # AC-13 row 2
    with util.locked():
        tracker.ensure_writable()
        issue = _find_package_any(package, from_node)
        if issue.get("state") != "ready-for-test":
            raise util.refuse(package + " is not ready for test")
        review.check_acceptance_sha(issue, sha)
        issue.records.append(tracker.record("acceptance", from_node, sha=sha, verdict=verdict,
                                            caller=state.caller_id()))
        issue.header["state"] = "accepted" if verdict == "accept" else "rejected"
        if verdict == "accept":
            tracker.owe(issue.header, (tracker.project(issue.project) or {}).get("pm"), "acceptance:" + package)
        else:
            tracker.owe(issue.header, lane, "rejection:" + package)
        issue.save()
        if verdict == "accept":
            # The package's work is finished: the lane's unread notices about it alone are settled, so nothing stays
            # owed to the lane for an accepted package (A11.1 AC-03 row 2).
            _withdraw(issue, [lane], stamps=False)
    return "acceptance recorded on " + package + "\n"


# ---------------------------------------------------------------- issues, exceptions, blockers

def issue_raise(from_node, reason):
    nodes = state.all_nodes()
    state.check_identity(from_node, nodes)
    node = state.require_node(from_node, nodes)
    if node["role"] == "worker":
        raise util.refuse("writer: workers raise issues through their team lead")
    project = tracker.project(node["project"]) if node.get("project") else None
    if project is None:
        raise util.usage("node " + from_node + " has no project")
    reason = util.redact(reason)
    with util.locked():
        tracker.ensure_writable()
        identifier = tracker.allocate("issue")
        record = tracker.new_issue(project["id"], identifier, "issue", ["issue"], from_node,
                                   to=project["pm"], reason=reason)
        record.save()
    if from_node != project["pm"]:
        ratatoskr.send(from_node, project["pm"], "issue", identifier + ": " + reason, internal=True)
    return "issue " + identifier + " raised\n"


def _raise_exception(project, reason, plan):
    identifier = tracker.allocate("exception")
    record = tracker.new_issue(project["id"], identifier, "exception", ["exception"], project["pm"],
                               to="user", reason=reason, plan=plan, state="open")
    record.save()
    project = tracker.project(project["id"])
    project["state"] = "exception"
    tracker.save_project(project)
    state.alert("ygg-exception", "Exception " + identifier + " on " + project["id"] + ": " + reason,
                project=project["id"], record=identifier)
    return identifier


def exception_raise(from_node, reason, plan):
    nodes = state.all_nodes()
    state.check_identity(from_node, nodes)
    node = state.require_node(from_node, nodes)
    if node["role"] != "pm":
        raise util.refuse("exceptions are raised by the PM")
    project = tracker.project(node["project"])
    with util.locked():
        tracker.ensure_writable()
        identifier = _raise_exception(project, util.redact(reason), util.redact(plan))
    return "exception " + identifier + " raised\n"


def blocker_raise(from_node, reason, forecast=False):
    nodes = state.all_nodes()
    state.check_identity(from_node, nodes)
    node = state.require_node(from_node, nodes)
    if node["role"] in ("worker", "reviewer"):
        raise util.refuse("writer: " + from_node + " raises blockers through its team lead")
    kind = "ygg-blocker-forecast" if forecast else "ygg-blocker"
    reason = util.redact(reason)
    with util.locked():
        if node.get("project"):
            tracker.ensure_writable()
            identifier = tracker.allocate("issue")
            label = "exception-forecast" if forecast else "issue"
            record = tracker.new_issue(node["project"], identifier, label, [label, "blocker"], from_node,
                                       reason=reason)
            project = tracker.project(node["project"])
            if project and project["pm"] != from_node:
                tracker.owe(record.header, project["pm"], "blocker")
            record.save()
        state.alert(kind, ("Blocker forecast: " if forecast else "Blocker: ") + reason, node=from_node,
                    project=node.get("project"))
    return "blocker raised\n"


# ---------------------------------------------------------------- pause, attach

def pause(programme=None):
    """Write the intent marker (creation lock), complete older stop intents locally, then commit `pause` to
    the journal (exit 8 when that fails; reconciliation persists the intent later)."""
    if programme is not None:
        util.check_id(programme, "programme id")
    from . import interlock, safety
    intent = safety.publish_marker("pause", programme)
    interlock.complete_stops()
    try:
        with util.locked(timeout=interlock.JOURNAL_WAIT_SECONDS):
            from . import journal
            safety.ensure_established()
            home = programme if programme and tracker.programme(programme) is not None else None
            journal.add(home, "pause", scope=programme or "*", intent=intent)
            interlock.reconcile(local=False)  # inside the same bounded wait for the journal lock
    except util.LockTimeout:
        raise YggError(util.EXIT_TRACKER, "paused " + (programme or "everything") + "; tracker write pending")
    except YggError as error:
        raise YggError(util.EXIT_TRACKER, "paused " + (programme or "everything") + "; tracker write failed: " +
                       error.message)
    return "paused " + (programme or "everything") + "\n"


def resume(scope=None):
    """Journal-first: reconcile, read the active intents for the scope (markers and journal), commit
    `resume {scope, clears}`, then delete exactly those markers. A marker no committed resume lists is never
    deleted."""
    state.check_user_only("resume")
    nodes = state.all_nodes()
    from . import faults, interlock, safety
    if scope:
        util.check_id(scope, "scope")
    interlock.reconcile()
    wanted = scope or "*"
    with util.locked():
        from . import journal
        # The markers this resume clears are fixed now, under the creation lock; a stop or pause published
        # after this snapshot keeps its marker.
        clears = safety.marker_ids(scope)
        journalled, _, _ = interlock.journal_intents()
        clears += [i["intent"] for i in journalled if i["intent"] and i["intent"] not in clears
                   and (scope is None or i.get("scope") == wanted)]
        journal.add(scope if scope and tracker.programme(scope) is not None else None, "resume",
                    scope=wanted, clears=clears)
        for programme_name in journal.all_programmes():
            # An intent written before ids existed cannot be listed: the resume clears it instead.
            journal.clear_where(programme_name, lambda e: e.get("kind") in ("pause", "stop") and not e.get("intent")
                                and (scope is None or e.get("scope") == wanted))
    faults.boundary("after-resume-commit")  # the resume is committed, its markers not yet deleted
    safety.remove_markers(clears)  # exactly the markers the committed resume lists
    for node in nodes.values():
        if scope and scope not in (node["id"], node.get("programme")):
            continue
        data = state.runtime(node["id"])
        if data.get("state") == "stopped":
            data["state"] = "idle"
            state.save_runtime(node["id"], data)
    return "resumed " + (scope or "everything") + "\n"


def attach(session, takeover=False):
    if takeover:
        state.check_user_only("attach --takeover")
    from . import config
    ttl = float(config.get("l1_lock_ttl_minutes", 30)) * 60
    path = state.root() / "l1.lock"
    with util.locked("l1"):
        current = util.read_json(path)
        moment = util.now()
        if isinstance(current, dict) and current.get("session") != session:
            heartbeat = util.parse_time(current.get("heartbeat") or current.get("at"))
            expired = (moment - heartbeat).total_seconds() >= ttl
            if not expired and not takeover:
                raise YggError(util.EXIT_LOCK, "L1 lock held by session " + current["session"])
            if expired:
                state.alert("ygg-l1-lock-expired", "Replaced expired L1 lock of session " + current["session"],
                            session=current["session"])
        util.write_json(path, {"session": session, "at": util.iso(moment), "heartbeat": util.iso(moment),
                               "pid": os.getpid()})
    return "attached " + session + "\n"


def detach(session):
    path = state.root() / "l1.lock"
    with util.locked("l1"):
        current = util.read_json(path)
        if isinstance(current, dict) and current.get("session") == session:
            path.unlink(missing_ok=True)
    return "detached " + session + "\n"


def project_shape(project_name, collapsed, standard):
    state.check_user_only("project shape")
    if collapsed == standard:
        raise util.usage("give exactly one of --collapsed or --standard")
    project = _project_for(project_name)
    shape = "collapsed" if collapsed else "standard"
    with util.locked():
        tracker.ensure_writable()
        if shape == "standard":
            nodes = state.all_nodes()
            under_pm = [n["id"] for n in nodes.values() if n.get("project") == project_name
                        and n.get("role") == "worker" and n.get("parent") == project["pm"]]
            if under_pm:
                raise util.refuse("shape: " + ", ".join(sorted(under_pm)) + " report to the PM, so project " +
                                  project_name + " stays collapsed")
        text = tracker.read_document(project_name, "pid")
        if text:
            issue = tracker.parse_issue(project_name, "pid", text)
        else:
            issue = tracker.Issue(project_name, "pid", {"schema": "ygg.pid.v1", "project": project_name,
                                                        "programme": project["programme"], "draft": True,
                                                        "kind": "pid", "standard_version": STANDARD_VERSION,
                                                        "actor": "user"})
        issue.records.append(tracker.record("shape", None, actor="user", shape=shape))
        tracker.write_text(tracker.document_path(project_name, "pid"), _render_doc(issue))
        project = _project_for(project_name)
        project["shape"] = shape
        tracker.save_project(project)
    return "project " + project_name + " shape " + shape + "\n"


def hardstop_check(node_id, action, target):
    from .decisions import HARD_STOP_ACTIONS
    if action not in HARD_STOP_ACTIONS:
        raise util.usage("unknown hard-stop action '" + str(action) + "'")
    caller = util.current_node()
    if caller is not None and caller != node_id:
        raise YggError(util.EXIT_REFUSED, "hardstop: " + caller + " may only ask for itself")
    nodes = state.all_nodes()
    node = state.require_node(node_id, nodes)
    state.alert("ygg-blocker", "Hard stop " + action + " on " + util.redact(target) + " by " + node_id +
                " waits for the user", node=node_id, project=node.get("project"), action=action)
    return util.EXIT_GUARD, "DENIED: hard stop " + action + " on " + util.redact(target) + " waits for the user\n"


def _lane_from_branch(branch):
    match = re.fullmatch(r"(?:refs/heads/)?ygg/([a-z0-9][a-z0-9-]{0,62})/lane/([a-z0-9][a-z0-9-]{0,62})", branch)
    return (match[1], match[2]) if match else (None, None)


def event_ci(repo, branch, status):
    project_name, lane = _lane_from_branch(branch)
    if not project_name:
        return "ignored: " + branch + " is not a lane branch\n"
    nodes = state.all_nodes()
    if lane not in nodes or nodes[lane].get("project") != project_name:
        raise util.usage("unknown lane " + str(lane))
    state.wake(lane, "ci:" + status)
    for su in paired_seniors(lane, nodes):
        state.wake(su, "ci:" + status)
    return "ci " + status + " recorded for " + branch + "\n"


def event_boat(node_id, job, status):
    state.require_node(node_id)
    reason = "boat:" + str(job) + ":" + str(status)
    if len(quote(reason, safe="")) > 2400:
        # The journal keeps a reason whole or not at all: an oversized event is refused, never truncated.
        raise util.usage("boat job and status are too long: the encoded event must be at most 2400 characters")
    state.wake(node_id, reason)
    return "boat job " + str(job) + " " + str(status) + "\n"


def exception_decide(identifier, plan):
    with util.locked():
        state.check_user_only("exception decide")
        if not re.fullmatch(r"E-[0-9]{4,9}", identifier or ""):
            raise util.usage("invalid exception id '" + str(identifier) + "'")
        issue = None
        for project in tracker.projects():
            issue = tracker.read_issue(project["id"], identifier)
            if issue is not None:
                break
        if issue is None or issue.get("kind") != "exception":
            raise util.usage("unknown exception '" + identifier + "'")
        if issue.get("state", "open") != "open":
            raise util.refuse("gate: exception " + identifier + " is already closed")
        project = tracker.project(issue.project)
        with util.locked():
            tracker.ensure_writable()
            issue.header.update(state="closed", resolution=plan, resolved_by="user", resolved_at=util.iso())
            issue.records.append(tracker.record("approval", None, actor="user", resolution=plan))
            if plan != "close":
                tracker.owe(issue.header, (tracker.project(issue.project) or {}).get("pm"),
                            "exception-decided:" + identifier)
            issue.save()
            project = tracker.project(issue.project)
            if plan == "approve":
                project["state"] = "executing"
            elif plan == "close":
                project["state"] = "closed"
            tracker.save_project(project)
            if plan == "reject":
                identifier_d = tracker.allocate("decision")
                decision = tracker.new_issue(project["id"], identifier_d, "decision", ["decision"], project["pm"],
                                             question="Exception " + identifier + " plan rejected: what next?",
                                             options=["replan", "close"], recommended="replan", chosen=None,
                                             chosen_by=None, reversible=False, undo=None, aligns=None,
                                             scope={"type": "project", "id": project["id"]}, action=None, target=None,
                                             provenance={"type": "agent", "origin": project["pm"]},
                                             exception=identifier)
                decision.save()
        return "exception " + identifier + " " + plan + "\n"

def project_close(project_name, premature=False, note=None):
    with util.locked():
        state.check_user_only("project close")
        project = _project_for(project_name)
        if premature and not (note or "").strip():
            raise util.usage("--premature needs --note <text>")
        with util.locked():
            tracker.ensure_writable()
            project["state"] = "closed"
            project["closed_at"] = util.iso()
            project["closed_note"] = util.redact(note or "") or None
            project["closed_premature"] = bool(premature)
            tracker.save_project(project)
            # Nodes of a closed project owe nothing: owed work is derived, and it skips closed projects.
    state.alert("ygg-project-closed", project_name + " is closed", project=project_name)  # A11 AC-06 row 3
    from . import t3
    t3.close_project(project["id"])  # A5.1: the T3 projects of its worktrees, then the worktrees, go (unlocked)
    return "project " + project_name + " closed\n"

