"""Linear tracker adapter: the files-tracker layout kept as a local cache, written through to Linear.

Every tracker write goes to Linear first and to the local cache (`$YGG_STATE_ROOT/linear-cache`) second, so
a failed Linear call leaves nothing written locally and the command can be run again. Every tracker read is
served by the cache. The cache is valid until the tracker revision changes (ruling 109): the revision is the
`updatedAt` of the documents whose concurrent change would lose data, the programme journals and the id
counters. `refresh()` checks it with one request and re-reads only what changed; a write to one of those
documents first checks that Linear still holds the version the cache holds, so a write can never replace a
change it has not seen.

The Linear journal is the authority (ruling 86), but a changed guarded document is adopted only when it
extends what the cache holds: journal entries may be appended or marked cleared, never altered or removed,
and counters may only rise. Anything else is not adopted, the cache stays as it is, one
`ygg-tracker-rewrite` alert is raised, and writes to that document are refused until it is resolved.
Signing records (a MAC per entry) belongs with the credential broker, planned for a later release.

Mapping (tracker standard v1, "PRINCE2 to tracker mapping"):

| cache path | Linear object |
|---|---|
| `initiatives/<p>/initiative.json` | initiative `<p>`, plus its document `initiative.json` |
| `initiatives/<p>/documents/<slug>.md` | initiative document `<slug>` (journal, North Star, autonomy) |
| `initiatives/<p>/<other>` | initiative document titled with the relative path |
| `projects/<p>/project.json` | project `<p>` in the configured team, linked to its initiative, plus document `project.json` |
| `projects/<p>/issues/<id>.md` | issue titled `<id> <title>` in that project (a worker task: its task text, as a sub-issue of its package); the file is its description; its labels plus `std:v<n>`; milestone `Stage <n>`; each new record or comment also posted as a Linear comment |
| `projects/<p>/milestones.json` | project document `milestones.json`, plus a milestone `Stage <n>` per stage |
| `projects/<p>/updates/<stamp>-highlight.md` | project update with the health from the frontmatter, plus the document |
| `projects/<p>/documents/<slug>.md` | project document `<slug>` (PID, plans, reports, rehydration documents) |
| `projects/<p>/<other>` | project document titled with the relative path |
| `_ygg/<file>` | document on the team-private control project `_ygg-control` (counters, the root journal) |

`pull()` rebuilds the cache from Linear alone (`ygg rehydrate`): the control project's documents, then every
initiative and project the app created, with their documents and issues (ruling 49). A document is adopted
when the app actor (or a configured `linear_authority_actor_ids` user) created it and updated it last.
Linear's Issue, Initiative and Project types name no last updater, so those are adopted only as the app
recorded them in its registry document (`_ygg/linear-registry.json`, a document on the control project):
the registered id under its name, one per name, and for an issue the content the app last wrote. Anything
else is skipped with one `ygg-linear-foreign` alert, and the write path only ever updates registered issues.

Design point, not built: edits made in Linear's own interface. The tick only checks the guarded documents, so
an issue or document changed in Linear by a person is not seen until a rehydrate, where it is refused unless
an authority actor made it. Seeing such edits on every tick, for example authority comments that should wake
a PM, needs a cheap changed-since query; the published API filters by `updatedAt`, so it costs one more
request per tick. Webhooks are planned for a later release (ruling 109).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import config, state, util
from ..util import YggError
from .client import LinearError

CONTROL = "_ygg-control"
FENCE = "`````"
NOTE = "Yggdrasil record: written by ygg, read back exactly. Edit through ygg, not by hand."
OPEN = re.compile(r"^(`{3,})text[ \t]*$", re.M)
# Documents whose lost update would lose data: each write checks Linear still holds the cached version.
GUARDED = re.compile(r"^(initiatives/[^/]+/documents/journal\.md|_ygg/journal\.md|_ygg/counters\.json)$")


def cache_root() -> Path:
    return state.root() / "linear-cache"


# ---------------------------------------------------------------- transport

_CLIENT = None


def client():
    global _CLIENT
    if _CLIENT is None:
        from . import auth
        _CLIENT = auth.client()
    return _CLIENT


def _succeeded(data: dict, field: str, entity: str | None = None, version: bool = True) -> dict:
    """A mutation answered with HTTP 200 counts only with `success: true` and, when it names one, the object
    it returns (with its id, and its version where the caller relies on it); anything else is a failed
    write (exit 8), never confirmed. Every mutation goes through here."""
    payload = (data or {}).get(field) or {}
    if payload.get("success") is not True:
        raise YggError(util.EXIT_TRACKER, "linear: " + field + " was not applied")
    if entity is not None:
        item = payload.get(entity) or {}
        if not item.get("id") or (version and not item.get("updatedAt")):
            raise YggError(util.EXIT_TRACKER, "linear: " + field + " was not applied")
    return data


def gql(operation: str, query: str, variables: dict | None = None) -> dict:
    try:
        return client().execute(operation, query, variables)
    except LinearError as error:
        if error.kind in ("auth", "forbidden"):
            from .auth import _fail
            raise _fail(error.kind + " in " + operation + ": " + error.message)
        raise


def pages(operation: str, query: str, variables: dict, path: list) -> list:
    try:
        return client().pages(operation, query, variables, path)
    except LinearError as error:
        if error.kind in ("auth", "forbidden"):
            from .auth import _fail
            raise _fail(error.kind + " in " + operation + ": " + error.message)
        raise


# ---------------------------------------------------------------- id cache

def _ids_path() -> Path:
    return state.root() / "linear-ids.json"


def _ids() -> dict:
    return util.read_json(_ids_path(), {}) or {}


def _remember(**values) -> None:
    with util.locked("linear-ids"):
        data = _ids()
        data.update(values)
        util.write_json(_ids_path(), data)


def _known(key: str) -> str | None:
    return _ids().get(key)


# ---------------------------------------------------------------- encoding (kept from the earlier store)

def encode(text: str) -> str:
    """Wrap a cache file in a five-backtick block so Linear's markdown handling keeps it byte-exact."""
    return NOTE + "\n\n" + FENCE + "text\n" + text.replace(FENCE, "\\`````") + "\n" + FENCE + "\n"


