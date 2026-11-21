"""Autonomy envelopes and morning-go items."""

from __future__ import annotations

from . import tracker, util


def active_envelope(project: dict) -> dict | None:
    found = [e for e in _envelopes(tracker.project_dir(project["id"]) / "envelopes") if e.get("state") == "active"]
    return found[-1] if found else None


def covers(project: dict, stage_number: int) -> bool:
    envelope = effective_envelope(project)
    if not envelope:
        return False
    if envelope.get("until"):
        return util.now() < util.parse_time(envelope["until"])
    if envelope.get("through_stage") is not None:
        return stage_number < int(envelope["through_stage"])
    return False


def _git(repo, *args):
    return util.trusted_git("-C", repo, *args)


def commit_ranges(project: dict, envelope: dict | None = None) -> tuple[list[dict], list[str]]:
    """Ranges of every snapshot branch that moved, plus fail-closed flags. `envelope` pins the snapshot
    (a work-ahead item keeps the one that covered its launch); otherwise the effective or last envelope."""
    envelope = envelope or effective_envelope(project) or last_envelope(project) or {}
    ranges, flags = [], []
    integration = None
    seen = set()
    for item in envelope.get("ref_snapshot") or []:
        seen.add((item["repo"], item["branch"]))
        result = _git(item["repo"], "rev-parse", "--verify", "--quiet", "refs/heads/" + item["branch"])
        head = result.stdout.strip()
        if item["branch"].endswith("/_integration"):
            integration = item
        if result.returncode != 0 or not head:
            flags.append("deleted-or-unreadable: " + item["branch"] + " in " + item["repo"])
            continue
        if head != item["sha"]:
            ancestor = _git(item["repo"], "merge-base", "--is-ancestor", item["sha"], head)
            if ancestor.returncode != 0:
                flags.append("non-fast-forward: " + item["branch"] + " in " + item["repo"])
            ranges.append({"repo": item["repo"], "branch": item["branch"], "range": item["sha"] + ".." + head})
    for repo in sorted({item["repo"] for item in envelope.get("ref_snapshot") or []}):
        listing = _git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)",
                       "refs/heads/ygg/" + project["id"])
        for line in listing.stdout.splitlines():
            branch, sha = line.split()
            if (repo, branch) not in seen:
                flags.append("created-since-snapshot: " + branch + " in " + repo)
                ranges.append({"repo": repo, "branch": branch, "range": "0" * 40 + ".." + sha})
    if not ranges and integration and not flags:
        ranges.append({"repo": integration["repo"], "branch": integration["branch"],
                       "range": integration["sha"] + ".." + integration["sha"]})
    if not envelope.get("ref_snapshot"):
        flags.append("no-ref-snapshot")
    return ranges, flags


def morning_go_item(project: dict, kind: str, ref: str, *, ranges: bool = True, **extra) -> str:
    """Allocate a morning-go item. `ranges=False` defers the commit ranges to `ygg morning-go build`
    (work-ahead items: the work happens after the launch that allocates them)."""
    identifier = tracker.allocate("morning-go")
    data = {"id": identifier, "programme": project["programme"], "project": project["id"], "kind": kind,
            "ref": ref, "status": "pending-user", "commit_ranges": [], "created_at": util.iso(),
            "decided_at": None, "decided_by": None, "standard_version": util.STANDARD_VERSION,
            "actor": "ygg-app"}
    data.update(extra)
    if ranges:
        data["commit_ranges"], data["flags"] = commit_ranges(project)
    else:
        data["flags"] = []
    tracker.write_json(tracker.root() / "initiatives" / project["programme"] / "morning-go" /
                       (identifier + ".json"), data)
    return identifier


def _envelopes(base) -> list[dict]:
    """Envelope records that count: written by an authority actor (A1.4, ruling 49)."""
    from .commands import authority_actors
    allowed = authority_actors()
    found = []
    if base.is_dir():
        for path in sorted(base.glob("EN-*.json")):
            data = util.read_json(path)
            if isinstance(data, dict) and data.get("actor") in allowed:
                found.append(data)
    return found


def last_envelope(project: dict) -> dict | None:
    found = _envelopes(tracker.project_dir(project["id"]) / "envelopes")
    return found[-1] if found else None


def programme_envelope(programme: str) -> dict | None:
    found = [e for e in _envelopes(tracker.root() / "initiatives" / programme / "envelopes")
             if e.get("state") == "active"]
    return found[-1] if found else None


def morning_go_queue() -> list[dict]:
    queue = []
    base = tracker.root() / "initiatives"
    if not base.is_dir():
        return queue
    for path in sorted(base.glob("*/morning-go/MG-*.json")):
        data = util.read_json(path)
        if isinstance(data, dict):
            queue.append({key: data.get(key) for key in
                          ("id", "programme", "project", "kind", "ref", "status", "commit_ranges", "flags")
                          if key in data or key != "flags"})
    return queue


GRANTS = ["land-lane", "push-lane", "merge-main-local", "self-approve-stage", "provisional-decision"]
HARD_STOPS = ["push", "force", "delete", "rewrite-history", "send-external", "spend", "workflow-change"]


