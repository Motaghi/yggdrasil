"""Registry of nodes, runtime node state, wakes, inboxes and alerts."""

from __future__ import annotations

from pathlib import Path

from . import tracker, util


# ---------------------------------------------------------------- registry

def all_nodes() -> dict[str, dict]:
    nodes: dict[str, dict] = {}
    programmes = {p["id"]: p for p in tracker.programmes()}

    def register(node_id: str, source) -> None:
        # Ids are unique across every file, never reserved, and never a programme's id.
        if node_id in nodes:
            raise tracker.corrupt(source, "node id " + node_id + " is registered twice")
        if node_id in util.RESERVED_IDS:
            raise tracker.corrupt(source, "node id " + node_id + " is reserved")
        if node_id in programmes:
            raise tracker.corrupt(source, "node id " + node_id + " collides with a programme id")

    for project in tracker.projects():
        programme = programmes.get(project.get("programme"), {})
        register(project["pm"], tracker.project_dir(project["id"]) / "project.json")
        nodes[project["pm"]] = {
            "id": project["pm"], "role": "pm", "parent": "l1", "programme": project.get("programme"),
            "project": project["id"], "pairs_with": [], "domain": programme.get("domain"),
        }
        for entry in tracker.node_entries(tracker.nodes_file(project["id"])):
            register(entry["id"], tracker.nodes_file(project["id"]))
            nodes[entry["id"]] = {
                "id": entry["id"], "role": entry.get("role"), "parent": entry.get("parent"),
                "programme": project.get("programme"), "project": project["id"],
                "pairs_with": list(entry.get("pairs_with") or []), "domain": programme.get("domain"),
            }
    for programme in programmes.values():
        for entry in tracker.node_entries(tracker.nodes_file(programme_name=programme["id"]),
                                          roles=tracker.PROGRAMME_ROLES, default_role="reviewer"):
            register(entry["id"], tracker.nodes_file(programme_name=programme["id"]))
            if entry.get("pairs_with"):
                raise tracker.corrupt(tracker.nodes_file(programme_name=programme["id"]),
                                      "reviewer " + entry["id"] + " has pairs_with; reviewers pair with no node")
            nodes[entry["id"]] = {
                "id": entry["id"], "role": entry.get("role", "reviewer"), "parent": programme["id"],
                "programme": programme["id"], "project": None, "pairs_with": [],
                "domain": programme.get("domain"),
            }
    _check_topology(nodes)
    return nodes


# A project node's parent, by role (a pm-parented worker also needs the user's collapsed-shape record).
PARENT_ROLES = {"tl": ("pm",), "su": ("pm",), "worker": ("tl", "su", "pm"), "reviewer": ("pm",)}


def _check_topology(nodes: dict) -> None:
    """Every project node hangs beneath a node of its own project with an allowed role, and pairs only with
    team leads of that project, so no record can place a node under another project's PM."""
    collapsed: dict = {}
    for node in nodes.values():
        if node["project"] is None:
            continue  # reviewers: programme file, role checked when read, parented to the programme
        source = tracker.nodes_file(node["project"])
        if node["role"] == "reviewer" and node["pairs_with"]:
            # A project reviewer checks finished work for the whole project: it pairs with no node.
            raise tracker.corrupt(source, "reviewer " + node["id"] + " has pairs_with; reviewers pair with no node")
        for paired in node["pairs_with"]:
            target = nodes.get(paired)
            if (target is None or paired == node["id"] or target["role"] != "tl"
                    or target["project"] != node["project"]):
                raise tracker.corrupt(source, "node " + node["id"] + " pairs with " + paired +
                                      ", which is not a team lead of project " + node["project"])
        if node["role"] == "pm":
            continue  # built from the project record, parented to L1
        if node["role"] not in PARENT_ROLES:
            raise tracker.corrupt(source, "node " + node["id"] + " has role " + str(node["role"]) +
                                  ", which a project's nodes file cannot hold")
        parent = nodes.get(node["parent"]) if node["parent"] else None
        if (parent is None or parent["id"] == node["id"] or parent["project"] != node["project"]
                or parent["role"] not in PARENT_ROLES[node["role"]]):
            raise tracker.corrupt(source, "node " + node["id"] + " has parent " + str(node["parent"]) +
                                  ", which is not a " + " or ".join(PARENT_ROLES[node["role"]]) +
                                  " of project " + node["project"])
        if node["role"] == "worker" and parent["role"] == "pm":
            if node["project"] not in collapsed:
                from . import commands
                collapsed[node["project"]] = commands.project_collapsed(node["project"])
            if not collapsed[node["project"]]:
                raise tracker.corrupt(source, "node " + node["id"] + " is a worker under PM " + parent["id"] +
                                      ", but project " + node["project"] + " has no user-written collapsed shape")