def decode(content: str | None) -> str | None:
    """Read back a block written by `encode`. Linear re-serialises the fence with three backticks and trims
    the block's trailing blank line, so the fence length is not trusted and the text is returned with
    exactly one trailing newline."""
    if not content or not content.startswith(NOTE):
        return None
    match = OPEN.search(content)
    end = content.rstrip().rfind("\n```")
    if not match or end <= match.end():
        return None
    text = content[match.end() + 1:end].replace("\\`````", FENCE)
    return text.rstrip("\n") + "\n"


# ---------------------------------------------------------------- the registry (what the app created)

# Linear's Issue, Initiative and Project types carry no `updatedBy`, so the app keeps its own record of
# every initiative, project and issue it created, and of the content it last wrote to each issue, in a
# document on the control project (a Document does name its last updater). Only registered objects are
# looked up on the write path and adopted by `pull`.
REGISTRY = "_ygg/linear-registry.json"


def _registry() -> dict:
    """The registry as cached. A missing file is an empty registry (first use); a malformed one is an error
    (exit 8), never read as empty, so it can never be replaced by an empty one."""
    path = cache_root() / REGISTRY
    if not path.exists():
        return {"initiatives": {}, "projects": {}, "issues": {}}  # first use: nothing registered yet
    text = util.read_text(path)
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    sections = ("initiatives", "projects", "issues")

    def valid_issue(entry) -> bool:
        return isinstance(entry, dict) and isinstance(entry.get("linear"), str) and entry["linear"] and \
            (isinstance(entry.get("sha256"), str) or isinstance(entry.get("pending"), str)) and \
            all(isinstance(entry.get(k), (str, type(None))) for k in ("sha256", "pending", "pending_from", "updated_at"))
    if not isinstance(data, dict) or not all(isinstance(data.get(key, {}), dict) for key in sections) or \
            not all(isinstance(v, str) and v for key in ("initiatives", "projects")
                    for v in (data.get(key) or {}).values()) or \
            not all(valid_issue(v) for v in (data.get("issues") or {}).values()):
        # An existing file that is empty, blank or malformed is never read as empty: one alert per version.
        version = _sha(text)
        if _known("registry-refused") != version:
            state.alert("ygg-tracker-rewrite", "The Linear registry " + REGISTRY + " is malformed; tracker writes "
                        "are held until it is restored from Linear (ygg rehydrate)", path=REGISTRY)
            _remember(**{"registry-refused": version})
        raise YggError(util.EXIT_TRACKER, "linear: the registry " + REGISTRY + " is malformed; run ygg rehydrate")
    return {key: dict(data.get(key) or {}) for key in sections}


def _register(section: str, key: str, value) -> None:
    with util.locked("linear-registry"):
        data = _registry()
        if value is None:
            if key not in data[section]:
                return
            data[section].pop(key)
        elif data[section].get(key) == value:
            return
        if value is not None:
            data[section][key] = value
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        _put_document("project", project_id(CONTROL), REGISTRY, REGISTRY, text)
        util.atomic_write(cache_root() / REGISTRY, text)


def _sha(text: str) -> str:
    """The hash of a record as Linear gives it back: `decode` returns exactly one trailing newline."""
    import hashlib
    return hashlib.sha256((text.rstrip("\n") + "\n").encode()).hexdigest()


def _foreign_alert(what: str, name: str, detail: str) -> YggError:
    state.alert("ygg-linear-foreign", "Linear " + what + " '" + name + "' " + detail + "; not adopted",
                object=what, name=name)
    return YggError(util.EXIT_TRACKER, "linear: " + what + " '" + name + "' " + detail)


# ---------------------------------------------------------------- containers

def team_id() -> str:
    key = str(config.get("linear_team_key") or "YGG")
    cached = _known("team:" + key)
    if cached:
        return cached
    data = gql("YggTeam", "query YggTeam($key: String!) { teams(filter: {key: {eq: $key}}) { nodes { id key } } }",
               {"key": key})
    nodes = (data.get("teams") or {}).get("nodes") or []
    if not nodes:
        raise YggError(util.EXIT_TRACKER, "linear: team " + key + " is not visible to the app")
    _remember(**{"team:" + key: nodes[0]["id"]})
    return nodes[0]["id"]


def _viewer() -> str:
    cached = _known("viewer")
    if cached:
        return cached
    data = gql("YggViewer", "query YggViewer { viewer { id } }")
    identifier = (data.get("viewer") or {}).get("id")
    if not identifier:
        raise YggError(util.EXIT_TRACKER, "linear: the app actor could not be read")
    _remember(viewer=identifier)
    return identifier


def initiative_id(name: str) -> str:
    cached = _known("initiative:" + name) or _registry()["initiatives"].get(name)
    if cached:
        _remember(**{"initiative:" + name: cached})
        _register("initiatives", name, cached)  # repairs a registration an interrupted create never wrote
        return cached
    data = gql("YggInitiativeFind", "query YggInitiativeFind($name: String!) { initiatives(filter: "
               "{name: {eq: $name}}) { nodes { id name creator { id } } } }", {"name": name})
    nodes = (data.get("initiatives") or {}).get("nodes") or []
    if len(nodes) > 1:
        raise _foreign_alert("initiative", name, "has " + str(len(nodes)) + " initiatives with that name")
    if nodes and (nodes[0].get("creator") or {}).get("id") != _viewer():
        raise _foreign_alert("initiative", name, "exists and was not created by the app")
    if nodes:
        identifier = nodes[0]["id"]  # created by the app, its registration lost (a crash in between)
    else:
        made = gql("YggInitiativeCreate", "mutation YggInitiativeCreate($input: InitiativeCreateInput!) { "
                   "initiativeCreate(input: $input) { success initiative { id name } } }",
                   {"input": {"name": name, "description": "Yggdrasil programme " + name}})
        _succeeded(made, "initiativeCreate", "initiative", version=False)
        identifier = made["initiativeCreate"]["initiative"]["id"]
    _register("initiatives", name, identifier)
    _remember(**{"initiative:" + name: identifier})
    return identifier