def standing_grants(programme: str) -> list[str]:
    import yaml
    from .commands import authority_actors
    path = tracker.root() / "initiatives" / programme / "documents" / "autonomy.yaml"
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict) or data.get("actor") not in authority_actors():
        return []
    grants = data.get("standing_grants") or []
    if "spend" in grants:
        return []
    return [g for g in grants if g in GRANTS]


def snapshot(project_id: str) -> list[dict]:
    import subprocess
    from .commands import approved_pid, pid_document
    refs = []
    approved = approved_pid(project_id)
    if approved is not None:
        repos = (approved.get("guarded") or {}).get("repos") or []
    else:
        try:
            repos = pid_document(project_id).header.get("repos") or []
        except util.YggError:
            return refs
    for repo in repos:
        path = repo.get("path")
        if not path:
            continue
        result = util.trusted_git("-C", path, "for-each-ref", "--format=%(refname:short) %(objectname)",
                                  "refs/heads/ygg/" + project_id)
        for line in result.stdout.splitlines():
            branch, sha = line.split()
            refs.append({"repo": str(path), "branch": branch, "sha": sha})
    return refs


def grant(scope: str, until: str | None, through_stage: str | None, allow_local_merge: bool) -> str:
    from . import state
    state.check_user_only("envelope grant")
    util.check_id(scope, "scope")
    project = tracker.project(scope)
    programme = tracker.programme(scope) if project is None else None
    if project is None and programme is None:
        raise util.usage("unknown programme or project '" + scope + "'")
    if programme is not None and through_stage is not None:
        raise util.usage("a programme envelope takes --until only; --through-stage is for projects")
    if (until is None) == (through_stage is None):
        raise util.usage("give exactly one of --until or --through-stage")
    stage = util.check_stage(through_stage) if through_stage is not None else None
    until_value = util.iso(util.parse_time(until)) if until else None
    programme_name = project["programme"] if project else scope
    grants = standing_grants(programme_name)
    if allow_local_merge and "merge-main-local" not in grants:
        grants.append("merge-main-local")
    if not allow_local_merge:
        grants = [g for g in grants if g != "merge-main-local"]
    with util.locked():
        tracker.ensure_writable()
        identifier = tracker.allocate("envelope")
        base = (tracker.project_dir(scope) if project else tracker.root() / "initiatives" / scope) / "envelopes"
        for existing in _envelopes(base):
            if existing.get("state") == "active":
                existing["state"] = "ended"
                tracker.write_json(base / (existing["id"] + ".json"), existing)
        refs = []
        if project:
            refs = snapshot(scope)
        else:
            for item in tracker.projects():
                if item.get("programme") == scope:
                    refs += snapshot(item["id"])
        data = {"id": identifier, "scope": scope, "until": until_value, "through_stage": stage,
                "granted_at": util.iso(), "allow_local_merge": bool(allow_local_merge), "grants": grants,
                "ref_snapshot": refs, "state": "active", "standard_version": util.STANDARD_VERSION,
                "actor": "user"}
        tracker.write_json(base / (identifier + ".json"), data)
    return "envelope " + identifier + " granted for " + scope + "\n"


def revoke(scope: str) -> str:
    from . import state
    state.check_user_only("envelope revoke")
    util.check_id(scope, "scope")
    project = tracker.project(scope)
    base = (tracker.project_dir(scope) if project else tracker.root() / "initiatives" / scope) / "envelopes"
    affected = [project] if project else [p for p in tracker.projects() if p.get("programme") == scope]
    bases = [base] + ([] if project else [tracker.project_dir(p["id"]) / "envelopes" for p in affected])
    with util.locked():
        tracker.ensure_writable()
        for folder in bases:
            for existing in _envelopes(folder):
                if existing.get("state") == "active":
                    existing["state"] = "revoked"
                    existing["revoked_at"] = util.iso()
                    tracker.write_json(folder / (existing["id"] + ".json"), existing)
        for item in affected:
            item = tracker.project(item["id"])
            item["envelope_revoked_at"] = util.iso()
            tracker.owe(item, item["pm"], "envelope-revoked")
            tracker.save_project(item)
            if item.get("state") in ("executing", "awaiting-approval", "exception"):
                item["state"] = "awaiting-approval"
                item["hold"] = "envelope-revoked"
                tracker.save_project(item)
    return "envelope revoked for " + scope + "\n"


def effective_envelope(project: dict) -> dict | None:
    """The active envelope that applies to a project now (project scope first, then programme).

    A project-level revocation (hold or revocation marker) stops a programme envelope from covering it.
    """
    project = tracker.project(project["id"]) or project
    if project.get("hold") == "envelope-revoked":
        return None
    revoked_at = project.get("envelope_revoked_at")
    for envelope in (active_envelope(project), programme_envelope(project["programme"])):
        if not envelope:
            continue
        if envelope.get("until") and util.now() >= util.parse_time(envelope["until"]):
            continue
        if envelope.get("through_stage") is not None:
            stages = tracker.milestones(project["id"]).get("stages") or []
            current = next((s for s in stages if s.get("state") in ("executing", "awaiting-approval")), None)
            if current is None or int(current["number"]) > int(envelope["through_stage"]):
                continue
        if revoked_at and envelope.get("scope") != project["id"] and \
                util.parse_time(revoked_at) >= util.parse_time(envelope["granted_at"]):
            continue
        return envelope
    return None