def get_node(node_id: str, nodes: dict | None = None) -> dict | None:
    if node_id == "l1":
        return {"id": "l1", "role": "l1", "parent": None, "programme": None, "project": None,
                "pairs_with": [], "domain": None}
    return (nodes if nodes is not None else all_nodes()).get(node_id)


def require_node(node_id: str, nodes: dict | None = None) -> dict:
    found = get_node(node_id, nodes)
    if not found:
        raise util.usage("unknown node '" + str(node_id) + "'")
    return found


def children(node_id: str, nodes: dict) -> list[str]:
    return [n["id"] for n in nodes.values() if n.get("parent") == node_id]


def check_identity(claimed: str, nodes: dict | None = None) -> None:
    """With YGG_NODE set, --from/--as must be that node or one of its children."""
    caller = util.current_node()
    if caller is None:
        return
    if claimed == caller:
        return
    nodes = nodes if nodes is not None else all_nodes()
    found = nodes.get(claimed)
    if found and found.get("parent") == caller:
        return
    raise util.refuse("identity: " + caller + " may not act as " + str(claimed))


def check_self(claimed: str, record: str, verb: str = "recorded") -> None:
    """Judgements (criteria, acceptance, rejection, review) are written only by the acting node itself or by
    the user, never by its parent."""
    caller = util.current_node()
    if caller is not None and caller != claimed:
        raise util.YggError(util.EXIT_REFUSED, record + " for " + str(claimed) + " must be " + verb + " by " +
                            str(claimed) + " itself")


def caller_id() -> str:
    """Who actually ran the command: the managed node, or the user."""
    return util.current_node() or "user"


# Verbs that start, supervise or drive sessions, rebuild state, feed events or take the cockpit lock. A managed
# session may run none of them; the value says whether the subcommand belongs to the verb's name.
USER_VERBS = {"launch": False, "_supervise": False, "conductor": True, "heimdall": True, "rehydrate": False,
              "event": True, "attach": False, "detach": False, "t3": True, "agents": True,
              "demo": False, "codex": False}


MANAGED_MARKER = ".ygg-managed"


def inside_managed_worktree(start=None) -> bool:
    """Whether the working folder is inside a managed worktree (its `.ygg-managed` marker, A5.1)."""
    try:
        here = Path(start or Path.cwd()).resolve()
    except OSError:
        return False
    return any((folder / MANAGED_MARKER).exists() for folder in (here, *here.parents))


def managed_session() -> bool:
    """A session counts as managed when it carries either marker, so dropping YGG_NODE alone is not enough;
    and inside a managed worktree even with both dropped (A5.1: a session that lost its environment fails
    closed instead of acting as the user)."""
    return util.current_node() is not None or util.env("YGG_ACTIVATION") is not None or inside_managed_worktree()


def visible_programme(nodes: dict | None = None):
    """For reads: None for the user (everything visible), else the managed caller's programme, or "" when the
    caller cannot be resolved (nothing visible)."""
    if not managed_session():
        return None
    caller = util.current_node()
    if caller is None:
        return ""
    known = (nodes if nodes is not None else all_nodes()).get(caller) or {}
    return known.get("programme") or ""


def check_user_verb(command: str, action: str | None) -> None:
    if command not in USER_VERBS or not managed_session():
        return
    verb = command + (" " + action if USER_VERBS[command] and action else "")
    raise util.refuse("user-only: '" + verb + "' is a user verb and cannot run in a managed session")


def check_user_only(verb: str) -> None:
    if managed_session():
        raise util.refuse("user-only: '" + verb + "' is a user verb and cannot run in a managed session")


# ---------------------------------------------------------------- runtime

def root() -> Path:
    return util.state_root()


def runtime_path(node_id: str) -> Path:
    return root() / "nodes" / (node_id + ".json")


def runtime(node_id: str) -> dict:
    data = util.read_json(runtime_path(node_id), {}) or {}
    data.setdefault("state", "idle")
    return data


