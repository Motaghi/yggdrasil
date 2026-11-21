"""Package reviews and user rulings (A11 lane 2: AC-11 to AC-13, AC-29, AC-56 to AC-61, AC-65, AC-89, AC-90).

A ready package gets one review by the project's reviewer node (`<pm>rv1`, created by `ygg pid approve`). A review
records its base and head SHAs, the criteria version, the project's rulings version and its round. One reviewer
activation handles one review: a review is owed to the reviewer (a stamp on the package record) only while no
other review of that reviewer is assigned. The reviewer writes `verdict.json` into `$YGG_REVIEW_DIR` and exits 0;
the conductor's `reviews` step records it only for the review assigned to that activation, and only while the lane
head, the criteria and the rulings are those the review recorded (otherwise the review is stale). A revise sends
the package back with the findings; the cap (`review_rounds`, counted in revise verdicts) opens a user decision.

Reviews and rulings live in the tracker (`_ygg/reviews.json`, `_ygg/rulings.json`), so they survive an emptied
state root.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from . import config, state, tracker, util
from .util import YggError

REVIEW_RE = re.compile(r"^rv-[0-9]{4,9}$")
CAP_OPTIONS = ["accept as is", "one more round", "stop"]
VERDICT_LIMIT = 64 * 1024
FINDINGS_LIMIT = 50
OPEN_STATES = ("queued", "assigned")


# ---------------------------------------------------------------- stores

def _store_path(name: str) -> Path:
    return tracker.root() / "_ygg" / (name + ".json")


def _load(name: str) -> dict:
    """A store, read strictly: a missing file is empty; a malformed one is a corrupt tracker record (exit 8 and an
    alert), never silently empty, so review history, caps and rulings can't vanish (A7 I13)."""
    path = _store_path(name)
    if not path.exists():
        data = {}
    else:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise tracker.corrupt(path, str(error))
        kind = dict if name == "reviews" else list
        if not isinstance(data, dict) or not isinstance(data.get("counter", 0), int) or \
                not isinstance(data.get(name, kind()), kind) or \
                (name == "reviews" and not isinstance(data.get("packages", {}), dict)):
            raise tracker.corrupt(path, "expected {\"counter\": n, \"" + name + "\": ...}")
    data.setdefault("counter", 0)
    data.setdefault(name, {} if name == "reviews" else [])
    if name == "reviews":
        data.setdefault("packages", {})
    return data


def _save(name: str, data: dict) -> None:
    tracker.write_json(_store_path(name), data)


def reviews() -> dict:
    return _load("reviews")["reviews"]


def get(review_id: str) -> dict | None:
    return reviews().get(review_id)


def _package_key(project: str, package: str) -> str:
    return project + "/" + package


def package_reviews(project: str, package: str) -> list[dict]:
    return sorted((r for r in reviews().values() if r.get("project") == project and r.get("package") == package),
                  key=lambda r: r["id"])


def open_review(project: str, package: str) -> dict | None:
    found = [r for r in package_reviews(project, package) if r.get("state") in OPEN_STATES + ("held",)]
    return found[-1] if found else None


def package_info(project: str, package: str) -> dict:
    return dict(_load("reviews")["packages"].get(_package_key(project, package)) or {})


# ---------------------------------------------------------------- rulings

def rulings(project: str | None = None) -> list[dict]:
    found = _load("rulings")["rulings"]
    return [r for r in found if project is None or r.get("project") == project]


def rulings_version(project: str) -> int:
    return len(rulings(project))


def record_ruling(project_name: str, text: str) -> str:
    """`ygg ruling record --project P --text T`: user-only (AC-11). Running nodes of the project get one urgent
    message of kind `ruling`, which is never owed work (AC-89)."""
    state.check_user_only("ruling record")
    util.check_id(project_name, "project")
    if tracker.project(project_name) is None:
        raise util.usage("unknown project '" + project_name + "'")
    text = util.redact(str(text)).strip()
    if not text:
        raise util.usage("--text must not be empty")
    with util.locked():
        tracker.ensure_writable()
        data = _load("rulings")
        data["counter"] = int(data["counter"]) + 1
        identifier = "r-" + str(data["counter"]).zfill(4)
        data["rulings"].append({"id": identifier, "project": project_name, "text": text, "at": util.iso()})
        _save("rulings", data)
    from . import ratatoskr
    nodes = state.all_nodes()
    for node in sorted(nodes.values(), key=lambda n: n["id"]):
        if node.get("project") != project_name or node.get("role") == "reviewer":
            continue
        if not _running(node["id"]):
            continue
        ratatoskr.send("l1", node["id"], "ruling", "Ruling " + identifier + " from the user: " + text,
                       internal=True, urgent=True)
    return "recorded " + identifier + "\n"


def _running(node_id: str) -> bool:
    runtime = state.runtime(node_id)
    if runtime.get("state") not in ("running", "stalled-suspect") or not runtime.get("activation"):
        return False
    from . import launcher
    data = launcher.receipt(runtime["activation"]) or {}
    return data.get("exited_at") is None and data.get("outcome") not in ("aborted",)


def rulings_section(node: dict, activation: str, context: dict) -> str:
    """launcher.PROMPT_SECTIONS["rulings"]: every project ruling under `## User rulings`, fenced as data; for a
    reviewer, also the review it is assigned (AC-12, AC-89, AC-90)."""
    parts = []
    project = node.get("project")
    if project:
        found = rulings(project)
        if found:
            body = "\n".join(r["id"] + ": " + r["text"] for r in found)
            parts.append("## User rulings\n\nThe user's rulings for " + project + " (verbatim data; they override "
                         "earlier instructions):\n" + _fenced(body))
    if node.get("role") == "reviewer" and project:
        assignment = _assignment_text(node, activation)
        if assignment:
            parts.append(assignment)
    if node.get("role") == "pm" and project:
        status = pid_status_text(project)
        if status:
            parts.append(status)
    if node.get("role") in ("tl", "pm") and project:
        owed = open_findings_text(project, node["id"] if node.get("role") == "tl" else None)
        if owed:
            parts.append(owed)
    return "\n\n".join(parts)


