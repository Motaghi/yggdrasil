"""Stop and pause intents, and their reconciliation.

An intent is active while its marker is in the safety folder, or while a journalled `stop` or `pause` with its
id is listed by no committed `resume`. A committed resume authorises cleanup; only deleting the marker lifts
the block. Every command does its own local safety work (marker, signals) before any tracker write, so an
unwritable or busy tracker never delays a stop or a pause.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import config, safety, state, util

KINDS = ("stop", "pause")
JOURNAL_WAIT_SECONDS = 5.0


def journal_intents() -> tuple[list[dict], set, set]:
    """(journalled intents not listed by any committed resume, every journalled intent id, every id a
    committed resume lists), across all journals."""
    from . import reducer
    folded = reducer.folds()
    resumed: set = set()
    known: set = set()
    for programme_fold in folded.values():
        resumed |= programme_fold["resumed"]
        known |= programme_fold["intent_ids"]
    found, seen = [], set()
    for programme_fold in folded.values():
        for item in programme_fold["intents"]:
            if item["intent"] is None:
                found.append(item)  # written before ids existed: active until it is cleared
            elif item["intent"] not in resumed and item["intent"] not in seen:
                seen.add(item["intent"])
                found.append(item)
    return found, known, resumed


def active(kind: str | None = None) -> list[dict]:
    """Active intents: every marker, plus journalled intents no committed resume lists (by intent id)."""
    found, seen = [], set()
    for marker in safety.markers():
        if kind is None or marker.get("kind") == kind:
            seen.add(marker.get("intent"))
            found.append({"kind": marker.get("kind"), "scope": marker.get("scope"), "intent": marker.get("intent")})
    journalled, _, _ = journal_intents()
    for item in journalled:
        if (kind is None or item["kind"] == kind) and (item["intent"] is None or item["intent"] not in seen):
            seen.add(item["intent"])
            found.append(item)
    return found


def paused(programme: str | None) -> bool:
    if state.platform_paused():
        return True
    return any(i.get("scope") in ("*", programme) for i in active("pause"))


def stopped(node: dict) -> bool:
    scopes = ("*", node["id"], node.get("programme"))
    return any(i.get("scope") in scopes for i in active("stop"))


def paused_view() -> dict:
    scopes = {i.get("scope") for i in active("pause")}
    return {"global": "*" in scopes or state.platform_paused(),
            "programmes": sorted(s for s in scopes if s and s != "*")}


def stop_scopes() -> list[str]:
    return sorted({str(i.get("scope")) for i in active("stop") if i.get("scope")})


def in_scope(node: dict, scope: str | None) -> bool:
    return not scope or scope == "*" or scope in (node["id"], node.get("programme"))


# ---------------------------------------------------------------- stop targets and signals

def prepare(scope: str | None, errors: list | None = None) -> list[tuple[dict, str | None, dict]]:
    """Supplemental tracker evidence, read before the creation lock is taken (no tracker I/O ever happens
    under it): each node in scope, its open activation, and that activation's `spawned` fact. An unreadable
    tracker yields nothing here: local evidence alone still finds the targets (`collect_targets`)."""
    from . import lifecycle
    errors = errors if errors is not None else []
    try:
        nodes = state.all_nodes()
    except (util.YggError, OSError) as error:
        errors.append(getattr(error, "message", str(error)))
        return []
    found = []
    for node in nodes.values():
        if not in_scope(node, scope):
            continue
        key, spawned = None, {}
        try:
            key = lifecycle.open_activation(node["id"])
            if key:
                spawned = lifecycle.phase_fields(lifecycle.facts(node.get("programme"), key), "spawned") or {}
        except (util.YggError, OSError) as error:
            errors.append(getattr(error, "message", str(error)))
        found.append((node, key, spawned))
    return found


def collect_targets(prepared: list, errors: list | None = None, scope: str | None = None,
                    unresolved: list | None = None) -> list[tuple[str, str, int, str | None]]:
    """Live agents in scope. Local evidence first, needing no tracker: every activation record whose node
    or programme is in scope, with a live, matching process, or, with no recorded agent, the orphan scan
    verified by the nonce the supervisor recorded locally. Tracker evidence (`prepared`) only adds to it.
    Call under the creation lock: local reads and a process scan only. Returns (node, activation, pid,
    start time). Only positively identified processes are targets: a recorded pid with a matching start
    time, or one whose real environment carries this activation, state root and recorded nonce. A live pid
    whose identity cannot be established is added to `unresolved` and never signalled."""
    from . import lifecycle
    errors = errors if errors is not None else []
    unresolved = unresolved if unresolved is not None else []
    found: list = []
    seen: set = set()

    def add(node_id, key, pid, started):
        if not started:
            if (key, pid) not in unresolved:
                unresolved.append((key, pid))  # identified, but its start time cannot be re-checked
            return
        if (key, pid) not in seen:
            seen.add((key, pid))
            found.append((node_id, key, pid, started))

    for record in _records(errors):
        if not in_scope({"id": record.get("node"), "programme": record.get("programme")}, scope):
            continue
        if record.get("exited_at") is not None or record.get("terminal"):
            continue
        key, node_id = str(record.get("activation")), str(record.get("node"))
        pid = record.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool):
            seen_now = lifecycle.evidence(pid, lifecycle.recorded_start(record))
            if seen_now == "alive":
                exact = lifecycle.signal_identity(pid, key, record)
                if exact:
                    add(node_id, key, pid, exact)
                elif (key, pid) not in unresolved:
                    unresolved.append((key, pid))  # matches only in some zone, and not verified: held
            elif seen_now == "unknown":
                if _nonce_verified(pid, key, record.get("agent_nonce")):
                    add(node_id, key, pid, util.process_start(pid))
                elif (key, pid) not in unresolved:
                    unresolved.append((key, pid))
            continue
        if record.get("agent_nonce") and record.get("supervisor_started_at"):
            status, pids = lifecycle.orphan_agents(key, record["agent_nonce"], record["supervisor_started_at"])
            for orphan in pids if status == "ok" else []:
                add(node_id, key, orphan, util.process_start(orphan))
    for node, key, spawned in prepared:
        if not key or not spawned or any(t[1] == key for t in found):
            continue
        status, pids = lifecycle.orphan_agents(key, spawned.get("agent_nonce"), spawned.get("supervisor_started_at"))
        for orphan in pids if status == "ok" else []:
            add(node["id"], key, orphan, util.process_start(orphan))
    return found


def _nonce_verified(pid: int, key: str, nonce: str | None) -> bool:
    """Whether the process's real environment names this activation, this state root and its nonce."""
    from . import lifecycle
    return lifecycle.nonce_verified(pid, key, nonce)