def project_id(name: str, programme: str | None = None) -> str:
    identifier = _known("project:" + name) or (_registry()["projects"].get(name) if name != CONTROL else None)
    if identifier is not None and name != CONTROL:
        _register("projects", name, identifier)  # repairs a registration an interrupted create never wrote
    if identifier is None:
        data = gql("YggProjectFind", "query YggProjectFind($team: String!, $name: String!) { team(id: $team) { "
                   "projects(filter: {name: {eq: $name}}) { nodes { id name creator { id } } } } }",
                   {"team": team_id(), "name": name})
        nodes = ((data.get("team") or {}).get("projects") or {}).get("nodes") or []
        if len(nodes) > 1:
            raise _foreign_alert("project", name, "has " + str(len(nodes)) + " projects with that name")
        if nodes and (nodes[0].get("creator") or {}).get("id") != _viewer():
            raise _foreign_alert("project", name, "exists and was not created by the app")
        if nodes:
            identifier = nodes[0]["id"]
        else:
            made = gql("YggProjectCreate", "mutation YggProjectCreate($input: ProjectCreateInput!) { "
                       "projectCreate(input: $input) { success project { id name } } }",
                       {"input": {"name": name, "teamIds": [team_id()], "description": "Yggdrasil project " + name}})
            _succeeded(made, "projectCreate", "project", version=False)
            identifier = made["projectCreate"]["project"]["id"]
        if name != CONTROL:
            _register("projects", name, identifier)
        _remember(**{"project:" + name: identifier})
    if programme and not _known("link:" + name):
        _succeeded(gql("YggLinkProject", "mutation YggLinkProject($input: InitiativeToProjectCreateInput!) { "
                       "initiativeToProjectCreate(input: $input) { success } }",
                       {"input": {"initiativeId": initiative_id(programme), "projectId": identifier}}),
                   "initiativeToProjectCreate")
        _remember(**{"link:" + name: programme})
    return identifier


# ---------------------------------------------------------------- documents

DOCUMENT_FIELDS = "id title updatedAt"


def _find_document(container: str, container_id: str, title: str) -> str | None:
    """The id of the document Yggdrasil keeps under `title`: only a document the app (or an authority actor)
    created is selected or remembered. A title held only by someone else's document refuses the write."""
    field = "project" if container == "project" else "initiative"
    actors = _actors()
    found, foreign = None, False
    for node in pages("YggDocumentTitles", "query YggDocumentTitles($id: String!, $after: String) { " + field +
                      "(id: $id) { documents(first: 50, after: $after) { nodes { id title creator { id } } "
                      "pageInfo { hasNextPage endCursor } } } }", {"id": container_id}, [field, "documents"]):
        if (node.get("creator") or {}).get("id") not in actors:
            foreign = foreign or node.get("title") == title
            continue
        _remember(**{"document:" + container_id + ":" + node["title"]: node["id"]})
        if node["title"] == title:
            found = node["id"]
    if found is None and foreign:
        detail = "was created by another Linear user"
        if _known("foreign-title:" + container_id + ":" + title):
            raise YggError(util.EXIT_TRACKER, "linear: document '" + title + "' " + detail)
        error = _foreign_alert("document", title, detail)  # alerted first, then marked as alerted
        _remember(**{"foreign-title:" + container_id + ":" + title: "1"})
        raise error
    return found


def _read_document(identifier: str) -> dict:
    data = gql("YggDocumentRead", "query YggDocumentRead($id: String!) { document(id: $id) { id updatedAt content "
               "creator { id } updatedBy { id } } }", {"id": identifier})
    return data.get("document") or {}


def rewrite_problem(relative: str, cached: str, remote: str | None) -> str | None:
    """Why a changed guarded document must not replace the cached one, or None when it only extends it."""
    if remote is None:
        return "its content is not a Yggdrasil record"
    if not cached.strip():
        return None  # nothing cached yet (first use or a wiped state root): adopt what Linear holds
    if relative.endswith("counters.json"):
        try:
            old, new = json.loads(cached), json.loads(remote)
        except ValueError:
            return "the counters do not parse"
        if not isinstance(old, dict) or not isinstance(new, dict):
            return "the counters are not a mapping"
        for kind, value in old.items():
            if not isinstance(new.get(kind), int) or isinstance(new.get(kind), bool) or \
                    (isinstance(value, int) and new[kind] < value):
                return "counter " + str(kind) + " went down or disappeared"
        return None
    from .. import journal
    header_old, _ = util.split_frontmatter(util.normalise_newlines(cached))
    header_new, _ = util.split_frontmatter(util.normalise_newlines(remote))
    old_revision = (header_old or {}).get("revision")
    new_revision = (header_new or {}).get("revision")
    if isinstance(old_revision, int) and not (isinstance(new_revision, int) and new_revision >= old_revision):
        return "the journal revision went back"
    old_entries = journal._parse_blocks(util.normalise_newlines(cached))
    new_entries = journal._parse_blocks(util.normalise_newlines(remote))
    if len(new_entries) < len(old_entries):
        return "journal entries were removed"
    for position, (before, after) in enumerate(zip(old_entries, new_entries)):
        if after == before:
            continue
        cleared = not before.get("cleared_at") and after.get("cleared_at") and \
            {k: v for k, v in after.items() if k != "cleared_at"} == before
        if not cleared:
            return "journal entry " + str(position + 1) + " was rewritten"
    return None