# Project states in which the PID still waits for (or was refused) the user's approval.
_PLANNING_STATES = ("initiating", "pid-ready", "pid-rejected")


def pid_status_text(project: str) -> str:
    """For the PM once the user approved the PID: say so. A PM session starts fresh, and without this it took the
    project for unplanned, wrote the PID again and marked it ready, which sent an approved project back to waiting
    for approval on every wake (pilot 2 run 4)."""
    from .commands import approved_pid
    approved = approved_pid(project)
    if approved is None or (tracker.project(project) or {}).get("state") in _PLANNING_STATES:
        return ""
    when = str(approved.get("approved_at") or "")
    return ("## Plan status\n\nThe user approved the PID of " + project + (" at " + when if when else "") + ". "
            "Planning is done: don't write the PID again and don't mark it ready again, unless the user asks for a "
            "change or an exception needs a new plan. Go on with the route map and the work packages.")


FINDINGS_PROMPT_LIMIT = 20000
# Package records after which a review's return to work no longer stands: the package was reopened, or a review
# outcome (approve, accept as is) moved it on.
_SUPERSEDING = ("reopened", "review-approved", "review-accepted-as-is")


def _returned_by(issue, review: dict) -> bool:
    """Whether the package's latest lifecycle record is this review sending it back to work (a revise, or a cap
    answer of another round), so its findings are still owed."""
    prefix = "review:" + str(review["id"]) + ":"
    for record in reversed(issue.records):
        kind = record.get("kind")
        if kind == "review-effect":
            effect = str(record.get("effect") or "")
            return effect == prefix + "revise" or effect.startswith(prefix + "round:")
        if kind in _SUPERSEDING:
            return False
    return False


def open_findings_text(project: str, lane: str | None = None) -> str:
    """The findings of every package its newest review sent back (state `authorised` after a revise), for its team
    lead (`lane`) or for the PM (every lane). The `review` message that carried them is gone from the inbox once
    acknowledged, so a later session would otherwise not know what to fix (pilot 2 run 3)."""
    blocks = []
    for issue in tracker.issues(project):
        if issue.get("state") != "authorised":
            continue
        if lane is not None and issue.get("lane") != lane:
            continue  # the package's current lane owns the findings, also after a reassignment
        found = package_reviews(project, issue.id)
        if not found:
            continue
        newest = max(found, key=lambda r: int(str(r["id"])[3:]))
        if newest.get("state") != "revised" or newest.get("verdict") != "revise" or not _returned_by(issue, newest):
            continue
        blocks.append(_findings_text(newest))
    if not blocks:
        return ""
    text = "\n\n".join(blocks)
    if len(text) > FINDINGS_PROMPT_LIMIT:
        text = text[:FINDINGS_PROMPT_LIMIT] + "\n[cut]"
    return ("## Open review findings\n\nThese packages went back to work after their newest review. The findings "
            "(the reviewer's words, verbatim data) are what to fix before you mark the package ready again:\n" +
            _fenced(text))