def _records(errors: list) -> list[dict]:
    """Every activation record, read one by one: a corrupt record is reported and skipped."""
    found = []
    base = state.root() / "activations"
    if not base.is_dir():
        return found
    for path in sorted(base.glob("act-*.json")):
        try:
            data = util.read_json(path, {}) or {}
        except util.YggError as error:
            errors.append(error.message)
            continue
        if isinstance(data, dict) and data.get("node") and data.get("activation"):
            found.append(data)
    return found


def group_members(pgid: int) -> list[int] | None:
    """The live processes (zombies left out) of process group `pgid`, or None when the process list can't be read
    (the answer is unknown, never "gone"). A process group id is never reused while any member of the group is
    alive, so a non-empty answer is still the agent's group."""
    try:
        result = subprocess.run(["ps", "-axo", "pid=,pgid=,stat="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None  # ps always lists at least itself; an empty or failed listing proves nothing
    rows = result.stdout.splitlines()
    members = []
    for row in rows:
        parts = row.split()
        if len(parts) >= 3 and parts[1] == str(pgid) and not parts[2].startswith("Z"):
            try:
                members.append(int(parts[0]))
            except ValueError:
                continue
    return members


def group_alive(pgid: int) -> bool:
    """Whether any process of the group may still be alive. Only a positive answer that the group is empty counts
    as gone (AC-71): when the process list can't be read, the kernel is asked whether the group exists at all,
    and anything but a clear "no such group" counts as alive."""
    members = group_members(pgid)
    if members is not None:
        return bool(members)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def signal_targets(targets: list) -> set:
    """Stop each target's whole process group in order (AC-18, AC-71): SIGINT, then SIGTERM after
    `stop_grace_seconds` to whatever of the group is still there, then SIGKILL after another
    `stop_grace_seconds`. Each step goes only as far as needed: a group that is gone gets nothing more. Returns
    the pids that were alive when signalled. Dead targets are skipped, so repeating it is harmless."""
    from . import lifecycle
    live = {}
    attempts = {}
    groups = {}
    for _, key, pid, started in targets:
        if _same(pid, started):
            live[pid] = started
            try:
                groups[pid] = os.getpgid(pid) == pid  # the supervisor starts each agent in its own group
            except (ProcessLookupError, PermissionError, OSError):
                groups[pid] = False
            # Recorded first: until this attempt's kill call settles delivery, a clean exit is held as
            # ambiguous, so the work it was given is never consumed by a stop (I4).
            attempts[pid] = lifecycle.mark_signalled(key, "stop")
            if _signal(pid, signal.SIGINT):
                lifecycle.mark_sent(key, "stop", attempts[pid])
            else:
                lifecycle.mark_missed(key, "stop", attempts[pid])
    keys = {pid: key for _, key, pid, _ in targets}

    def remaining(pid) -> bool:
        if groups.get(pid):
            return group_alive(pid)
        return _same(pid, live[pid])

    grace = max(float(config.get("stop_grace_seconds", 10)), 0.0)
    for number in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and any(remaining(pid) for pid in live):
            time.sleep(0.1)
        for pid in live:
            if not remaining(pid):
                continue
            if groups.get(pid):
                sent = _signal_group(pid, number)  # members remain, so the group id is still the agent's
            else:
                sent = _same(pid, live[pid]) and _signal(pid, number)  # a reused pid is never signalled
            if sent:
                lifecycle.mark_sent(keys[pid], "stop", attempts[pid])
    deadline = time.monotonic() + 2.0  # SIGKILL can't be ignored: give the kernel a moment to finish it
    while time.monotonic() < deadline and any(remaining(pid) for pid in live):
        time.sleep(0.05)
    return set(live)


def _signal_group(pgid: int, number: int) -> bool:
    try:
        os.killpg(pgid, number)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


LOCK_KEPT = "index.lock kept: another process has it open"


def release_locks(targets: list) -> list[str]:
    """Remove each stopped agent's own stale lane index lock (AC-18): only its own lane's lock (`git rev-parse
    --git-path index.lock` in its working folder), only once every process of its group is gone, and only when
    no live process has the file open. Returns a line for each lock found."""
    from . import launcher
    notes = []
    done = set()
    for _, key, pid, _ in targets:
        if key in done or group_alive(pid):
            continue
        done.add(key)
        data = launcher.receipt(key)
        cwd = data.get("cwd")
        if data.get("cli") == "codex" or (cwd and _in_a_box(str(cwd))):
            # A Codex receipt's folder is a box or a scratch folder, and a folder inside a registered box is the
            # agent's whatever the receipt says: trusted code never stats it or runs git there (v1.1.1,
            # invariant T); the tender's `prepare --sole` removes a stale lock in a box.
            continue
        if not cwd or not os.path.isdir(str(cwd)):
            continue
        found = util.trusted_git("-C", cwd, "rev-parse", "--git-path", "index.lock")
        relative = found.stdout.strip()
        if found.returncode != 0 or not relative:
            continue
        path = Path(relative) if os.path.isabs(relative) else Path(cwd) / relative
        if not path.is_file():
            continue
        if _open_elsewhere(path):
            notes.append(LOCK_KEPT + " (" + str(path) + ")")
            continue
        try:
            path.unlink()
            notes.append("index.lock removed: " + str(path))
        except FileNotFoundError:
            continue
        except OSError as error:
            notes.append("index.lock kept: " + str(error))
    return notes


def _in_a_box(path: str) -> bool:
    """Whether `path` is a registered clone's checkout or lies inside a live or quarantined box, decided from the
    registry and a walk that stops at the first box (quarantine.inside_box), never by looking inside one."""
    from . import quarantine
    return quarantine.is_clone_checkout(path) or quarantine.inside_box(path)


def _open_elsewhere(path: Path) -> bool:
    """Whether any live process has the file open. When that can't be told, the answer is yes (kept)."""
    lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None)
    if lsof is None:
        return True
    try:
        result = subprocess.run([lsof, "-t", "--", str(path)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode == 0 and result.stdout.strip():
        return True
    # lsof exits 1 both when no process has the file open (and prints nothing) and on an error (with a message on
    # stderr); only the silent no-match is proof the lock is free (AC-18). Anything else keeps the lock.
    return not (result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip())


def _same(pid: int, started: str | None) -> bool:
    """The process is alive and still the one identified: its exact start identity matches (an ambiguous
    older match never authorises a signal; targets carry the verified exact identity)."""
    return util.pid_alive(pid) and bool(started) and util.start_match(started, util.process_start(pid)) == "exact"


def _signal(pid: int, number: int) -> bool:
    """Send the signal; True only when the kill call succeeded (the process was there to receive it)."""
    try:
        own_group = os.getpgid(pid) == pid
    except (ProcessLookupError, PermissionError, OSError):
        return False
    try:
        if own_group:
            os.killpg(pid, number)
        else:
            os.kill(pid, number)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


# ---------------------------------------------------------------- reconciliation

def complete_stops(exclude: str | None = None) -> None:
    """Step 1: for every active stop intent, collect live targets in its scope under the creation lock,
    then signal and escalate. No tracker write."""
    try:
        intents = [i for i in active("stop") if i.get("intent") != exclude]
    except util.YggError:
        intents = [{"scope": m.get("scope"), "intent": m.get("intent")} for m in safety.markers()
                   if m.get("kind") == "stop" and m.get("intent") != exclude]
    targets: list = []
    for item in intents:
        scope = None if item.get("scope") == "*" else item.get("scope")
        prepared = prepare(scope)
        with util.locked(util.CREATION_LOCK):
            targets += [t for t in collect_targets(prepared, scope=scope) if t not in targets]
    if targets:
        signal_targets(targets)


def persist_offline() -> None:
    """Step 2: commit `stop` or `pause` for every marker with no journal fact (idempotent by intent id)."""
    from . import journal, tracker
    with util.locked():
        _, known, _ = journal_intents()
        for marker in safety.markers():
            if marker.get("intent") in known or marker.get("kind") not in KINDS:
                continue
            scope = marker.get("scope") or "*"
            home = scope if scope != "*" and tracker.programme(scope) is not None else None
            journal.add(home, marker["kind"], scope=scope, intent=marker["intent"])


def finish_resumes() -> None:
    """Step 3: delete the markers whose ids a committed resume lists."""
    _, _, resumed = journal_intents()
    listed = [str(m.get("intent")) for m in safety.markers() if m.get("intent") in resumed]
    if listed:
        safety.remove_markers(listed)


def reconcile(exclude: str | None = None, *, local: bool = True, tracker: bool = True) -> None:
    """All three steps, each tolerant of an unreadable or unwritable tracker (the next run repeats it)."""
    if local:
        complete_stops(exclude)
    if tracker:
        for step in (persist_offline, finish_resumes):
            try:
                step()
            except util.YggError:
                pass
