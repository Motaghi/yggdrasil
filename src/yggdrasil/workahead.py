"""Work ahead into later stages (Amendment A4, rulings 68 to 70; contracts A4.1).

Every decision here is derived from tracker records alone (the approved PID snapshot, work-ahead-setting
records on the PID document, work-ahead and work-ahead-review records on the package, envelopes and package
states), so `ygg rehydrate` needs no local state to restore it.

Enforcement is one reducer blocker (`screen`). A team lead's owed work items and unread messages are each
mapped to the packages they are about. An item about a later-stage package that may not be worked on now is
withheld: it is left out of the reserved work set, so it is never consumed and stays owed, and it never
makes the lead a launch candidate on its own. A lead whose owed work is all withheld is held, and its held
reason is the one of its lowest-numbered held package. Nothing about a hold is stored: every read derives
it again, so a switch-off, a lapsed envelope or a new approval takes effect on the next read.
"""

from __future__ import annotations

import hashlib
import json
import re

from . import state, tracker, util

SETTINGS = ("off", "conditional")
PID_OFF = "work-ahead: pid off"
NO_ENVELOPE = "work-ahead: no envelope"
CURRENT_WORK = "work-ahead: current-stage work available"
NO_RECORD = "work-ahead: no record"
REJECTED_SU = "work-ahead: rejected by su"
REJECTED_REVIEWER = "work-ahead: rejected by reviewer"
AWAITING_BOTH = "work-ahead: awaiting su and reviewer approval"
AWAITING_SU = "work-ahead: awaiting su approval"
AWAITING_REVIEWER = "work-ahead: awaiting reviewer approval"
# Owed work withheld only for suspended later-stage packages (a not-ok work-ahead morning-go item).
SUSPENDED = "work-ahead: package suspended"
# Authorised, unfinished states (the WIP states): a CI result on the lane can be about any of them.
UNFINISHED = ("authorised", "ready-for-test", "rejected")
PACKAGE_RE = re.compile(r"\bWP-[0-9]+\b")
FLAG_BRANCH_RE = re.compile(r"^[a-z-]+: (\S+) in ")


def normalise(value):
    """The PID enum as written. YAML reads an unquoted `off` as false; that is the documented `off`."""
    if value is False:
        return "off"
    return value


# ---------------------------------------------------------------- setting

def setting(project_id: str) -> tuple[str, str]:
    """(effective setting, source label): the newest authority `work-ahead-setting` record, else the
    approved snapshot, else `off` from core. Before approval, the live PID value (as for other PID keys)."""
    from .commands import approved_pid, authority_actors
    label = "pid:" + project_id
    text = tracker.read_document(project_id, "pid")
    approved = approved_pid(project_id)
    if approved is None:
        header = util.split_frontmatter(text)[0] if text else None
        value = normalise((header or {}).get("work_ahead"))
        return (str(value), label) if value is not None else ("off", "core")
    if text:
        issue = tracker.parse_issue(project_id, "pid", text)
        actors = set(authority_actors())
        records = [r for r in issue.records if r.get("kind") == "work-ahead-setting"
                   and r.get("actor") in actors and r.get("setting") in SETTINGS]
        if records:
            return str(records[-1]["setting"]), label
    value = normalise((approved.get("guarded") or {}).get("work_ahead"))
    if value is not None:
        return str(value), label
    return "off", "core"


# ---------------------------------------------------------------- stages and packages

def executing_stage(project_id: str) -> int | None:
    stages = tracker.milestones(project_id).get("stages") or []
    current = next((s for s in stages if s.get("state") == "executing"), None)
    return int(current["number"]) if current else None


def is_later(issue, executing: int | None) -> bool:
    """A later-stage package: its milestone is greater than the executing stage's number."""
    if executing is None or issue is None or issue.get("kind") != "work-package":
        return False
    milestone = issue.get("milestone")
    return isinstance(milestone, int) and not isinstance(milestone, bool) and milestone > executing


def _number(package_id: str) -> int:
    match = re.search(r"([0-9]+)$", package_id)
    return int(match[1]) if match else 0