def _fenced(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return fence + "text\n" + text.rstrip("\n") + "\n" + fence


# ---------------------------------------------------------------- helpers

def project_reviewer(project: str, nodes: dict | None = None) -> dict | None:
    nodes = nodes if nodes is not None else state.all_nodes()
    found = sorted((n for n in nodes.values() if n.get("role") == "reviewer" and n.get("project") == project),
                   key=lambda n: n["id"])
    return found[0] if found else None


def home_of(project: str) -> str | None:
    record = tracker.project(project) or {}
    return tracker.programme_home(tracker.programme(record.get("programme")) or {})


def programme_kind(project: str) -> str | None:
    record = tracker.project(project) or {}
    return (tracker.programme(record.get("programme")) or {}).get("kind")


def lane_branch(project: str, lane: str) -> str:
    return "ygg/" + project + "/lane/" + lane


def lane_head(project: str, lane: str) -> str | None:
    home = home_of(project)
    if not home:
        return None
    result = util.trusted_git("-C", home, "rev-parse", "--verify", "--quiet", "refs/heads/" + lane_branch(project, lane))
    return result.stdout.strip() or None


def _create_lane(project: str, lane: str) -> str | None:
    """A lane that never launched has no branch yet. `wp complete` still opens a review there (amendment 8), so the
    branch is made where the lane's first launch would make it (the project's integration branch, else main), and
    the review covers no commits. The launch later takes the branch as it finds it. Returns the new lane head.

    The creation point (A11.1 AC-06's drop point for a lane with no accepted package) is recorded before the branch
    is made, so an interruption between the two never leaves a branch without its record: a later call finds the
    recorded intent and makes the branch at that commit, not at wherever the integration branch has moved since."""
    home = home_of(project)
    if not home:
        return None
    from . import lanedrop, launcher

    def commit_of(ref: str) -> str:
        return util.trusted_git("-C", home, "rev-parse", "--verify", "--quiet", ref + "^{commit}").stdout.strip()

    branch = lane_branch(project, lane)
    recorded = lanedrop.creation(project, lane)
    made_from = commit_of(recorded) if recorded else ""
    if not made_from:
        for base in ("ygg/" + project + "/_integration", "main"):
            made_from = commit_of("refs/heads/" + base)
            if made_from:
                break
        if not made_from:
            return None
        launcher._record_lane_creation(branch, made_from)  # the intent, durable before the branch exists
        made_from = commit_of(lanedrop.creation(project, lane) or made_from) or made_from
    if util.trusted_git("-C", home, "branch", branch, made_from).returncode != 0:
        return lane_head(project, lane)
    return made_from


def merge_base(project: str, head: str) -> str | None:
    home = home_of(project)
    if not home or not head:
        return None
    result = util.trusted_git("-C", home, "merge-base", head, "refs/heads/ygg/" + project + "/_integration")
    return result.stdout.strip() or None


def criteria_version(issue) -> str:
    records = [r for r in issue.records if r.get("kind") == "criteria"]
    text = json.dumps([[r.get("node"), r.get("body"), r.get("at")] for r in records], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def criteria_text(issue) -> str:
    records = [r for r in issue.records if r.get("kind") == "criteria"]
    return "\n\n".join(str(r.get("body") or "") for r in records)


def review_rounds(project: str, package: str) -> int:
    layered = config.explain_all(project, package).get("review_rounds")
    return int(layered[0] if layered else config.get("review_rounds", 5))


def _issue(project: str, package: str):
    issue = tracker.read_issue(project, package)
    if issue is None or issue.get("kind") != "work-package":
        raise util.usage("unknown work package '" + str(package) + "'")
    return issue


def cap_decision_open(project: str, package: str) -> str | None:
    info = package_info(project, package)
    decision = info.get("cap_decision")
    if not decision:
        return None
    from . import decisions
    issue = tracker.read_issue(project, decision)
    return decision if issue is not None and decisions.is_open(issue) else None


# ---------------------------------------------------------------- ready

def ready(from_node: str, package: str, *, complete: bool = False) -> str:
    """`ygg wp ready --from TL --package P` (and `wp complete` when the project has a reviewer node)."""
    nodes = state.all_nodes()
    state.check_identity(from_node, nodes)
    node = state.require_node(from_node, nodes)
    project = node.get("project")
    if not project:
        raise util.refuse("writer: " + from_node + " has no project")
    try:
        _apply_cap_answers()  # an answered cap decision takes effect before ready reads the state
    except YggError:
        pass
    issue = _issue(project, package)
    if issue.get("lane") != from_node:
        raise util.refuse("writer: only the lane team lead marks " + package + " ready")
    if programme_kind(project) == "vault-docs":
        raise util.refuse("review of document folders is not built yet; use ygg wp complete")
    current = open_review(project, package)
    if current is not None and current.get("state") in OPEN_STATES:
        return "review " + current["id"] + " of " + package + " is already open\n"
    if cap_decision_open(project, package):
        raise util.refuse(package + " waits for the user's decision on its review cap")
    if issue.get("state") == "review-exhausted":
        raise util.refuse("review-exhausted: " + package)
    if issue.get("state") not in ("authorised", "rejected"):
        raise util.refuse("gate: " + package + " is " + str(issue.get("state")) + " and cannot be marked ready")
    reviewer = project_reviewer(project, nodes)
    if reviewer is None:
        raise util.refuse("gate: project " + project + " has no reviewer node")
    head = lane_head(project, from_node) or _create_lane(project, from_node)
    if not head:
        raise util.refuse("gate: lane " + from_node + " has no branch to review")
    base = merge_base(project, head) or head
    _trusted_recheck(project, package, from_node, base, head)
    with util.locked():
        tracker.ensure_writable()
        issue = _issue(project, package)
        # Repeated under the lock, so two concurrent readies open one review (A11 AC-12 row 2).
        current = open_review(project, package)
        if current is not None and current.get("state") in OPEN_STATES:
            return "review " + current["id"] + " of " + package + " is already open\n"
        if cap_decision_open(project, package):
            raise util.refuse(package + " waits for the user's decision on its review cap")
        if issue.get("state") not in ("authorised", "rejected"):
            raise util.refuse("gate: " + package + " is " + str(issue.get("state")) + " and cannot be marked ready")
        data = _load("reviews")
        data["counter"] = int(data["counter"]) + 1
        identifier = "rv-" + str(data["counter"]).zfill(4)
        rounds = len([r for r in data["reviews"].values()
                      if r.get("project") == project and r.get("package") == package and r.get("state") != "stale"])
        data["reviews"][identifier] = {
            "id": identifier, "project": project, "package": package, "lane": from_node,
            "reviewer": reviewer["id"], "base": base, "head": head, "criteria": criteria_version(issue),
            "rulings": rulings_version(project), "round": rounds + 1, "state": "queued", "no_verdict": 0,
            "launches": 0, "stamp": None, "seen": [], "opened_at": util.iso(), "family": None,
            # The opening intent: durable with the review, cleared once the package is in review. A crash in between
            # is finished by recovery (`finish_openings`) before the reviewer is assigned (A7 crash recovery, AC-13).
            # `records_at` is the package's durable order at the intent: the number of its records then, so a reopen or
            # cancel recorded afterwards is told apart without comparing whole-second timestamps.
            "opening": {"by": from_node, "via": "complete" if complete else "ready", "records_at": len(issue.records)},
        }
        _save("reviews", data)
    finish_openings()
    assign()
    return "review " + identifier + " of " + package + " at " + head[:7] + "\n"


def _trusted_recheck(project: str, package: str, lane: str, base: str, head: str) -> None:
    """D5: `ygg wp ready` re-runs the lane check with Yggdrasil's own git (lane 1's module, when present)."""
    try:
        from . import lanecheck
    except ImportError:
        return
    check = getattr(lanecheck, "recheck_package", None)  # lane 1 (AC-93 row 3); refuses with its own text
    if check is None:
        return
    check(project, package)


def _moved_on(issue, opening: dict) -> bool:
    """A reopen or cancel recorded on the package after the opening intent (by record position, not time)."""
    start = int(opening.get("records_at") or 0)
    return any(r.get("kind") in ("reopened", "cancelled", "test-cancelled") for r in issue.records[start:])


def finish_openings() -> None:
    """Finish every review opening a crash interrupted: the package enters review with its review-opened record,
    unless the package was reopened or cancelled since, moved to another state, or a newer review owns it; then the
    review is abandoned and never assigned. A package that already holds this review's opened record is revalidated the
    same way (still in review, still this review's, nothing recorded after the intent), and only then is the intent
    cleared."""
    with util.locked():
        data = _load("reviews")
        changed = False
        for review in sorted(data["reviews"].values(), key=lambda r: r["id"]):
            opening = review.get("opening")
            if not opening:
                continue
            issue = tracker.read_issue(review["project"], review["package"])
            if issue is None or _moved_on(issue, opening) or not _owns(review):
                review["state"] = "abandoned"
            else:
                opened = any(r.get("kind") == "review-opened" and r.get("review") == review["id"]
                             for r in issue.records)
                if opened and issue.get("state") == "in-review":
                    pass  # the opening finished; only the intent is left to clear
                elif not opened and issue.get("state") in ("authorised", "rejected"):
                    tracker.ensure_writable()
                    issue.header["state"] = "in-review"
                    issue.records.append(tracker.record("review-opened", opening.get("by"), review=review["id"],
                                                        base=review.get("base"), head=review.get("head"),
                                                        via=opening.get("via") or "ready"))
                    issue.save()
                else:
                    review["state"] = "abandoned"  # suspended, stopped or otherwise moved on since the opening
            review.pop("opening", None)
            changed = True
        if changed:
            _save("reviews", data)


def _assignable(review: dict) -> bool:
    """A queued review may be owed to its reviewer only while its package is in review and this review owns it."""
    issue = tracker.read_issue(review["project"], review["package"])
    return issue is not None and issue.get("state") == "in-review" and _owns(review)


def assign() -> None:
    """Owe each idle reviewer its oldest queued review: one review per reviewer activation. Interrupted openings are
    finished first, so no reviewer is assigned to a package that never entered review."""
    finish_openings()
    with util.locked():
        data = _load("reviews")
        busy = {r["reviewer"] for r in data["reviews"].values() if r.get("state") == "assigned"}
        changed = False
        for review in sorted(data["reviews"].values(), key=lambda r: r["id"]):
            if review.get("state") != "queued" or review["reviewer"] in busy:
                continue
            if review.get("opening") or not _assignable(review):
                # Revalidated under the assignment lock: a package suspended, reopened or cancelled since its review
                # opened is never stamped; the review is abandoned (an unfinished opening waits for finish_openings).
                if not review.get("opening"):
                    review["state"] = "abandoned"
                    changed = True
                continue
            issue = tracker.read_issue(review["project"], review["package"])
            tracker.ensure_writable()
            tracker.owe(issue.header, review["reviewer"], "review:" + review["id"])
            issue.header["owed"][-1]["review"] = review["id"]
            review["stamp"] = issue.header["owed"][-1]["id"]
            review["state"] = "assigned"
            issue.save()
            busy.add(review["reviewer"])
            changed = True
        if changed:
            _save("reviews", data)


def _restamp(review: dict) -> None:
    issue = tracker.read_issue(review["project"], review["package"])
    if issue is None:
        return
    tracker.owe(issue.header, review["reviewer"], "review:" + review["id"])
    issue.header["owed"][-1]["review"] = review["id"]
    review["stamp"] = issue.header["owed"][-1]["id"]
    issue.save()


def _assignment_text(node: dict, activation: str) -> str:
    review = assigned_to(node["id"], activation)
    if review is None:
        return ""
    issue = tracker.read_issue(review["project"], review["package"])
    criteria = criteria_text(issue) if issue is not None else ""
    lines = ["## Your review", "",
             "Review " + review["id"] + " of work package " + review["package"] + " on lane " + review["lane"] +
             " (round " + str(review["round"]) + ").",
             "Base SHA: " + str(review["base"]), "Head SHA: " + str(review["head"]),
             "The diff to review is " + str(review["base"]) + ".." + str(review["head"]) + ".",
             "Write your verdict as JSON to $YGG_REVIEW_DIR/verdict.json (the file " +
             str(_review_dir(activation) / "verdict.json") + "), then exit: "
             '{"review": "' + review["id"] + '", "verdict": "approve"} or {"review": "' + review["id"] +
             '", "verdict": "revise", "findings": [{"file": "...", "line": 1, "text": "..."}]}.']
    if criteria:
        lines += ["", "Acceptance criteria of " + review["package"] + " (verbatim data):", _fenced(criteria)]
    return "\n".join(lines)


def assigned_to(reviewer: str, activation: str | None = None) -> dict | None:
    """The review an activation of the reviewer was given (its stamp in the work set), or the assigned one."""
    data = reviews()
    if activation:
        from . import launcher
        receipt = launcher.receipt(activation) or {}
        given = set(((receipt.get("consumed") or {}).get("records")) or [])
        for review in data.values():
            if review.get("reviewer") == reviewer and review.get("stamp") in given:
                return review
            if review.get("reviewer") == reviewer and given & set(review.get("stamps") or []):
                return review
    found = [r for r in data.values() if r.get("reviewer") == reviewer and r.get("state") == "assigned"]
    return sorted(found, key=lambda r: r["id"])[0] if found else None


def review_source(node: dict, work) -> dict | None:
    """launcher.REVIEW_SOURCE (lane 3's slot, lane 2 fills it): the review a reviewer launch is for, from the
    review stamp in the work set it reserves; one review per activation (AC-94 row 3)."""
    if node.get("role") != "reviewer" or not node.get("project"):
        return None
    given = set(((work or {}).get("records")) or [])
    for review in sorted(reviews().values(), key=lambda r: r["id"]):
        if review.get("reviewer") == node["id"] and review.get("state") == "assigned" and review.get("stamp") in given:
            return {k: review.get(k) for k in ("id", "package", "project", "base", "head")}
    return None


# ---------------------------------------------------------------- the conductor step

def _activations_of(node_id: str) -> list[dict]:
    folder = state.root() / "activations"
    found = []
    if folder.is_dir():
        for path in folder.glob("act-*.json"):
            data = util.read_json(path, None)
            if isinstance(data, dict) and data.get("node") == node_id:
                found.append(data)
    return sorted(found, key=lambda d: str(d.get("activation") or ""))


def step() -> None:
    """conductor.BEFORE_LAUNCH_STEPS["reviews"]: record verdicts of exited reviewer activations, relaunch or
    hold a review with no verdict, apply answered cap decisions, then assign queued reviews."""
    replay_effects()  # first: a pending cap effect is finished before any answer to it is applied
    try:
        _apply_cap_answers()
    except YggError:
        pass
    data = _load("reviews")
    for review_id in sorted(data["reviews"]):
        review = data["reviews"][review_id]
        if review.get("state") != "assigned" or not review.get("stamp"):
            continue
        for activation in _activations_of(review["reviewer"]):
            given = set(((activation.get("consumed") or {}).get("records")) or [])
            if review["stamp"] not in given or activation["activation"] in (review.get("seen") or []):
                continue
            if activation.get("exited_at") is None:
                continue
            _settle(review_id, activation)
            break
    assign()


def _settle(review_id: str, activation: dict) -> None:
    with util.locked():
        data = _load("reviews")
        review = data["reviews"][review_id]
        if review.get("state") != "assigned":
            return
        review.setdefault("seen", []).append(activation["activation"])
        review.setdefault("stamps", []).append(review.get("stamp"))
        review["launches"] = int(review.get("launches") or 0) + 1
        if activation.get("cli") == "codex":
            review["family"] = "other"
        elif review.get("family") != "fallback":
            review["family"] = "same"
        code = activation.get("exit_code")
        from . import launcher
        klass = launcher.classify(activation) if activation.get("exited_at") else None
        if klass == "capacity" or activation.get("interrupted") or activation.get("outcome") == "aborted" or \
                activation.get("phase") == "aborted":
            _save("reviews", data)
            return  # the stamp stays owed: the capacity backoff or the stop decides the next launch
        verdict = _read_verdict(activation) if code == 0 else None
        if code not in (0, None) and activation.get("cli") == "codex" and not review.get("fallback"):
            # A failed Codex reviewer launch falls back to the builder's family once (AC-56 row 2).
            review["family"] = "fallback"
            review["fallback"] = True
            _restamp(review)
            _save("reviews", data)
            current = state.runtime(review["reviewer"])
            if current.get("activation") == activation["activation"]:
                state.set_state(review["reviewer"], "idle")  # the failed reviewer is done; the fallback may start
            state.alert("ygg-review-fallback", review["package"] + " review " + review_id + " falls back to the "
                        "same model family: the Codex reviewer was unavailable, so this review runs on the Claude "
                        "model", review=review_id,
                        project=review["project"], package=review["package"])
            return
        if verdict is None or verdict.get("review") != review_id:
            review["no_verdict"] = int(review.get("no_verdict") or 0) + 1
            if review["no_verdict"] >= 2:
                review["state"] = "held"
                _save("reviews", data)
                state.alert("ygg-review-held", review["package"] + " review held: the reviewer gave no verdict",
                            review=review_id, project=review["project"], package=review["package"])
                return
            _restamp(review)
            _save("reviews", data)
            return
        stale = _stale_reason(review)
        if stale:
            review["state"] = "stale"
            review["stale_text"] = ("review " + review_id + " is stale " + stale + "; mark " + review["package"] +
                                    " ready again when it is.")
        elif verdict["verdict"] == "approve":
            review["state"] = "approved"
            review["verdict"] = "approve"
        else:
            review["state"] = "revised"
            review["verdict"] = "revise"
            review["findings"] = verdict.get("findings") or []
            key = _package_key(review["project"], review["package"])
            info = data["packages"].setdefault(key, {})
            info["revises"] = int(info.get("revises") or 0) + 1
            allowed = int(info.get("allowed") or review_rounds(review["project"], review["package"]))
            info["allowed"] = allowed
            review["capped"] = info["revises"] >= allowed
        # The result and its pending effect are one write; the effect is replayed until it is done (A7 I4), so a
        # crash between this write and the package change can't strand the package.
        review["effect"] = "pending"
        _save("reviews", data)
    apply_effect(review_id)


def replay_effects() -> None:
    """Finish every review effect a crash left pending (each step is idempotent)."""
    for review_id, review in sorted(reviews().items()):
        if review.get("effect") == "pending":
            apply_effect(review_id)


def apply_effect(review_id: str) -> None:
    finish_openings()  # an approval never meets a package whose opening was interrupted
    review = get(review_id)
    if review is None or review.get("effect") != "pending":
        return
    kind = review.get("state")
    note = None
    if kind == "approved":
        _to_testing(review)
    elif kind == "revised" and review.get("capped"):
        if not _open_cap(review):
            note = "superseded"
    elif kind == "revised":
        _back_to_work(review, _findings_text(review), tag="revise")
    elif kind == "stale":
        _back_to_work(review, str(review.get("stale_text") or ("review " + review_id + " is stale")), tag="stale")
    elif kind == "dropped":
        _back_to_work(review, "review dropped by the user (" + review_id + " of " + review["package"] + "); mark it "
                      "ready again when it is.", tag="dropped")
    with util.locked():
        data = _load("reviews")
        entry = data["reviews"].get(review_id)
        if entry is not None and entry.get("effect") == "pending":
            entry["effect"] = "done"
            if note:
                entry["effect_note"] = note
            _save("reviews", data)


def _review_dir(activation: str) -> Path:
    try:
        from . import permissions
        found = getattr(permissions, "review_dir", None)
        if found is not None:
            return Path(found(activation))
    except ImportError:
        pass
    return state.root() / "scratch" / activation


def _read_verdict(activation: dict) -> dict | None:
    path = _review_dir(str(activation.get("activation"))) / "verdict.json"
    try:
        if path.stat().st_size > VERDICT_LIMIT:
            return None
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("verdict") not in ("approve", "revise"):
        return None
    if not isinstance(data.get("review"), str):
        return None
    findings = data.get("findings")
    if findings is not None:
        if not isinstance(findings, list) or len(findings) > FINDINGS_LIMIT:
            return None
        cleaned = []
        for item in findings:
            if not isinstance(item, dict):
                return None
            cleaned.append({"file": str(item.get("file") or ""), "line": item.get("line"),
                            "text": util.redact(str(item.get("text") or ""))})
        data["findings"] = cleaned
    return data


def _stale_reason(review: dict) -> str | None:
    if lane_head(review["project"], review["lane"]) != review["head"]:
        return "(the lane moved)"
    issue = tracker.read_issue(review["project"], review["package"])
    if issue is not None and criteria_version(issue) != review["criteria"]:
        return "(the criteria changed)"
    if rulings_version(review["project"]) != review["rulings"]:
        return "(a ruling changed)"
    return None


def _findings_text(review: dict) -> str:
    # The package is named, so the message is tied to it (a cancel settles it with the package, A11.1 AC-06).
    lines = ["review " + review["id"] + " of " + review["package"] + " (round " + str(review["round"]) + "): revise."]
    for item in review.get("findings") or []:
        lines.append("- " + str(item.get("file")) + (":" + str(item.get("line")) if item.get("line") else "") +
                     " " + str(item.get("text")))
    return "\n".join(lines)


def _pm_of(project: str) -> str | None:
    return (tracker.project(project) or {}).get("pm")


def _owns(review: dict) -> bool:
    """Whether the review is still its package's current (newest) review: an older review's effect never acts on the
    package once a newer review exists (A11 AC-59, A7 I6)."""
    found = package_reviews(review["project"], review["package"])
    return bool(found) and max(found, key=lambda r: int(str(r["id"])[3:]))["id"] == review["id"]


def _effect_done(issue, key: str) -> bool:
    return any(r.get("kind") == "review-effect" and r.get("effect") == key for r in issue.records)


def _mark_effect(issue, review: dict, key: str) -> None:
    issue.records.append(tracker.record("review-effect", None, effect=key, review=review["id"]))


def _back_to_work(review: dict, body: str, *, tag: str) -> None:
    """The package goes back to `authorised`, owed to its lane through a `review` message. The effect's identity is
    saved in the same package write as the transition, so a replay skips an applied effect whatever happened since;
    an effect acts only while its review is the package's current one and the package is in review. The message is
    sent once per effect (its dispatch key), so a crash between it and the package write repeats nothing."""
    from . import ratatoskr
    key = "review:" + review["id"] + ":" + tag
    with util.locked():
        tracker.ensure_writable()
        issue = tracker.read_issue(review["project"], review["package"])
        if issue is None or _effect_done(issue, key) or not _owns(review) or issue.get("state") != "in-review":
            return
        ratatoskr.send(_pm_of(review["project"]) or "l1", review["lane"], "review", body, internal=True,
                       dispatch=key)
        issue = tracker.read_issue(review["project"], review["package"])
        issue.header["state"] = "authorised"
        _mark_effect(issue, review, key)
        issue.save()


def _to_testing(review: dict, *, accepted_as_is: bool = False, key: str | None = None) -> None:
    from .commands import paired_seniors
    nodes = state.all_nodes()
    key = key or "review:" + review["id"] + ":approve"
    with util.locked():
        tracker.ensure_writable()
        issue = tracker.read_issue(review["project"], review["package"])
        if issue is None or _effect_done(issue, key) or not _owns(review) or issue.get("state") != "in-review":
            # Applied already, superseded by a newer review, or the package moved on since the verdict (a senior
            # user's rejection, a cancel, a stop, a reopen): a replay never acts over a later transition (A7 I4, I6).
            return
        issue.header["state"] = "ready-for-test"
        issue.header["reviewed_head"] = review["head"]
        issue.records.append(tracker.record("review-approved" if not accepted_as_is else "review-accepted-as-is",
                                            None, review=review["id"], head=review["head"]))
        _mark_effect(issue, review, key)
        tracker.owe(issue.header, _pm_of(review["project"]), "wp-complete:" + review["package"])
        for su in paired_seniors(issue.get("lane"), nodes):
            tracker.owe(issue.header, su, "ready-for-test:" + review["package"])
        issue.save()


def _open_cap(review: dict) -> bool:
    """The review-cap decision, opened once per review. The decision id and its review are recorded before the
    decision is written, so a replay after a crash at any point reuses that id and never opens a second decision
    (A7 I6, AC-13 row 3)."""
    project = tracker.project(review["project"]) or {}
    key = _package_key(review["project"], review["package"])
    created = False
    with util.locked():
        tracker.ensure_writable()
        current = tracker.read_issue(review["project"], review["package"])
        if current is None or not _owns(review) or current.get("state") != "in-review":
            # Superseded (a newer review owns the package, or it moved on since the verdict: suspended, reopened,
            # cancelled): retired without touching the package's work, its cap metadata or its messages (A7 I4, I6).
            return False
        info = package_info(review["project"], review["package"])
        if info.get("cap_review") == review["id"] and info.get("cap_decision"):
            identifier = info["cap_decision"]
        else:
            identifier = tracker.allocate("decision")
            data = _load("reviews")
            entry = data["packages"].setdefault(key, {})
            entry["cap_decision"] = identifier
            entry["cap_review"] = review["id"]
            entry["cap_revises"] = int(entry.get("revises") or 0)
            _save("reviews", data)
        if tracker.read_issue(review["project"], identifier) is None:
            issue = tracker.new_issue(
                review["project"], identifier, "decision", ["decision", "review-cap"], None,
                question=review["package"] + " reached its review cap after " + str(_revises(review)) +
                " revise verdicts. Last findings: " + "; ".join(str(f.get("text")) for f in review.get("findings") or []),
                options=list(CAP_OPTIONS), recommended="one more round", chosen=None, chosen_by=None,
                reversible=False, undo=None, aligns=False, scope={"type": "review-cap", "id": review["package"]},
                action=None, target=None, review=review["id"], package=review["package"],
                provenance={"type": "agent", "origin": project.get("pm")})
            issue.save()
            created = True
        decision = tracker.read_issue(review["project"], identifier)
        from . import decisions
        answered = decision is not None and not decisions.is_open(decision)
        package_issue = tracker.read_issue(review["project"], review["package"])
        if package_issue is not None and not answered:
            # Withdrawn only while the decision waits: once answered (say, one more round), the answer owns the
            # package's work, and a replayed cap effect must not take the new findings back (A7 I4, AC-60 row 2).
            from .commands import _withdraw
            _withdraw(package_issue, [review["lane"]])
            package_issue.save()
    if created:
        state.alert("ygg-decision-waiting", review["package"] + " reached its review cap; decision " + identifier +
                    " waits for you", decision=identifier, project=review["project"], package=review["package"])
    return True


def _revises(review: dict) -> int:
    return int(package_info(review["project"], review["package"]).get("revises") or 0)


def apply_cap_answers() -> None:
    _apply_cap_answers()


def _apply_cap_answers() -> None:
    from . import decisions
    data = _load("reviews")
    for key, info in sorted(data["packages"].items()):
        decision = info.get("cap_decision")
        if not decision or info.get("cap_applied") == decision:
            continue
        project, _, package = key.partition("/")
        issue = tracker.read_issue(project, decision)
        if issue is None or decisions.is_open(issue):
            continue
        answer = issue.get("chosen")
        review = data["reviews"].get(info.get("cap_review") or "") or {}
        # Each answer's effect is idempotent and marked applied only after it is durable (A7 I4).
        if answer == "accept as is":
            _to_testing(review, accepted_as_is=True, key="cap:" + decision)
        elif answer == "one more round":
            with util.locked():
                fresh = _load("reviews")
                entry = fresh["packages"].setdefault(key, {})
                entry["allowed"] = int(entry.get("cap_revises", entry.get("revises")) or 0) + 1
                _save("reviews", fresh)
            _back_to_work(review, _findings_text(review), tag="round:" + decision)
        elif answer == "stop":
            with util.locked():
                tracker.ensure_writable()
                package_issue = tracker.read_issue(project, package)
                if package_issue is not None and package_issue.get("state") not in ("stopped", "cancelled"):
                    package_issue.header["state"] = "stopped"
                    from .commands import _withdraw
                    _withdraw(package_issue, [package_issue.get("lane"), review.get("reviewer")])
                    package_issue.save()
            from . import ratatoskr
            stage = (package_issue.get("stage") if package_issue is not None else None) or _stage_number(project)
            ratatoskr.send("l1", _pm_of(project), "decision",
                           package + " stopped by the user; re-plan stage " + str(stage) + ".", internal=True,
                           dispatch="cap:" + decision)
        with util.locked():
            fresh = _load("reviews")
            fresh["packages"].setdefault(key, {})["cap_applied"] = decision
            _save("reviews", fresh)


def _stage_number(project: str) -> int:
    from .commands import current_stage
    try:
        stage = current_stage(project)
        return int(stage["number"]) if stage else 1
    except (YggError, KeyError, TypeError, ValueError):
        return 1


# ---------------------------------------------------------------- user verbs

def retry(review_id: str) -> str:
    state.check_user_only("review retry")
    if not REVIEW_RE.match(str(review_id)):
        raise util.usage("invalid review id '" + str(review_id) + "'")
    with util.locked():
        tracker.ensure_writable()
        data = _load("reviews")
        review = data["reviews"].get(review_id)
        if review is None:
            raise util.usage("unknown review '" + review_id + "'")
        if review.get("state") != "held":
            raise util.refuse("gate: review " + review_id + " is " + str(review.get("state")) + ", not held")
        review["state"] = "assigned"
        review["no_verdict"] = 0
        _restamp(review)
        _save("reviews", data)
    return "review " + review_id + " retried\n"


def drop(review_id: str) -> str:
    state.check_user_only("review drop")
    if not REVIEW_RE.match(str(review_id)):
        raise util.usage("invalid review id '" + str(review_id) + "'")
    with util.locked():
        tracker.ensure_writable()
        data = _load("reviews")
        review = data["reviews"].get(review_id)
        if review is None:
            raise util.usage("unknown review '" + review_id + "'")
        if review.get("state") not in OPEN_STATES + ("held",):
            raise util.refuse("gate: review " + review_id + " is " + str(review.get("state")))
        review["state"] = "dropped"
        review["effect"] = "pending"
        _save("reviews", data)
    apply_effect(review_id)
    assign()
    return "review " + review_id + " dropped\n"


# ---------------------------------------------------------------- checks other verbs make

def check_review_record(issue) -> None:
    """Amendment 24: the team lead's pair-review record is refused while the package is in review."""
    current = open_review(issue.project, issue.id)
    if issue.get("state") == "in-review" and current is not None:
        raise util.refuse(issue.id + " is being checked by " + current["id"])


def check_acceptance_sha(issue, sha: str) -> None:
    """AC-13 row 2: an acceptance names the reviewed head."""
    reviewed = issue.get("reviewed_head")
    if reviewed and str(sha) != str(reviewed):
        raise util.refuse("acceptance must name the reviewed head")


# ---------------------------------------------------------------- the reviewer's model (AC-12, AC-56)

def reviewer_choice(node: dict) -> tuple[str, str, str] | None:
    """(model, effort, cli) for a project reviewer: `models.review_model` when set, else the other family from the
    builder (`pair_model`, on Codex when the `codex` executable resolves), else the builder's own family."""
    if node.get("role") != "reviewer" or not node.get("project"):
        return None
    from . import models
    merged = models.resolve_node(dict(node, role="tl"))
    review_model = _review_model(node)
    pair, pair_effort = merged.get("pair_model"), merged.get("pair_effort") or merged.get("effort")
    model, effort = merged.get("model"), merged.get("effort")
    current = assigned_to(node["id"])
    fallback = bool(current and current.get("fallback"))
    if review_model:
        chosen = str(review_model)
        chosen_effort = str(pair_effort if chosen.startswith("gpt") else effort)
    elif pair and str(pair).startswith("gpt") and _codex_available() and not fallback:
        chosen, chosen_effort = str(pair), str(pair_effort)
    else:
        chosen, chosen_effort = str(model), str(effort)
    if chosen_effort not in ("low", "medium", "high", "xhigh"):
        chosen_effort = "high"
    return chosen, chosen_effort, "codex" if chosen.startswith("gpt") else "claude-code"


def _review_model(node: dict):
    from .commands import approved_pid
    approved = approved_pid(node["project"]) or {}
    found = ((approved.get("guarded") or {}).get("models") or {}).get("review_model")
    if found:
        return found
    value = config.get("models")
    return value.get("review_model") if isinstance(value, dict) else None


def _codex_available() -> bool:
    import shutil
    binary = util.env("YGG_CODEX_BIN", "codex")
    if os.path.sep in str(binary):
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    return shutil.which(str(binary)) is not None


def launch_env(data: dict) -> dict:
    """YGG_REVIEW_DIR for a project reviewer: its `<state root>/scratch/<activation>` folder."""
    if data.get("role") != "reviewer" or not data.get("project"):
        return {}
    folder = state.root() / "scratch" / str(data["activation"])
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return {"YGG_REVIEW_DIR": str(folder)}


# ---------------------------------------------------------------- status

def package_line(project: str, package: str, package_state: str) -> str | None:
    found = package_reviews(project, package)
    info = package_info(project, package)
    if not found and not info:
        return None
    last = found[-1] if found else {}
    words = [package + ": " + package_state]
    if last:
        if last.get("state") in ("queued", "assigned"):
            words.append("review round " + str(last["round"]) + " open (" + last["id"] + ")")
        elif last.get("state") == "revised":
            words.append("review round " + str(last["round"]) + ", revise")
        elif last.get("state") == "approved":
            words.append("review round " + str(last["round"]) + ", approved")
        elif last.get("state") == "held":
            words.append("review " + last["id"] + " held")
        elif last.get("state") == "stale":
            words.append("review " + last["id"] + " stale")
        elif last.get("state") == "dropped":
            words.append("review " + last["id"] + " dropped")
    words.append(str(int(info.get("revises") or 0)) + " revise of " +
                 str(int(info.get("allowed") or review_rounds(project, package))))
    family = last.get("family") if last else None
    if family is None and last and last.get("state") in OPEN_STATES:
        family = _expected_family(last)
    if family == "fallback":
        words.append("review: same family (the hub's Codex pool is unavailable)")
    elif family == "same":
        words.append("review: same family")
    return ", ".join(words)


def _expected_family(review: dict) -> str | None:
    node = state.get_node(review["reviewer"])
    if not node:
        return None
    try:
        choice = reviewer_choice(node)
    except YggError:
        return None
    return None if choice is None else ("other" if choice[2] == "codex" else "same")


def held_reason(project: str, package: str) -> str | None:
    return "decision" if cap_decision_open(project, package) else None


def status_lines(status: dict) -> list[str]:
    lines = []
    for package in status.get("work_packages") or []:
        line = package_line(package["project"], package["id"], package["state"])
        if line:
            lines.append(line)
        current = open_review(package["project"], package["id"])
        if current is not None and current.get("state") == "held":
            lines.append(package["id"] + " review held: the reviewer gave no verdict")
    visible = {p["id"] for p in status.get("projects") or []}
    for ruling in rulings():
        if ruling.get("project") in visible:
            lines.append("Ruling " + ruling["id"] + ": " + ruling["text"])
    return lines