def _adopt(identifier: str, relative: str, remote: dict) -> bool:
    """Adopt a changed guarded document into the cache when it only extends it; otherwise keep the cache,
    raise one `ygg-tracker-rewrite` alert for that version and return False."""
    if not _trusted(remote, _actors()):
        # A version created or last changed by someone other than the app or an authority actor is never
        # adopted, whatever it says: the cache is kept and writes to it are held (one alert per version).
        if _known("rejected:" + identifier) != remote.get("updatedAt"):
            _foreign_alert("document", relative, "was changed by another Linear user; writes to it are held")
            _remember(**{"rejected:" + identifier: remote.get("updatedAt")})
        return False
    text = decode(remote.get("content"))
    problem = rewrite_problem(relative, util.read_text(cache_root() / relative), text)
    if problem:
        if _known("rejected:" + identifier) != remote.get("updatedAt"):
            state.alert("ygg-tracker-rewrite", "Linear document " + relative + " changed and was not adopted: " +
                        problem + "; writes to it are held until it is resolved", path=relative)
            _remember(**{"rejected:" + identifier: remote.get("updatedAt")})
        return False
    util.atomic_write(cache_root() / relative, text)
    _remember(**{"updated:" + identifier: remote.get("updatedAt"), "path:" + identifier: relative})
    return True


class Changed(YggError):
    """A guarded write refused because Linear held a newer version that extends the cache: it was adopted, and
    the write, computed from the older cache, is to be computed again from the adopted one."""


def _put_document(container: str, container_id: str, relative: str, title: str, text: str) -> dict:
    key = "document:" + container_id + ":" + title
    identifier = _known(key) or _find_document(container, container_id, title)
    guarded = bool(GUARDED.match(relative))
    remote = _read_document(identifier) if identifier else {}
    if identifier and not _trusted(remote, _actors()):
        # Never written over: a document someone else created or last changed (also one cached by id). The
        # alert comes before the mark that suppresses its repeats, so a stop in between never loses it.
        if _known("rejected:" + identifier) != remote.get("updatedAt"):
            error = _foreign_alert("document", relative, "is not Yggdrasil's or was changed by another Linear user")
            _remember(**{"rejected:" + identifier: remote.get("updatedAt")})
            raise error
        raise YggError(util.EXIT_TRACKER, "linear: " + relative + " was changed by another Linear user; writes to "
                       "it are held (see the ygg-linear-foreign alert)")
    if identifier and guarded:
        # Linear must still hold the version this write was computed from; otherwise its change is pulled
        # into the cache and the command is refused, so its re-run starts from what Linear holds.
        moved = remote.get("updatedAt") != _known("updated:" + identifier)
        held = decode(remote.get("content"))
        if moved and isinstance(remote.get("updatedAt"), str) and remote["updatedAt"] and held is not None and \
                _sha(held) == _sha(util.read_text(cache_root() / relative)):
            # Linear holds exactly the content this write was computed from, under a version this process never
            # recorded: Linear moves a document's updatedAt after the update that set the content has returned
            # (pilot 2, 20 Nov). No change goes unseen, so the version is recorded and the write goes on.
            _remember(**{"updated:" + identifier: remote["updatedAt"]})
        elif moved:
            if _adopt(identifier, relative, remote):
                raise Changed(util.EXIT_TRACKER, "linear: " + relative + " changed in Linear since it was "
                              "read; run the command again")
            raise YggError(util.EXIT_TRACKER, "linear: " + relative + " was rewritten in Linear and not "
                           "adopted; writes to it are held (see the ygg-tracker-rewrite alert)")
    if identifier:
        made = _succeeded(gql("YggDocumentUpdate", "mutation YggDocumentUpdate($id: String!, $input: "
                              "DocumentUpdateInput!) { documentUpdate(id: $id, input: $input) { success document { " +
                              DOCUMENT_FIELDS + " } } }", {"id": identifier, "input": {"content": encode(text)}}),
                          "documentUpdate", "document")
        document = made["documentUpdate"]["document"]
    else:
        field = "projectId" if container == "project" else "initiativeId"
        made = _succeeded(gql("YggDocumentCreate", "mutation YggDocumentCreate($input: DocumentCreateInput!) { "
                              "documentCreate(input: $input) { success document { " + DOCUMENT_FIELDS + " } } }",
                              {"input": {field: container_id, "title": title, "content": encode(text)}}),
                          "documentCreate", "document")
        document = made["documentCreate"]["document"]
    if not guarded:
        _remember(**{key: document["id"]})
        return {}
    # The document is registered for refresh together with its id, at once, so a crash before the cache
    # write still leaves the next refresh to find it. Only the synced revision waits until the cache file
    # holds this content (`synced`): until then the revision is unknown and refresh adopts Linear's version.
    _remember(**{key: document["id"], "path:" + document["id"]: relative, "guarded:" + document["id"]: relative})
    return {"updated:" + document["id"]: document.get("updatedAt")}


def synced(values: dict) -> None:
    """Called by the tracker once the cache file is written: record the revision the cache now holds, then
    run the write's follow-ups (an issue's registry entry and comments)."""
    values = dict(values or {})
    follow_up = values.pop("follow_up", None)
    if values:
        _remember(**values)
    if follow_up is not None:
        follow_up()


# ---------------------------------------------------------------- labels and milestones

def label_id(name: str) -> str:
    cached = _known("label:" + name)
    if cached:
        return cached
    labels = pages("YggLabels", "query YggLabels($team: ID!, $after: String) { issueLabels(first: 100, "
                   "after: $after, filter: {team: {id: {eq: $team}}}) { nodes { id name } "
                   "pageInfo { hasNextPage endCursor } } }", {"team": team_id()}, ["issueLabels"])
    found = {label["name"]: label["id"] for label in labels}
    if name not in found:
        made = gql("YggLabelCreate", "mutation YggLabelCreate($input: IssueLabelCreateInput!) { issueLabelCreate("
                   "input: $input) { success issueLabel { id name } } }", {"input": {"name": name, "teamId": team_id()}})
        _succeeded(made, "issueLabelCreate", "issueLabel", version=False)
        found[name] = made["issueLabelCreate"]["issueLabel"]["id"]
    _remember(**{"label:" + label: identifier for label, identifier in found.items()})
    return found[name]