def content_hash(issue) -> str:
    """What an approval is bound to: the package title, body, stage and lane."""
    payload = json.dumps({"title": issue.get("title"), "body": issue.body or "", "stage": issue.get("milestone"),
                          "lane": issue.get("lane")}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _decision_scoped(issues: list) -> tuple[set, set]:
    """Packages and lanes named by the scope of an open decision."""
    from . import decisions
    packages, lanes = set(), set()
    for issue in issues:
        if not decisions.is_open(issue):
            continue
        scope = issue.get("scope") or {}
        if scope.get("type") == "package" and scope.get("id"):
            packages.add(scope["id"])
        elif scope.get("type") == "lane" and scope.get("id"):
            lanes.add(scope["id"])
    return packages, lanes


def current_stage_work(project_id: str, executing: int | None, issues: list | None = None) -> bool:
    """Condition (c): an executing-stage package that is authorised, or planned with a criteria record.

    Suspended packages and packages named (directly or through their lane) by an open decision's scope
    do not count as work that can be done.
    """
    if executing is None:
        return False
    issues = issues if issues is not None else tracker.issues(project_id)
    scoped_packages, scoped_lanes = _decision_scoped(issues)
    for issue in issues:
        if issue.get("kind") != "work-package" or issue.get("milestone") != executing:
            continue
        if issue.id in scoped_packages or (issue.get("lane") and issue.get("lane") in scoped_lanes):
            continue
        if issue.get("state") == "authorised":
            return True
        if issue.get("state") == "planned" and any(r.get("kind") == "criteria" for r in issue.records):
            return True
    return False


# ---------------------------------------------------------------- records and reviews

def newest_record_index(issue) -> int | None:
    found = [i for i, r in enumerate(issue.records) if r.get("kind") == "work-ahead" and r.get("actor") == "ygg-app"]
    return found[-1] if found else None


def _review_counts(record: dict, project: dict | None) -> bool:
    """A review counts only when its real caller is the reviewing node itself, or the user's own shell."""
    caller = record.get("caller")
    if caller not in (record.get("node"), "user"):
        return False
    return not (project and caller == project.get("pm"))


def counted_reviews(issue, project: dict | None = None) -> dict:
    """role -> verdict of the newest review written after the newest work-ahead record (record order),
    bound to the package's current content and written by the reviewing node itself."""
    start = newest_record_index(issue)
    if start is None:
        return {}
    current = content_hash(issue)
    counted = {}
    for record in issue.records[start + 1:]:
        if record.get("kind") == "work-ahead-review" and record.get("actor") == "ygg-app" and \
                record.get("role") in ("su", "reviewer") and record.get("content") == current and \
                _review_counts(record, project):
            counted[record["role"]] = record.get("verdict")
    return counted


def reason_for(project: dict, issue, *, executing: int | None = None, issues: list | None = None) -> str | None:
    """The first unmet condition, (a) to (e) in order, for a later-stage package; None when all hold.

    A work-ahead record written for different package content (title, body, stage or lane) is no record.
    """
    from . import envelopes
    if setting(project["id"])[0] != "conditional":
        return PID_OFF
    if envelopes.effective_envelope(project) is None:
        return NO_ENVELOPE
    executing = executing if executing is not None else executing_stage(project["id"])
    if current_stage_work(project["id"], executing, issues):
        return CURRENT_WORK
    index = newest_record_index(issue)
    if index is None or issue.records[index].get("content") != content_hash(issue):
        return NO_RECORD
    reviews = counted_reviews(issue, project)
    if reviews.get("su") == "reject":
        return REJECTED_SU
    if reviews.get("reviewer") == "reject":
        return REJECTED_REVIEWER
    su, reviewer = reviews.get("su") == "approve", reviews.get("reviewer") == "approve"
    if not su and not reviewer:
        return AWAITING_BOTH
    if not su:
        return AWAITING_SU
    if not reviewer:
        return AWAITING_REVIEWER
    return None


# ---------------------------------------------------------------- the gate (a reducer blocker)

class _View:
    """Project, executing stage, issues and the five conditions per package, read from the tracker."""

    def __init__(self, node: dict):
        self.node = node
        self.project = tracker.project(node["project"]) if node.get("project") else None
        self.executing = executing_stage(node["project"]) if self.project else None
        self.issues = tracker.issues(node["project"]) if self.project else []
        self.by_id = {i.id: i for i in self.issues}
        self._reasons: dict[str, str | None] = {}

    def later(self, package_id: str) -> bool:
        return is_later(self.by_id.get(package_id), self.executing)

    def reason(self, package_id: str) -> str | None:
        if package_id not in self._reasons:
            self._reasons[package_id] = reason_for(self.project, self.by_id[package_id],
                                                   executing=self.executing, issues=self.issues)
        return self._reasons[package_id]

    def suspended(self, package_id: str) -> bool:
        """A suspended later-stage package (a not-ok work-ahead item): nothing about it wakes the lead."""
        return self.later(package_id) and self.by_id[package_id].get("state") == "suspended"

    def held(self, package_id: str) -> bool:
        return self.later(package_id) and not self.suspended(package_id) and self.reason(package_id) is not None

    def blocked(self, package_id: str) -> bool:
        """A later-stage package of the project that may not be worked on now (held or suspended)."""
        return package_id in self.by_id and (self.suspended(package_id) or self.held(package_id))

    def lane_packages(self, states) -> list:
        return [i for i in self.issues if i.get("kind") == "work-package" and i.get("lane") == self.node["id"]
                and i.get("state") in states]


def message_packages(item: dict) -> list[str]:
    """Every package id a message refers to: its package field and every id anywhere in its body."""
    found = [str(item["package"])] if item.get("package") else []
    for match in PACKAGE_RE.findall(str(item.get("body") or "")):
        if match not in found:
            found.append(match)
    return found


class Screen:
    """A team lead's owed work, split by the work-ahead gate.

    `items` and `messages` are what a launch may be given; `withheld` is what stays owed; `held` lists the
    held packages, lowest-numbered first; `reason` is the held reason when withheld work is all the node
    owes (None otherwise); `blocks` is True when nothing may be given although something is owed. `ahead`
    names the later-stage packages the given work is about, `packages` every package it is about.
    """

    def __init__(self, items, messages, withheld=(), held=(), reason=None, blocks=False, ahead=(), packages=(),
                 blocked=()):
        self.items, self.messages = list(items), list(messages)
        self.withheld, self.held = list(withheld), list(held)
        self.reason, self.blocks = reason, blocks
        self.ahead, self.packages = list(ahead), list(packages)
        self.blocked = sorted(blocked, key=_number)  # every blocked package of the project at screening time


def _view_for(node_id: str, nodes: dict | None) -> _View | None:
    nodes = nodes if nodes is not None else state.all_nodes()
    node = nodes.get(node_id)
    if not node or node.get("role") != "tl" or not node.get("project"):
        return None
    view = _View(node)
    if view.project is None or view.executing is None:
        return None  # no stage executes: no package is a later-stage package
    return view


def _activation_packages(activation: str) -> list[str]:
    """The packages a retried or capacity-held activation was about, from its reserved fact (the chain
    carries the package purpose, so a retry is withheld exactly when its packages are)."""
    from . import reducer
    for programme_fold in reducer.folds().values():
        record = programme_fold["activations"].get(activation)
        if record is not None:
            return [p for p in record.get("packages") or [] if isinstance(p, str)]
    return []


def _item_packages(view: _View, item: dict, inbox: dict) -> tuple[list[str], bool]:
    """(packages an owed item is about, whether any one blocked package withholds it). Messages and
    records naming a blocked package anywhere are withheld, and so is a retry or capacity relaunch whose
    activation was about one: consumption is tracked per item, not per package, so a mixed continuation is
    held back whole rather than spent on its allowed part. A CI result is withheld only when every unfinished
    package of the lane is blocked (it is also about the lane's executing-stage work)."""
    kind, reason = item.get("kind"), str(item.get("reason") or "")
    if kind == "message":
        message = inbox.get(item.get("id")) or {}
        found = message_packages(message) or list(item.get("packages") or [])
        return found, True
    if kind == "record":
        found = [str(item["package"])] if item.get("package") else []
        found += [p for p in PACKAGE_RE.findall(reason) if p not in found]
        return found, True
    if kind in ("retry", "capacity"):
        return _activation_packages(str(item.get("id"))), True
    if kind == "event" and reason.startswith("ci:"):
        return [i.id for i in view.lane_packages(UNFINISHED)], False
    return PACKAGE_RE.findall(reason), True


def screen(node_id: str, items: list[dict], messages: list[dict] | None = None,
           nodes: dict | None = None) -> Screen:
    """Split a node's owed items and unread messages by the work-ahead gate (Amendment A4). Nodes that are
    not team leads, and team leads of a project with no executing stage, are never screened."""
    messages = list(messages if messages is not None else
                    [m for m in state.inbox(node_id) if not m.get("acked_at")])
    view = _view_for(node_id, nodes)
    if view is None:
        return Screen(items, [m["msg_id"] for m in messages])
    inbox = {m["msg_id"]: m for m in messages}
    allowed, withheld, blocked_seen, packages = [], [], [], []

    def judge(found: list[str], any_blocks: bool) -> bool:
        # A cancelled package is left out wherever a message's packages are derived (A11.1 AC-06, AC-13).
        known = [p for p in found if p in view.by_id and view.by_id[p].get("state") != "cancelled"]
        blocked = [p for p in known if view.blocked(p)]
        blocked_seen.extend(p for p in blocked if p not in blocked_seen)
        if blocked and (any_blocks or len(blocked) == len(known)):
            return False
        packages.extend(p for p in known if p not in packages and not view.blocked(p))
        return True

    for item in items:
        (allowed if judge(*_item_packages(view, item, inbox)) else withheld).append(item)
    withheld_ids = {i.get("id") for i in withheld if i.get("kind") == "message"}
    given_messages = []
    for message in messages:
        if message["msg_id"] in withheld_ids or not judge(message_packages(message), True):
            continue
        given_messages.append(message["msg_id"])
    # An unread message with no owed item (one already given to an earlier activation) is judged by its own
    # text for what a launch may read, but owes no launch: only owed items decide whether the node is held.
    held = sorted((p for p in blocked_seen if view.held(p)), key=_number)
    ahead = sorted((p for p in packages if view.later(p)), key=_number)
    blocks = not allowed and bool(withheld)
    # The reason of the lowest-numbered held package; withheld only for suspended packages, the suspension.
    reason = (view.reason(held[0]) if held else SUSPENDED) if blocks else None
    blocked = [i.id for i in view.issues if view.blocked(i.id)]
    return Screen(allowed, given_messages, withheld, held, reason, blocks, ahead, packages, blocked)


def held_reason(node_id: str, items: list[dict], nodes: dict | None = None) -> str | None:
    return screen(node_id, items, nodes=nodes).reason


def _selection(node_id: str) -> tuple[set, set, set] | None:
    """(given packages, given message ids, packages blocked at reservation) of the node's current
    activation, from its durable reserved fact: the activation this process runs for, else the one the
    journal holds open for the node. None once it is settled, or for a reservation without a selection."""
    from . import lifecycle, reducer
    activation = util.env("YGG_ACTIVATION") if util.env("YGG_NODE") == node_id else None
    activation = activation or lifecycle.open_activation(node_id)
    if not activation:
        return None
    for programme_fold in reducer.folds().values():
        record = programme_fold["activations"].get(str(activation))
        if record is None:
            continue
        if record.get("node") != node_id or "blocked" not in record or record.get("terminal") is not None:
            return None
        messages = set((record.get("work") or {}).get("messages") or [])
        return set(record.get("packages") or []), messages, set(record.get("blocked") or [])
    return None


def blocked_packages(node_id: str, nodes: dict | None = None) -> set[str]:
    """The later-stage packages a team lead may not be shown (held or suspended). While an activation is
    unsettled, every package its work set is about stays shown and every package blocked at its
    reservation stays hidden, whatever has changed since: the context matches what it consumes."""
    view = _view_for(node_id, nodes)
    current = {i.id for i in view.issues if view.blocked(i.id)} if view is not None else set()
    chosen = _selection(node_id)
    if chosen is None:
        return current
    given, _, recorded = chosen
    return (current | recorded) - given


def hidden(node_id: str, item: dict, nodes: dict | None = None) -> bool:
    """Whether a message is kept out of a team lead's context: it refers to a blocked package anywhere. A
    message the current activation was given is never hidden from it."""
    chosen = _selection(node_id)
    if chosen is not None and item.get("msg_id") in chosen[1]:
        return False
    blocked = blocked_packages(node_id, nodes)
    return bool(blocked) and any(p in blocked for p in message_packages(item))


def wip_counts(project_id: str, issue) -> bool:
    """Whether an authorised, unfinished package counts toward the WIP limit. A held later-stage package is
    an early authorisation, not work in progress, so it cannot block current-stage work."""
    executing = executing_stage(project_id)
    if not is_later(issue, executing):
        return True
    project = tracker.project(project_id)
    return reason_for(project, issue, executing=executing) is None


# ---------------------------------------------------------------- morning go and digest

def launch_fields(node: dict, ahead: list[str]) -> dict:
    """What a reservation records on its receipt so the morning-go item can be allocated once startup is
    confirmed: the later-stage packages it works ahead on, their stages and the covering envelope."""
    if not ahead:
        return {}
    stages = {}
    for package in ahead:
        issue = tracker.read_issue(node["project"], package)
        stages[package] = issue.get("milestone") if issue else None
    return {"work_ahead_packages": list(ahead), "work_ahead_stages": stages,
            "work_ahead_envelope": covering_envelope(node["project"])}


def record_launch(activation: str) -> list[str]:
    """Allocate (once per package) the `work-ahead` morning-go item for each package an activation works
    ahead on, and append the launch to it for the digest. Called when process startup is confirmed, from
    the package identities stored on the receipt at reservation. Idempotent per activation."""
    from . import envelopes, launcher, morning_go
    data = launcher.receipt(activation)
    packages = data.get("work_ahead_packages") or []
    if not packages or not data.get("project"):
        return []
    project = tracker.project(str(data["project"]))
    if not project:
        return []
    created = []
    with util.locked():
        items = morning_go.items(project["programme"])
        for package in packages:
            found = next((i for i in items if i.get("kind") == "work-ahead" and i.get("ref") == package
                          and i.get("project") == project["id"]), None)
            launch = {"activation": activation, "started_at": data.get("started_at") or util.iso()}
            if found is None:
                cover = data.get("work_ahead_envelope") or {}
                created.append(envelopes.morning_go_item(
                    project, "work-ahead", package, ranges=False, stage=(data.get("work_ahead_stages") or {})
                    .get(package), lane=data.get("node"), started_at=launch["started_at"],
                    envelope=cover.get("id"), ref_snapshot=cover.get("ref_snapshot") or [], launches=[launch]))
                items = morning_go.items(project["programme"])
                continue
            launches = found.setdefault("launches", [])
            if not any(entry.get("activation") == activation for entry in launches):
                launches.append(launch)
                tracker.write_json(morning_go.item_path(project["programme"], found["id"]), found)
    return created


def covering_envelope(project_id: str) -> dict | None:
    from . import envelopes
    project = tracker.project(project_id)
    envelope = envelopes.effective_envelope(project) if project else None
    if not envelope:
        return None
    return {"id": envelope.get("id"), "ref_snapshot": envelope.get("ref_snapshot") or []}


def lane_ranges(item: dict) -> tuple[list[dict], list[str]]:
    """Commit ranges of a work-ahead item, taken at morning-go build from the envelope snapshot that covered
    its launch to the lane head now. Fail-closed flags are kept unless they name another lane branch."""
    from . import envelopes
    project = tracker.project(str(item.get("project")))
    if not project:
        return [], ["unknown-project"]
    # The launch snapshot is the baseline even when it is empty (granted before any branch existed): a later
    # envelope's snapshot never stands in for it.
    cover = {"ref_snapshot": item["ref_snapshot"]} if isinstance(item.get("ref_snapshot"), list) else None
    ranges, flags = envelopes.commit_ranges(project, envelope=cover)
    branch = "ygg/" + project["id"] + "/lane/" + str(item.get("lane"))
    lane_prefix = "ygg/" + project["id"] + "/lane/"
    kept = []
    for flag in flags:
        match = FLAG_BRANCH_RE.match(flag)
        named = match[1] if match else None
        if named is None or named == branch or not named.startswith(lane_prefix):
            kept.append(flag)
    return [r for r in ranges if r.get("branch") == branch], kept


# ---------------------------------------------------------------- verbs

def set_setting(project_name: str, value: str, reason: str | None) -> str:
    """The user-only change of the approved PID's setting. It owes no launch: every hold is derived on read,
    so withheld work becomes available (or stays withheld) on the next read."""
    from .commands import _project_for, _render_doc, approved_pid, pid_document
    state.check_user_only("pid work-ahead")
    _project_for(project_name)
    if value not in SETTINGS:
        raise util.usage("--set must be off or conditional")
    if approved_pid(project_name) is None:
        raise util.usage("PID not approved for " + project_name)
    if not (reason or "").strip():
        raise util.usage("--reason is required and cannot be empty")
    with util.locked():
        tracker.ensure_writable()
        issue = pid_document(project_name)
        issue.records.append(tracker.record("work-ahead-setting", None, actor="user", setting=value,
                                            reason=util.redact(reason.strip())))
        tracker.write_text(tracker.document_path(project_name, "pid"), _render_doc(issue))
    return "work ahead set to " + value + " for " + project_name + "\n"


def write_record(project_name: str, package: str, as_node: str, reason: str | None, evidence: str | None) -> str:
    """The PM's work-ahead record. The launches it owes (the paired senior user and the programme's
    reviewers) are stamped on the package in the same write."""
    from .commands import _package, _project_for, paired_seniors
    nodes = state.all_nodes()
    state.check_identity(as_node, nodes)
    project = _project_for(project_name)
    if as_node != project["pm"]:
        raise util.refuse("pm-only: only the PM of " + project_name + " writes a work-ahead record")
    if setting(project_name)[0] != "conditional":
        raise util.refuse("work-ahead-off: work ahead is off for " + project_name)
    issue = _package(project_name, package)
    if not is_later(issue, executing_stage(project_name)):
        raise util.usage(package + " is not in a later stage")
    if not (reason or "").strip():
        raise util.usage("--reason is required and cannot be empty")
    if not (evidence or "").strip():
        raise util.usage("--evidence is required and cannot be empty")
    with util.locked():
        tracker.ensure_writable()
        issue = _package(project_name, package)
        issue.records.append(tracker.record("work-ahead", as_node, reason=util.redact(reason.strip()),
                                            evidence=util.redact(evidence.strip()), content=content_hash(issue)))
        targets = paired_seniors(issue.get("lane"), nodes) if issue.get("lane") else []
        targets += [n["id"] for n in nodes.values() if n.get("role") == "reviewer"
                    and n.get("programme") == project["programme"]]
        for node_id in dict.fromkeys(targets):
            tracker.owe(issue.header, node_id, "work-ahead:" + package)
        issue.save()
    return "work-ahead record written on " + package + "\n"


def write_review(project_name: str, package: str, as_node: str, verdict: str, note: str | None) -> str:
    """Separation of duties: the caller must be exactly the reviewing node (no parent delegation), never the
    PM; the real caller is stored, and reviews whose caller does not match are not counted. The PM's launch
    is stamped on the package in the same write; the team lead needs no stamp, because its withheld work
    becomes available on the next read once every condition holds."""
    from .commands import _package, _project_for
    nodes = state.all_nodes()
    project = _project_for(project_name)
    caller = util.current_node()
    if caller is not None and caller != as_node:
        raise util.refuse("work-ahead-role: " + caller + " may not review as " + as_node +
                          "; a work-ahead review is written by the reviewing node itself")
    if as_node == project["pm"] or caller == project["pm"]:
        raise util.refuse("work-ahead-role: the PM cannot review its own work-ahead record")
    node = nodes.get(as_node) or {}
    if node.get("role") == "su" and node.get("project") == project_name:
        role = "su"
    elif node.get("role") == "reviewer" and node.get("programme") == project["programme"]:
        role = "reviewer"
    else:
        raise util.refuse("work-ahead-role: " + as_node + " is not a senior user of " + project_name +
                          " or a reviewer of programme " + str(project["programme"]))
    if verdict not in ("approve", "reject"):
        raise util.usage("--verdict must be approve or reject")
    issue = _package(project_name, package)
    if newest_record_index(issue) is None:
        raise util.usage("no work-ahead record for " + package)
    if verdict == "reject" and not (note or "").strip():
        raise util.usage("--note is required with --verdict reject")
    with util.locked():
        tracker.ensure_writable()
        issue = _package(project_name, package)
        issue.records.append(tracker.record("work-ahead-review", as_node, role=role, verdict=verdict,
                                            note=util.redact((note or "").strip()), caller=caller or "user",
                                            content=content_hash(issue)))
        tracker.owe(issue.header, project["pm"], "work-ahead-review:" + package)
        issue.save()
    return "work-ahead review recorded on " + package + "\n"