def save_runtime(node_id: str, data: dict) -> None:
    util.write_json(runtime_path(node_id), data)


def set_state(node_id: str, state: str, **fields) -> dict:
    with util.locked():
        data = runtime(node_id)
        data["state"] = state
        data.update(fields)
        save_runtime(node_id, data)
        return data


# ---------------------------------------------------------------- wakes

def wakes() -> dict:
    """Owed work per node, derived from journal facts (level-triggered): node -> {"since", "reasons",
    "queued": [{"reason", "id", "kind"}]}. Nothing is stored; acknowledging, consuming or settling the
    underlying fact is what removes an entry."""
    from . import reducer
    found = {}
    for node_id, items in reducer.owed_all().items():
        found[node_id] = {"since": str(items[0].get("at") or ""), "reasons": [i["reason"] for i in items],
                          "queued": [{"reason": i["reason"], "id": i["id"], "kind": i["kind"]} for i in items]}
    return found


def encode_reason(reason: str) -> str:
    """A wake reason as stored in the journal: percent-encoded, so any job id or status a command accepts
    (slashes, spaces, parentheses) survives the journal's strict entry format."""
    from urllib.parse import quote
    # Nothing that fits the journal's 2400-character limit is cut (every reason that round-tripped before
    # still does); beyond it, whole characters are dropped from the end, never part of an escape.
    text = str(reason)
    encoded = quote(text, safe="")
    while len(encoded) > 2400 and text:
        text = text[:-1]
        encoded = quote(text, safe="")
    return encoded


def decode_reason(stored: str) -> str:
    from urllib.parse import unquote
    return unquote(str(stored))


def wake(node_id: str, reason: str) -> None:
    """Record that `node_id` is owed a launch for `reason`: one journal `event` fact, its own id. The fact
    is the obligation; it is settled when an activation given it was spawned and did not end aborted."""
    import uuid
    node = all_nodes().get(node_id)
    if node is None:
        return
    from . import journal
    journal.add(node.get("programme"), "event", event_id="e-" + uuid.uuid4().hex[:12], node=node_id,
                reason=encode_reason(reason))


# ---------------------------------------------------------------- inboxes

def inbox_path(node_id: str) -> Path:
    return root() / "inboxes" / (node_id + ".json")


def inbox(node_id: str) -> list[dict]:
    return list(util.read_json(inbox_path(node_id), []) or [])


def save_inbox(node_id: str, items: list[dict]) -> None:
    util.write_json(inbox_path(node_id), items)


def all_inbox_pending() -> list[dict]:
    base = root() / "inboxes"
    pending = []
    if base.is_dir():
        for path in sorted(base.glob("*.json")):
            for item in util.read_json(path, []) or []:
                if not item.get("acked_at"):
                    pending.append({"node": path.stem, "msg_id": item["msg_id"]})
    return pending


# ---------------------------------------------------------------- alerts

def alert(kind: str, message: str, **fields) -> dict:
    """Append one alert with its id, class and next step from the alert-kind table (A11, `alertkinds`). Lane 4
    adds delivery for class-`user` alerts; this function stays the one writer of `alerts.jsonl`."""
    import uuid
    from . import alertkinds
    entry = {"ts": util.iso(), "id": "al-" + uuid.uuid4().hex[:12], "kind": kind, "message": message,
             "class": alertkinds.classify(kind, fields), "next_step": alertkinds.next_step(kind)}
    entry.update(fields)
    util.append_line(root() / "alerts.jsonl", entry)
    return entry


def alerts() -> list[dict]:
    return util.read_lines(root() / "alerts.jsonl")


# ---------------------------------------------------------------- pause

def platform_paused() -> bool:
    if util.env("CLAUDE_PLATFORM_ENABLED") == "0":
        return True
    directory = util.env("YGG_PLATFORM_STATE") if util.overrides_allowed() else None
    if directory and (Path(directory) / "pause").exists():
        return True
    from . import config
    configured = config.get("platform_pause_dir")  # a profile pointing at the platform's state folder
    return bool(configured) and (Path(str(configured)).expanduser() / "pause").exists()


def paused(programme: str | None) -> bool:
    """Paused: the platform pause, or an active pause intent covering the programme (a marker in the safety
    folder, or a journalled pause no committed resume lists)."""
    from . import interlock
    return interlock.paused(programme)