def milestone_id(project: str, number: int) -> str:
    name = "Stage " + str(number)
    key = "milestone:" + project + ":" + name
    cached = _known(key)
    if cached:
        return cached
    pid = project_id(project)
    found = {node["name"]: node["id"] for node in pages(
        "YggMilestones", "query YggMilestones($id: String!, $after: String) { project(id: $id) { projectMilestones("
        "first: 50, after: $after) { nodes { id name } pageInfo { hasNextPage endCursor } } } }",
        {"id": pid}, ["project", "projectMilestones"])}
    if name not in found:
        made = gql("YggMilestoneCreate", "mutation YggMilestoneCreate($input: ProjectMilestoneCreateInput!) { "
                   "projectMilestoneCreate(input: $input) { success projectMilestone { id name } } }",
                   {"input": {"projectId": pid, "name": name}})
        _succeeded(made, "projectMilestoneCreate", "projectMilestone", version=False)
        found[name] = made["projectMilestoneCreate"]["projectMilestone"]["id"]
    _remember(**{"milestone:" + project + ":" + n: i for n, i in found.items()})
    return found[name]


# ---------------------------------------------------------------- issues

KIND_LABELS = ("exception", "issue", "risk", "rework", "exception-forecast", "decision")
REF = "ygg-ref: "


def _issue_id(project: str, identifier: str) -> str | None:
    """The Linear issue the app created for a record: only the registered one, never a title or header match
    (anyone with team access can make an issue that looks like Yggdrasil's)."""
    key = "issue:" + project + ":" + identifier
    found = _known(key) or (_registry()["issues"].get(project + "/" + identifier) or {}).get("linear")
    if found:
        _remember(**{key: found})
    return found


class _Settled(YggError):
    """`_resolve_issue` read the issue completely and settled it: its newer version was adopted into the
    cache (the command is to be run again) or it was refused as foreign (alerted)."""


def _complete_issue(data: dict, linear_id: str) -> dict:
    """The issue of a read, or an error (exit 8) when the answer is incomplete: no issue, another id, no
    version or no description. Nothing is concluded from an incomplete read."""
    remote = (data or {}).get("issue")
    if not isinstance(remote, dict) or remote.get("id") != linear_id or \
            not isinstance(remote.get("updatedAt"), str) or not remote["updatedAt"] or \
            not isinstance(remote.get("description"), str):
        raise YggError(util.EXIT_TRACKER, "linear: the read of issue " + linear_id + " was incomplete; nothing "
                       "was concluded from it")
    return remote


def _resolve_issue(project: str, identifier: str, linear_id: str, registry_key: str) -> dict:
    """Before an issue is written: Linear must hold what the cache was synced from. A newer version that the
    app wrote (its confirmed hash, or an unresolved pending write that landed since the version it started
    from) is adopted into the cache and the command refused, so its re-run starts from it; anything else is
    refused as foreign. Returns the registry entry to build the next write on. A failed or incomplete read
    raises before anything is changed, so pending evidence and the unsynced mark stay for the next try."""
    entry = dict(_registry()["issues"].get(registry_key) or {})
    data = gql("YggIssueRead", "query YggIssueRead($id: String!) { issue(id: $id) { id updatedAt description } }",
               {"id": linear_id})
    remote = _complete_issue(data, linear_id)
    synced = _known("issue-updated:" + linear_id)
    if synced is not None and remote.get("updatedAt") == synced and not entry.get("pending"):
        return entry  # Linear holds exactly what the cache was synced from
    text = decode(remote.get("description"))
    digest = _sha(text) if text is not None else None
    if entry.get("pending"):
        landed = digest == entry["pending"] and remote.get("updatedAt") not in (entry.get("pending_from"), None)
        if landed:  # an earlier write reached Linear and stopped before its confirmation
            entry = {"linear": linear_id, "sha256": digest, "updated_at": remote.get("updatedAt")}
        else:  # it never reached Linear: the confirmed version still stands
            entry = {k: v for k, v in entry.items() if k not in ("pending", "pending_from")}
        # Marked unsynced before the pending hash goes, until the cache below holds what Linear holds.
        _remember(**{UNSYNCED + registry_key: linear_id})
        _register("issues", registry_key, entry)
    if digest is None or digest != entry.get("sha256"):
        refused = _foreign_alert("issue", registry_key, "was changed in Linear by someone other than the app")
        raise _Settled(refused.code, refused.message)
    path = cache_root() / "projects" / project / "issues" / (identifier + ".md")
    if _sha(util.read_text(path)) != digest:
        # The app's own newer version, which the cache never received: adopted durably first, its version
        # recorded only after, and the command (computed from the stale cache) refused for a re-run.
        util.atomic_write(path, text)
        _remember(**{"issue-updated:" + linear_id: remote.get("updatedAt"), UNSYNCED + registry_key: None})
        raise _Settled(util.EXIT_TRACKER, "linear: issue " + identifier + " changed in Linear since it was read; "
                       "run the command again")
    _remember(**{"issue-updated:" + linear_id: remote.get("updatedAt"), UNSYNCED + registry_key: None})
    return entry


# A local mark (`unsynced:<project>/<id>`): the registry may name a version of the issue the cache does
# not hold yet. Set before the registry is confirmed, cleared once the cache and its synced revision are.
UNSYNCED = "unsynced:"


def _not_applied(error: LinearError) -> bool:
    """Whether Linear proved it did not run the mutation: it refused the request (a bad token, a missing
    permission, a rate limit, or a request it rejected as invalid with a 4xx status) before running it."""
    if error.kind in ("auth", "forbidden", "ratelimited"):
        return True
    return error.kind == "invalid" and isinstance(error.status, int) and 400 <= error.status < 500


def pending_issues() -> list[str]:
    """Registry keys (`<project>/<id>`) of issue writes not settled yet: a pending write whose outcome is
    unknown, or a confirmed version the cache may not hold (read locally, no request)."""
    try:
        issues = _registry()["issues"]
    except YggError:
        issues = {}
    marked = {key[len(UNSYNCED):] for key, value in _ids().items() if key.startswith(UNSYNCED) and value}
    return sorted({key for key, entry in issues.items() if entry.get("pending")} | marked)


def _reconcile_pending() -> list[str]:
    """Settle every issue write whose outcome is unknown from what Linear holds now: a write that landed is
    adopted into the cache and confirmed, one that did not is forgotten, anything else is refused as foreign
    (one alert, as on the write path). An issue Linear cannot be read for now stays pending, and held in
    status, until a later tick. Returns the cache paths that changed."""
    changed = []
    for key in pending_issues():
        entry = _registry()["issues"].get(key) or {}
        project, _, identifier = key.partition("/")
        if not entry.get("linear") or not ISSUE_ID.fullmatch(identifier) or not SAFE_PART.fullmatch(project):
            _remember(**{UNSYNCED + key: None})  # nothing registered to settle it from
            continue
        relative = "projects/" + project + "/issues/" + identifier + ".md"
        path = cache_root() / relative
        before = util.read_text(path) if path.exists() else None
        try:
            _resolve_issue(project, identifier, str(entry["linear"]), key)
        except _Settled:
            pass  # adopted into the cache, or refused as foreign (alerted)
        except YggError:
            continue  # a failed or incomplete read, or a failed write: everything stays for the next tick
        if _known(UNSYNCED + key):
            _remember(**{UNSYNCED + key: None})  # settled from a complete read, the cache durable
        if (util.read_text(path) if path.exists() else None) != before:
            changed.append(relative)
    return changed


def _posted(linear_id: str) -> set:
    """The ygg-ref of every comment already on the issue, so a re-run never posts a record twice."""
    refs = set()
    for node in pages("YggComments", "query YggComments($id: String!, $after: String) { issue(id: $id) { "
                      "comments(first: 50, after: $after) { nodes { body } pageInfo { hasNextPage endCursor } } } }",
                      {"id": linear_id}, ["issue", "comments"]):
        for line in str(node.get("body") or "").splitlines():
            if line.startswith(REF):
                refs.add(line[len(REF):].strip())
    return refs


def _put_issue(project: str, identifier: str, text: str, previous: str):
    from ..tracker import parse_issue
    issue = parse_issue(project, identifier, text)
    header = issue.header
    pid = project_id(project)
    kind = str(header.get("kind") or "")
    if kind == "worker-task":
        title = str(header.get("title") or identifier)  # its task text, as a sub-issue of its package
    else:
        title = identifier + " " + str(header.get("title") or header.get("question") or header.get("reason")
                                       or kind)
    labels = [str(label) for label in (header.get("labels") or [])]
    labels.append("std:v" + str(int(header.get("standard_version") or 1)))
    if kind in KIND_LABELS and kind not in labels:
        labels.append(kind)
    payload = {"title": title.strip()[:250], "description": encode(text),
               "labelIds": [label_id(label) for label in dict.fromkeys(labels)]}
    milestone = header.get("milestone")
    if isinstance(milestone, int) and not isinstance(milestone, bool):
        payload["projectMilestoneId"] = milestone_id(project, milestone)
    linear_id = _issue_id(project, identifier)
    registry_key = project + "/" + identifier
    if linear_id:
        entry = _resolve_issue(project, identifier, linear_id, registry_key)
        # The content about to be written is registered as pending, with the version it replaces. Only an
        # update Linear proves it did not apply (it refused the request before running it, or answered
        # `success: false`) restores the entry. Any other outcome (a lost or incomplete answer, an error
        # reported after the mutation ran, an interrupted call) keeps it, and the next tick or a rehydrate
        # settles the write from the issue Linear holds (the evidence is dropped only once it shows the write
        # did not land).
        _register("issues", registry_key, dict(entry, linear=linear_id, pending=_sha(text),
                                               pending_from=entry.get("updated_at")))
        restored = {k: v for k, v in entry.items() if k not in ("pending", "pending_from")} or None
        try:
            answer = gql("YggIssueUpdate", "mutation YggIssueUpdate($id: String!, $input: IssueUpdateInput!) "
                         "{ issueUpdate(id: $id, input: $input) { success issue { id updatedAt } } }",
                         {"id": linear_id, "input": payload})
        except LinearError as error:
            if _not_applied(error):
                _register("issues", registry_key, restored)
            raise
        if ((answer or {}).get("issueUpdate") or {}).get("success") is False:
            _register("issues", registry_key, restored)
        made = _succeeded(answer, "issueUpdate", "issue")
        updated = ((made.get("issueUpdate") or {}).get("issue") or {}).get("updatedAt")
    else:
        payload.update(teamId=team_id(), projectId=pid)
        if header.get("parent"):
            parent = _issue_id(project, str(header["parent"]))
            if parent:
                payload["parentId"] = parent
        made = _succeeded(gql("YggIssueCreate", "mutation YggIssueCreate($input: IssueCreateInput!) { issueCreate("
                              "input: $input) { success issue { id identifier updatedAt } } }", {"input": payload}),
                          "issueCreate", "issue")
        linear_id = made["issueCreate"]["issue"]["id"]
        updated = made["issueCreate"]["issue"].get("updatedAt")
        _remember(**{"issue:" + project + ":" + identifier: linear_id})
    # Confirmed at once, before the cache write: the registry names the content Linear now holds, with its
    # version, so a crash from here on leaves a rebuild (or the next write) able to adopt it. The issue is
    # marked unsynced first, locally, and stays due for reconciliation until the cache holds the version and
    # its synced revision is recorded (the follow-up clears the mark).
    _remember(**{UNSYNCED + registry_key: linear_id})
    _register("issues", registry_key, {"linear": linear_id, "sha256": _sha(text), "updated_at": updated})
    # The issue now holds the record: the cache is written next, and only then the follow-ups (the registry
    # entry and the comments), so a failing follow-up can never hide an obligation Linear already holds.
    # Records and comments are append-only: each new one is also posted as a Linear comment. Its identity is
    # its position (`r<n>` or `c<n>`), so the re-run of a command that failed after posting finds it again.
    old = parse_issue(project, identifier, previous) if previous else None
    due_key = "comments-due:" + project + "/" + identifier
    due = set(json.loads(_known(due_key) or "[]"))
    fresh = [("r" + str(n), entry) for n, entry in enumerate(issue.records)
             if not old or n >= len(old.records) or "r" + str(n) in due]
    fresh += [("c" + str(n), entry) for n, entry in enumerate(issue.comments)
              if not old or n >= len(old.comments) or "c" + str(n) in due]

    def follow_up() -> None:
        _remember(**{"issue-updated:" + linear_id: updated, UNSYNCED + registry_key: None})
        if not fresh:
            return
        # Until posted, the records stay due here, so the issue's next write posts them (never twice: refs).
        _remember(**{due_key: json.dumps(sorted({p for p, _ in fresh} | due))})
        posted = _posted(linear_id)
        for position, entry in fresh:
            ref = project + "/" + identifier + "#" + position
            if ref in posted:
                continue
            body = "kind: " + str(entry.get("kind")) + "\n\n" + str(entry.get("body") or util.dump_yaml(entry))
            _succeeded(gql("YggComment", "mutation YggComment($input: CommentCreateInput!) { commentCreate("
                           "input: $input) { success } }",
                           {"input": {"issueId": linear_id, "body": body + "\n\n" + REF + ref}}), "commentCreate")
        _remember(**{due_key: "[]"})
    return follow_up


HEALTH = re.compile(r"(?m)^health:\s*(onTrack|atRisk|offTrack)\s*$")


def _put_update(project: str, pid: str, relative: str, text: str) -> None:
    """A highlight report is a Linear project update with its health; its ref is its cache path."""
    ref = relative
    for node in pages("YggProjectUpdates", "query YggProjectUpdates($id: String!, $after: String) { project(id: $id) "
                      "{ projectUpdates(first: 50, after: $after) { nodes { body } pageInfo { hasNextPage endCursor } } "
                      "} }", {"id": pid}, ["project", "projectUpdates"]):
        if (REF + ref) in str(node.get("body") or ""):
            return
    match = HEALTH.search(text)
    _succeeded(gql("YggProjectUpdate", "mutation YggProjectUpdate($input: ProjectUpdateCreateInput!) { "
                   "projectUpdateCreate(input: $input) { success } }",
                   {"input": {"projectId": pid, "health": match[1] if match else "onTrack",
                              "body": text + "\n\n" + REF + ref}}), "projectUpdateCreate")


# ---------------------------------------------------------------- write-through and revision

def push(relative: str, text: str) -> dict:
    """Write one tracker file to Linear (called before the cache file is written). Returns what the caller
    passes to `synced` once the cache file is written."""
    parts = relative.split("/")
    previous = util.read_text(cache_root() / relative)
    if parts[0] == "_ygg" and len(parts) >= 2:
        return _put_document("project", project_id(CONTROL), relative, relative, text)
    if parts[0] == "initiatives" and len(parts) >= 3:
        container = initiative_id(parts[1])
        rest = "/".join(parts[2:])
        if rest.startswith("documents/") and rest.endswith(".md") and rest.count("/") == 1:
            rest = rest[len("documents/"):-3]
        return _put_document("initiative", container, relative, rest, text)
    if parts[0] == "projects" and len(parts) >= 3:
        name, rest = parts[1], "/".join(parts[2:])
        programme = None
        if rest == "project.json":
            try:
                programme = (json.loads(text or "{}") or {}).get("programme")
            except (ValueError, AttributeError):
                programme = None
        pid = project_id(name, programme)
        if parts[2] == "issues" and len(parts) == 4 and rest.endswith(".md"):
            return {"follow_up": _put_issue(name, parts[3][:-3], text, previous)}
        if parts[2] == "updates" and rest.endswith("-highlight.md"):
            _put_update(name, pid, relative, text)
        if rest == "milestones.json":
            try:
                for stage in json.loads(text).get("stages") or []:
                    milestone_id(name, int(stage["number"]))
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
        if rest.startswith("documents/") and rest.endswith(".md") and rest.count("/") == 1:
            rest = rest[len("documents/"):-3]
        return _put_document("project", pid, relative, rest, text)
    raise YggError(util.EXIT_TRACKER, "linear: no mapping for tracker path " + relative)


def refresh() -> list[str]:
    """One request: the revision of every guarded document. A document that changed in Linear is read again
    into the cache. Returns the cache paths that changed (none on an idle tick). Runs under the journal lock,
    the lock every journal and counter writer holds across its read and write, so a refresh can never change
    the cache between a writer's read and its write."""
    with util.locked():
        return _reconcile_pending() + _refresh_locked()


def _refresh_locked() -> list[str]:
    ids = _ids()
    guarded = sorted(k.split(":", 1)[1] for k in ids if k.startswith("guarded:"))
    if not guarded:
        return []
    data = gql("YggRevision", "query YggRevision($ids: [ID!]) { documents(first: 50, filter: {id: {in: $ids}}) { "
               "nodes { id updatedAt } } }", {"ids": guarded})
    changed = []
    for node in (data.get("documents") or {}).get("nodes") or []:
        if node.get("updatedAt") in (ids.get("updated:" + node["id"]), ids.get("rejected:" + node["id"])):
            continue  # unchanged, or a version already refused (alerted once, not read again)
        relative = ids.get("path:" + node["id"])
        if not relative:
            continue
        if _adopt(node["id"], relative, _read_document(node["id"])):
            changed.append(relative)
    return changed


# ---------------------------------------------------------------- rebuild from Linear alone

EXTENSIONS = (".json", ".md", ".yaml", ".yml", ".jsonl", ".txt")
DOC_READ = "id title content updatedAt creator { id } updatedBy { id }"


ISSUE_ID = re.compile(r"[A-Za-z][A-Za-z0-9]*-[0-9]+")
SAFE_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


def _rest_for(title: str) -> str | None:
    """The container-relative cache path of a document title (the inverse of `push`), or None for a title
    that could name a path outside its container (`..`, absolute, or an unexpected character)."""
    rest = title if ("/" in title or title.endswith(EXTENSIONS)) else "documents/" + title + ".md"
    parts = rest.split("/")
    if not all(SAFE_PART.fullmatch(part) and part not in (".", "..") for part in parts):
        return None
    return rest


def _actors() -> set:
    return {_viewer()} | {str(a) for a in (config.get("linear_authority_actor_ids") or [])}


def _trusted(node: dict, actors: set) -> bool:
    """Created and last updated by the app or an authority actor, both named: a missing or null last updater
    (Linear gives null for a deleted account) is unknown, never trusted."""
    creator = (node.get("creator") or {}).get("id")
    updater = (node.get("updatedBy") or {}).get("id")
    return creator in actors and updater in actors


def _foreign(what: str, name: str, skipped: list) -> None:
    skipped.append(what + " " + name)


def _write_cache(relative: str, text: str) -> None:
    util.atomic_write(cache_root() / relative, text)


def _pull_documents(container: str, container_id: str, base: str, actors: set, skipped: list) -> int:
    field = "project" if container == "project" else "initiative"
    count = 0
    for node in pages("YggPullDocuments", "query YggPullDocuments($id: String!, $after: String) { " + field +
                      "(id: $id) { documents(first: 50, after: $after) { nodes { " + DOC_READ + " } "
                      "pageInfo { hasNextPage endCursor } } } }", {"id": container_id}, [field, "documents"]):
        rest = _rest_for(str(node.get("title") or ""))
        text = decode(node.get("content"))
        if rest is None or text is None or not _trusted(node, actors):
            _foreign("document", base + str(node.get("title")), skipped)
            continue
        relative = base + rest
        values = {"document:" + container_id + ":" + node["title"]: node["id"]}
        if GUARDED.match(relative):
            if not _adopt(node["id"], relative, node):
                continue
            values.update({"guarded:" + node["id"]: relative, "path:" + node["id"]: relative,
                           "updated:" + node["id"]: node.get("updatedAt")})
        else:
            _write_cache(relative, text)
        _remember(**values)
        count += 1
    return count


def _registered(nodes: list, registered: dict, actors: set, what: str, skipped: list) -> list:
    """The containers to adopt: created by the app, registered under their name, one per name. A name used
    more than once, or a container the registry does not name, is skipped (never adopted by name)."""
    names: dict = {}
    for node in nodes:
        names.setdefault(str(node.get("name")), []).append(node)
    chosen = []
    for name, group in names.items():
        mine = [n for n in group if registered.get(name) == n.get("id")]
        for node in group:
            if node not in mine and (node.get("creator") or {}).get("id") in actors:
                _foreign(what, name + " (" + str(node.get("id")) + ", unregistered)", skipped)
        if len(group) > 1 and mine:
            _foreign(what, name + " (" + str(len(group)) + " with this name)", skipped)
        if len(mine) == 1 and SAFE_PART.fullmatch(name) and (mine[0].get("creator") or {}).get("id") in actors:
            chosen.append(mine[0])
    return chosen


def pull() -> dict:
    """Rebuild the cache from Linear alone. Returns counts; foreign objects are skipped and alerted once."""
    actors = _actors()
    skipped: list = []
    counts = {"documents": 0, "issues": 0}
    with util.locked():
        control = project_id(CONTROL)
        counts["documents"] += _pull_documents("project", control, "", actors, skipped)
        registry = _registry()  # pulled with the control project's documents, from a trusted document only
        initiatives = pages("YggPullInitiatives", "query YggPullInitiatives($after: String) { initiatives(first: 50, "
                            "after: $after) { nodes { id name creator { id } } pageInfo { hasNextPage endCursor } } }",
                            {}, ["initiatives"])
        for node in _registered(initiatives, registry["initiatives"], actors, "initiative", skipped):
            _remember(**{"initiative:" + node["name"]: node["id"]})
            counts["documents"] += _pull_documents("initiative", node["id"], "initiatives/" + node["name"] + "/",
                                                   actors, skipped)
        projects = [p for p in pages("YggPullProjects", "query YggPullProjects($team: String!, $after: String) { "
                                     "team(id: $team) { projects(first: 50, after: $after) { nodes { id name creator "
                                     "{ id } } pageInfo { hasNextPage endCursor } } } }", {"team": team_id()},
                                     ["team", "projects"]) if p.get("name") != CONTROL]
        for node in _registered(projects, registry["projects"], actors, "project", skipped):
            name = node["name"]
            _remember(**{"project:" + name: node["id"]})
            counts["documents"] += _pull_documents("project", node["id"], "projects/" + name + "/", actors, skipped)
            for issue in pages("YggPullIssues", "query YggPullIssues($id: String!, $after: String) { project(id: $id) "
                               "{ issues(first: 50, after: $after) { nodes { id description updatedAt creator { id } } "
                               "pageInfo { hasNextPage endCursor } } } }", {"id": node["id"]}, ["project", "issues"]):
                text = decode(issue.get("description"))
                header, _ = util.split_frontmatter(text or "")
                identifier = str((header or {}).get("id") or "")
                entry = registry["issues"].get(name + "/" + identifier) or {}
                # Adopted only as the issue the app registered for that record, with the content it wrote (its
                # confirmed hash, or a pending write that landed after the version it replaced: the next tick
                # confirms it): an edited description, or a header naming another record, is refused.
                landed = text is not None and bool(entry.get("pending")) and _sha(text) == entry["pending"] and \
                    issue.get("updatedAt") not in (entry.get("pending_from"), None)
                if text is None or not ISSUE_ID.fullmatch(identifier) or \
                        (issue.get("creator") or {}).get("id") not in actors or \
                        entry.get("linear") != issue.get("id") or (_sha(text) != entry.get("sha256") and not landed):
                    _foreign("issue", name + "/" + (identifier or str(issue.get("id"))), skipped)
                    continue
                _write_cache("projects/" + name + "/issues/" + identifier + ".md", text)
                _remember(**{"issue:" + name + ":" + identifier: issue["id"],
                             "issue-updated:" + issue["id"]: issue.get("updatedAt")})
                counts["issues"] += 1
    if skipped:
        state.alert("ygg-linear-foreign", "Linear objects not written by Yggdrasil were not adopted: " +
                    ", ".join(skipped[:20]) + (" and " + str(len(skipped) - 20) + " more" if len(skipped) > 20 else ""),
                    count=len(skipped))
    counts["skipped"] = len(skipped)
    return counts
