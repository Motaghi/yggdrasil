"""Activation lifecycle facts in the journal, process evidence, and recovery from them.

Facts (journal kind `activation`, keyed by the activation id): `reserved` (the launcher, with its own pid and
start time), `spawned` (the supervisor at its gate, with its pid and start time), and one terminal fact,
`exited`, `aborted` or `released`. The first terminal fact by journal order wins; a later one is rejected
under the journal lock (a stale writer) and ignored by every reader. The activation record mirrors the
terminal fact in its `terminal` field.

Process evidence is `alive`, `dead` or `unknown`, from a pid plus its recorded start time. Elapsed time is
never evidence. Once `spawned` is committed, an activation is aborted only on positive evidence written by
its supervisor (its own `aborted`, or a no-agent record in the safety folder); anything else that cannot be
settled from a live process or an exit record is uncertain and waits for the user (`ygg node release`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from . import faults, state, util

TERMINAL = ("exited", "aborted", "released")
PHASES = ("reserved", "spawned") + TERMINAL


def _programme(node_id: str):
    return (state.all_nodes().get(node_id) or {}).get("programme")


def facts(programme, activation: str) -> list[dict]:
    """The trusted lifecycle facts of one activation, in journal order."""
    from . import journal
    entries, _ = journal.trusted_entries(programme)
    return [e for e in entries if e.get("kind") == "activation" and e.get("key") == activation
            and not e.get("cleared_at")]


def all_facts(programme) -> dict:
    """activation id -> its lifecycle facts, for one programme journal."""
    from . import journal
    entries, _ = journal.trusted_entries(programme)
    found: dict = {}
    for entry in entries:
        if entry.get("kind") == "activation" and not entry.get("cleared_at"):
            found.setdefault(entry["key"], []).append(entry)
    return found


def terminal_of(found: list[dict]) -> str | None:
    return next((e["phase"] for e in found if e.get("phase") in TERMINAL), None)


def phase_fields(found: list[dict], phase: str) -> dict:
    return next((e for e in found if e.get("phase") == phase), {})


def commit(node_id: str, activation: str, phase: str, **fields) -> bool:
    """Commit one lifecycle fact under the journal lock. A terminal fact for an activation that already has
    one is rejected (returns False); so is a `spawned` after a terminal fact. The record's `terminal` mirror
    is written after the fact commits."""
    from . import journal, launcher
    programme = _programme(node_id)
    with util.locked():
        found = facts(programme, activation)
        done = terminal_of(found)
        if done is not None and (phase in TERMINAL or phase == "spawned"):
            return False
        if phase == "spawned":
            faults.barrier("in-journal-write", node_id, activation)
        journal.add(programme, "activation", key=activation, node=node_id, phase=phase, **fields)
        if phase in TERMINAL:
            launcher.update_receipt(activation, terminal=phase)
    return True


def mirror_terminal(node_id: str, activation: str) -> str | None:
    """Repair the record's `terminal` mirror from the journal (reconciliation)."""
    from . import launcher
    done = terminal_of(facts(_programme(node_id), activation))
    if done and launcher.receipt(activation).get("terminal") != done:
        launcher.update_receipt(activation, terminal=done)
    return done


# ---------------------------------------------------------------- process evidence

def evidence(pid, started) -> str:
    """`alive`, `dead` or `unknown` for a pid with its recorded start time. A recorded pid whose start time
    is missing, or whose start time cannot be read, is `unknown`; a mismatching start time is `dead`."""
    if faults.probe_unknown():
        return "unknown"
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return "dead"
    if not util.pid_alive(pid):
        return "dead"
    actual = util.process_start(pid)
    if actual is None or not started:
        return "unknown"
    return "alive" if util.start_matches(started, actual) else "dead"  # zone- and locale-proof


def recorded_start(record: dict) -> str | None:
    """The start time to check a recorded agent against: the exact identity verified for it, else the one
    recorded at spawn (older receipts hold lstart text)."""
    return record.get("process_start_identity") or record.get("process_started_at")


def nonce_verified(pid: int, key: str, nonce: str | None) -> bool:
    """Whether the process's real environment names this activation, this state root and its nonce."""
    if not nonce:
        return False
    environment = process_environment(pid)
    roots = {str(util.state_root()), str(util.state_root().resolve())}
    return bool(environment) and environment.get("YGG_ACTIVATION") == key and \
        environment.get("YGG_ACTIVATION_NONCE") == nonce and environment.get("YGG_STATE_ROOT") in roots


def signal_identity(pid, key: str, record: dict) -> str | None:
    """The exact start identity under which the recorded agent `pid` may be signalled, or None (held). An
    exact match stands on its own; a timezone-ambiguous match of older lstart text counts only once the
    process's real environment names this activation, state root and nonce, and the exact kernel start is
    then recorded for every later check."""
    actual = util.process_start(pid)
    kind = util.start_match(recorded_start(record), actual)
    if kind == "exact":
        return recorded_start(record) if str(recorded_start(record)).startswith("s:") else actual
    if kind == "ambiguous" and actual and nonce_verified(pid, key, record.get("agent_nonce")):
        from . import launcher
        launcher.update_receipt(key, process_start_identity=actual)
        return actual
    return None


def own_identity() -> dict:
    pid = os.getpid()
    return {"pid": pid, "started_at": util.process_start(pid)}


def process_environment(pid: int) -> dict | None:
    """A process's real environment, read apart from its arguments (macOS KERN_PROCARGS2, Linux
    /proc/<pid>/environ); None when it cannot be read."""
    import sys
    if sys.platform.startswith("linux"):
        try:
            raw = Path("/proc/" + str(pid) + "/environ").read_bytes()
        except OSError:
            return None
        return _pairs(raw.split(b"\0"))
    if sys.platform != "darwin":
        return None
    import ctypes
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    argmax = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(argmax))
    if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
        return None  # CTL_KERN, KERN_ARGMAX
    buffer = ctypes.create_string_buffer(argmax.value)
    size = ctypes.c_size_t(argmax.value)
    if libc.sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buffer, ctypes.byref(size), None, 0) != 0:
        return None  # CTL_KERN, KERN_PROCARGS2: argc, exec path, argv, then envp
    raw = buffer.raw[:size.value]
    if len(raw) < 4:
        return None
    argc = int.from_bytes(raw[:4], sys.byteorder)
    rest = raw[4:]
    end = rest.find(b"\0")
    if end < 0:
        return None
    position = end
    while position < len(rest) and rest[position:position + 1] == b"\0":
        position += 1  # the exec path and its padding
    fields = rest[position:].split(b"\0")
    return _pairs(fields[argc:])  # skip argv: what follows is the environment


def _pairs(fields: list) -> dict:
    found = {}
    for field in fields:
        if not field:
            break
        name, sep, value = field.partition(b"=")
        if sep:
            found[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return found


SYSTEM_PREFIXES = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/usr/libexec/", "/System/")


def _agent_binaries() -> set:
    found = set()
    for variable, default in (("YGG_CLAUDE_BIN", "claude"), ("YGG_CODEX_BIN", "codex")):
        import shutil
        value = util.env(variable, default) or default
        resolved = shutil.which(value) or value
        found.add(str(Path(resolved).resolve()) if os.path.isabs(resolved) else resolved)
    return found


def executable_path(pid: int) -> str | None:
    """A process's executable (macOS proc_pidpath, Linux /proc/<pid>/exe); None when unreadable."""
    import sys
    if sys.platform.startswith("linux"):
        try:
            return os.readlink("/proc/" + str(pid) + "/exe")
        except OSError:
            return None
    if sys.platform != "darwin":
        return None
    import ctypes
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    except OSError:
        return None
    buffer = ctypes.create_string_buffer(4096)
    length = libproc.proc_pidpath(pid, buffer, ctypes.sizeof(buffer))
    return buffer.value.decode("utf-8", "replace") if length > 0 else None


def _candidates(not_before: str | None) -> tuple[str, list[int]]:
    """Plausible orphan agents: this user's processes whose parent is launchd (the supervisor died) or the
    recorded supervisor, started no earlier than the supervisor, and not system binaries (unless an agent
    binary itself lives there)."""
    try:
        result = subprocess.run(["/bin/ps", "-x", "-U", str(os.getuid()), "-o", "pid=,ppid=,lstart=,comm="],
                                capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, TZ="UTC", LC_ALL="C", LANG="C"))  # start times in UTC
    except (OSError, subprocess.SubprocessError):
        return "unknown", []
    if result.returncode != 0:
        return "unknown", []
    floor = None
    if not_before:
        # The earliest instant the recorded start may denote (an older record's zone is not known): a lower
        # floor only lets more candidates through to the environment check, never fewer.
        epochs = util.start_epochs(not_before)
        floor = min(epochs) if epochs else None
    system_agent = any(b.startswith(SYSTEM_PREFIXES) for b in _agent_binaries())
    found = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 8 or not parts[0].isdecimal() or not parts[1].isdecimal():
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        command = " ".join(parts[7:])
        if pid in (os.getpid(), os.getppid()) or ppid != 1:
            continue
        executable = executable_path(pid) or command
        if executable.startswith(SYSTEM_PREFIXES) and not system_agent:
            continue
        if floor is not None:
            epochs = util.start_epochs(" ".join(parts[2:7]), zones=("UTC",))  # ps ran with TZ=UTC
            started = min(epochs) if epochs else None
            if started is not None and started < floor:
                continue
        found.append(pid)
    return "ok", found


def orphan_agents(activation: str, nonce: str | None = None, not_before: str | None = None) -> tuple[str, list[int]]:
    """Processes whose real environment carries exactly `YGG_ACTIVATION=<activation>`, this state root and
    (when one was recorded) the activation's nonce: (`ok`, pids). Arguments are never matched. When the scan
    fails, or any plausible candidate's environment cannot be read, the answer is (`unknown`, []): the
    activation stays a visible hold rather than guessing."""
    if faults.probe_unknown():
        return "unknown", []
    status, candidates = _candidates(not_before)
    if status != "ok":
        return "unknown", []
    roots = {str(util.state_root()), str(util.state_root().resolve())}
    found = []
    unreadable = False
    for pid in candidates:
        if not util.pid_alive(pid):
            continue
        environment = process_environment(pid)
        if environment is None:
            if util.pid_alive(pid):
                unreadable = True
            continue
        if environment.get("YGG_ACTIVATION") != activation or environment.get("YGG_STATE_ROOT") not in roots:
            continue
        if nonce and environment.get("YGG_ACTIVATION_NONCE") != nonce:
            continue
        found.append(pid)
    if unreadable:
        return "unknown", []
    return "ok", found


# ---------------------------------------------------------------- positive no-agent evidence

def no_agent_path(activation: str) -> Path:
    from . import safety
    return safety.root() / "no-agent" / (activation + ".json")


def no_agent_record(activation: str) -> dict | None:
    """The supervisor's no-agent record, parsed and checked to name this activation; None when it is
    missing, corrupt or names another activation."""
    try:
        data = util.read_json(no_agent_path(activation), None)
    except util.YggError:
        return None
    if not isinstance(data, dict) or data.get("activation") != activation:
        return None
    return data


def revoked_path(activation: str) -> Path:
    from . import safety
    return safety.root() / "revoked" / (activation + ".json")


def write_local(path: Path, payload: dict) -> None:
    util.atomic_write(path, json.dumps(payload) + "\n")


def signalled_path(activation: str) -> Path:
    from . import safety
    return safety.root() / "signalled" / (activation + ".json")


def mark_signalled(activation: str, why: str) -> str:
    """Open one signalling attempt, recorded as `intent` before a stop or reset signals the activation's
    agent, and return its id. The attempt stays `intent` until that same attempt resolves it, so a stopper
    that dies between its intent and its kill call leaves the exit held as ambiguous."""
    attempt = uuid.uuid4().hex[:12]
    _mark(activation, attempt, why, "intent")
    return attempt


def mark_sent(activation: str, why: str, attempt: str) -> None:
    """The attempt's signal was delivered (its kill call succeeded). Final for that attempt."""
    _mark(activation, attempt, why, "sent")


def mark_missed(activation: str, why: str, attempt: str) -> None:
    """The attempt's kill call failed (the process was gone or could not be signalled): this attempt
    reached nothing. It never downgrades a delivery the attempt already recorded."""
    _mark(activation, attempt, why, "missed")


def _mark(activation: str, attempt: str, why: str, value: str) -> None:
    """Read-modify-write of the activation's signal record under the creation lock (local safety-folder
    work), so overlapping stops and resets serialise: each attempt changes only its own entry, an `intent`
    moves to `sent` or `missed`, `missed` may still become `sent`, and `sent` is final (I4)."""
    from . import safety
    with util.locked(util.CREATION_LOCK):
        util.ensure_dir(safety.root() / "signalled")
        attempts = _attempts(activation)
        current = (attempts.get(attempt) or {}).get("state")
        if current == "sent" or (current and value == "intent"):
            return
        attempts[attempt] = {"state": value, "reason": why, "at": util.iso()}
        write_local(signalled_path(activation), {"activation": activation, "attempts": attempts})


def _attempts(activation: str) -> dict:
    """The activation's signalling attempts, id -> {state, reason, at}. A record from before attempts were
    kept counts as one attempt with its state."""
    path = signalled_path(activation)
    data = util.read_json(path, None) if path.exists() else None
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("attempts"), dict):
        return {str(k): dict(v) for k, v in data["attempts"].items() if isinstance(v, dict)}
    state = data.get("state") if data.get("state") in ("sent", "missed") else "intent"
    return {"legacy": {"state": state, "reason": data.get("reason"), "at": data.get("at")}}


def signal_state(activation: str) -> str | None:
    """None (never signalled), `sent` (some attempt was delivered), `intent` (some attempt is unresolved)
    or `missed` (every attempt is proven not delivered). Only `missed` and None leave a clean exit counted
    as the agent's own; `intent` and `sent` hold it as ambiguous."""
    states = {entry.get("state") for entry in _attempts(activation).values()}
    if not states:
        return None
    if "sent" in states:
        return "sent"
    if states - {"missed"}:
        return "intent"
    return "missed"


def ambiguous_activations(node_ids: list | None = None) -> list[str]:
    """Activations that exited after a stop or reset signal reached them, without dying of it: they may have
    finished or been interrupted. Held (visible) until the user settles them with `ygg node release`."""
    return sorted(_ambiguous(node_ids))


def held_stop_nodes() -> list[str]:
    """Nodes with an unsettled ambiguous activation (one pass over the folds)."""
    return sorted(set(_ambiguous().values()))


def _ambiguous(node_ids: list | None = None) -> dict[str, str]:
    """Unsettled ambiguous activations, each with its node."""
    return {key: record["node"] for key, record in _ambiguous_records(node_ids).items()}


def _ambiguous_records(node_ids: list | None = None) -> dict[str, dict]:
    from . import reducer
    nodes = state.all_nodes()
    wanted = set(nodes if node_ids is None else node_ids)
    found = {}
    for programme_fold in reducer.folds().values():
        for key, record in programme_fold["activations"].items():
            end = record.get("end") or {}
            if record.get("node") in wanted and end.get("ambiguous") and not record.get("resolution"):
                found[key] = record
    return found


def held_stop_work(node_id: str) -> list[str]:
    """The work items (`kind:id`) the node's unsettled held stops were given: neither done nor owed."""
    from . import reducer
    items = []
    for record in _ambiguous_records([node_id]).values():
        for kind, ids in (record.get("work") or {}).items():
            items += [reducer.WORK_KEYS.get(kind, kind) + ":" + str(i) for i in ids or []]
    return sorted(set(items))


def alert_stop_holds() -> None:
    """One `ygg-stop-held` alert per ambiguous hold, naming the node and the command that settles it. The
    supervisor calls it after such an exit and the conductor on every pass, so a hold found by recovery is
    reported too. The alert itself is the record that it was reported (an alert per activation in
    `alerts.jsonl`, which is append-only): a pass that stopped before appending it reports it next time."""
    if not _ambiguous():
        return
    with util.locked():  # one emitter at a time, so a hold is never reported twice
        holds = _ambiguous()
        reported = {a.get("activation") for a in state.alerts() if a.get("kind") == "ygg-stop-held"}
        for key in sorted(holds):
            if key in reported:
                continue
            node_id = holds[key]
            state.alert("ygg-stop-held", "Activation " + key + " of " + node_id + " exited after a stop "
                        "reached it and may not have finished; it is held until you settle it with `ygg node "
                        "release " + node_id + " --as aborted|failed` (aborted offers its work again, failed "
                        "counts it as done)", node=node_id, activation=key)


def revoke(activation: str, why: str) -> None:
    """A local revocation record, under the creation lock, before the journal fact that ends an activation
    from outside its supervisor (recovery, release, reset). It can only ever prevent an agent being made."""
    with util.locked(util.CREATION_LOCK):
        write_local(revoked_path(activation), {"activation": activation, "reason": why, "at": util.iso()})


# ---------------------------------------------------------------- recovery

def settle(node_id: str, activation: str, data: dict, found: list[dict]) -> str:
    """Settle one non-terminal activation from its facts and the evidence. Returns what it found:
    `pending`, `running`, `aborted`, `unsettled` (to be held as uncertain), `unknown` or `exited`."""
    from . import launcher
    if phase_fields(found, "spawned").get("launcher") == "t3":
        # A T3 session: T3's thread is the evidence (ruling 106); an ended run is committed by the per-node
        # pass, a gone thread is held as uncertain (I2), anything unreadable is unknown.
        from . import t3
        seen = t3.evidence(node_id, activation)["state"]
        return {"alive": "running", "ended": "pending", "gone": "unsettled"}.get(seen, "unknown")
    if "spawned" not in {e.get("phase") for e in found}:
        seen = launch_step(found, data)
        if seen == "alive":
            return "pending"
        if seen == "unknown":
            return "unknown"
        if held_before_spawn(data, seen):
            # Its confirm step already held it as uncertain (a start held past the confirm window): its supervisor
            # may still start the agent, so it stays held for the user, never aborted and relaunched by a later tick
            # (A11 AC-43 row 3, AC-22; I2: uncertain is held until settled).
            return "unsettled"
        with util.locked():
            if terminal_of(facts(_programme(node_id), activation)) is None and \
                    "spawned" not in {e.get("phase") for e in facts(_programme(node_id), activation)}:
                revoke(activation, "launcher-exited")
                launcher.abort_activation(node_id, activation, "launcher-exited")
        return "aborted"
    # Spawned: an agent may exist.
    if data.get("exited_at") is not None:
        return "exited"  # the exit record settles it (classified by the receipt path)
    agent = evidence(data.get("pid"), recorded_start(data)) if data.get("pid") is not None else None
    if agent == "alive":
        return "running"
    if agent == "unknown":
        return "unknown"
    spawned = phase_fields(found, "spawned")
    supervisor = evidence(spawned.get("supervisor_pid"), spawned.get("supervisor_started_at"))
    if supervisor == "alive":
        return "pending"
    if supervisor == "unknown":
        return "unknown"
    if no_agent_path(activation).exists():
        record = no_agent_record(activation)
        if record is None:
            return "corrupt"  # never proof: held as uncertain and reported
        with util.locked():
            if launcher.commit_aborted(node_id, activation, "no-agent: " + str(record.get("reason"))):
                no_agent_path(activation).unlink(missing_ok=True)
        return "aborted"
    if data.get("pid") is None:
        status, pids = orphan_agents(activation, spawned.get("agent_nonce"), spawned.get("supervisor_started_at"))
        if status == "unknown":
            return "unknown"
        live = [p for p in pids if util.pid_alive(p)]
        if len(live) > 1:
            return "unsettled"  # more than one candidate: never guess, hold it as uncertain
        if live:
            # Everything the record needs is resolved first: no tracker read (scope) and no process probe
            # happens inside the receipt lock, which a supervisor holding the creation lock may be waiting on.
            fields = {"node": node_id, "activation": activation, "lifecycle": True, "pid": live[0],
                      "process_started_at": util.process_start(live[0]), "adopted_at": util.iso(),
                      "agent_nonce": spawned.get("agent_nonce"),
                      "supervisor_started_at": spawned.get("supervisor_started_at"), **scope_fields(node_id)}
            with util.locked(util.RECEIPT_LOCK):
                record = launcher.receipt(activation)
                record.update(fields)
                launcher.save_receipt(activation, record, scope={})
            return "running"
    return "unsettled"


def evidence_unknown(node_id: str, activation: str, data: dict) -> bool:
    """Whether a non-terminal, spawned activation's process evidence is unknown right now: shown as a
    visible hold (in `uncertain_keys`) without being persisted, since the next probe may answer."""
    if data.get("terminal") or data.get("exited_at") is not None:
        return False
    # Membership comes from the journal: a wiped activation file still leaves the facts.
    found = facts(_programme(node_id), activation)
    if not found or terminal_of(found) is not None:
        return False
    if phase_fields(found, "spawned").get("launcher") == "t3":
        from . import t3
        return t3.evidence(node_id, activation)["state"] == "unknown"
    if "spawned" not in {e.get("phase") for e in found}:
        return unspawned_evidence(found, data) == "unknown"
    if data.get("pid") is not None:
        return evidence(data.get("pid"), recorded_start(data)) == "unknown"
    spawned = phase_fields(found, "spawned")
    supervisor = evidence(spawned.get("supervisor_pid"), spawned.get("supervisor_started_at"))
    if supervisor != "dead":
        return supervisor == "unknown"
    if no_agent_path(activation).exists():
        return False
    status, _ = orphan_agents(activation, spawned.get("agent_nonce"), spawned.get("supervisor_started_at"))
    return status == "unknown"


def open_evidence(node_id: str, key: str) -> str | None:
    """`live` or `unknown` when any process of the activation may still be running (its launcher before
    `spawned`, its recorded agent, its supervisor, or an unrecorded agent found by the orphan scan), else
    None: fresh evidence, never the cached runtime."""
    from . import launcher
    data = launcher.receipt(key)
    found = facts(_programme(node_id), key)
    if not found:
        return None
    if phase_fields(found, "spawned").get("launcher") == "t3":
        from . import t3
        seen = t3.evidence(node_id, key)["state"]
        return {"alive": "live", "pending": "unknown", "unknown": "unknown"}.get(seen)
    if data.get("pid") is not None and data.get("exited_at") is None:
        seen = evidence(data.get("pid"), recorded_start(data))
        if seen != "dead":
            return "live" if seen == "alive" else "unknown"
    spawned = phase_fields(found, "spawned")
    if not spawned:
        seen = unspawned_evidence(found, data)
        return None if seen == "dead" else ("live" if seen == "alive" else "unknown")
    supervisor = evidence(spawned.get("supervisor_pid"), spawned.get("supervisor_started_at"))
    if supervisor != "dead":
        return "live" if supervisor == "alive" else "unknown"
    if data.get("pid") is None:
        status, pids = orphan_agents(key, spawned.get("agent_nonce"), spawned.get("supervisor_started_at"))
        if status == "unknown":
            return "unknown"
        if pids:
            return "live"
    return None


def scope_fields(node_id: str) -> dict:
    """The scope metadata a reconstructed activation record carries, so an offline stop can match it to a
    programme or project scope from local evidence alone."""
    node = state.all_nodes().get(node_id) or {}
    return {"programme": node.get("programme"), "project": node.get("project"), "role": node.get("role")}


def launch_step(found: list[dict], data: dict) -> str:
    """`alive`, `dead` or `unknown` for the launch call that reserved an unspawned activation: its launcher's
    evidence until the launcher recorded that its launch step returned (`launch_returned_at`), then `dead`, even
    while that launcher (a long-running conductor) lives on."""
    if data.get("launch_returned_at"):
        return "dead"
    reserved = phase_fields(found, "reserved")
    return evidence(reserved.get("launcher_pid"), reserved.get("launcher_started_at"))


def unspawned_evidence(found: list[dict], data: dict) -> str:
    """`alive`, `dead` or `unknown` for whatever may still start an unspawned activation's agent: its launch call
    (`launch_step`), then the supervisor that call recorded right after creating it. No supervisor recorded once
    the launch call is gone is `dead`: none was created, or one created by a launcher that died before recording
    it can't pass the gate once a terminal fact is committed (A7 I1)."""
    seen = launch_step(found, data)
    if seen != "dead" or data.get("supervisor_pid") is None:
        return seen
    return evidence(data.get("supervisor_pid"), data.get("supervisor_started_at"))


def held_before_spawn(receipt: dict, reserving: str | None) -> bool:
    """An unspawned reservation held as uncertain: its confirm step held it (receipt outcome `uncertain`) and the
    process that made the reservation is dead. `settle` keeps it held and `is_uncertain` reports it (AC-43 row 3,
    AC-99 row 3); one condition for both."""
    return receipt.get("outcome") == "uncertain" and reserving == "dead"


def is_uncertain(node_id: str, key: str) -> bool:
    """Uncertain, derived from lifecycle facts and fresh evidence alone (no stored mirror): spawned, no
    terminal fact, nothing of it alive or unknown, no positive no-agent evidence, and no exit record that
    reconciliation would commit."""
    from . import launcher
    found = facts(_programme(node_id), key)
    if not found or terminal_of(found) is not None:
        return False
    if "spawned" not in {e.get("phase") for e in found}:
        # A start held past its confirm window: the confirm step held it as uncertain and its reserving process is
        # gone, so `settle` keeps it held (AC-43 row 3) and status must show it too (A11 AC-99 row 3, AC-22).
        data = launcher.receipt(key)
        return held_before_spawn(data, launch_step(found, data))
    if phase_fields(found, "spawned").get("launcher") == "t3":
        # A call whose outcome is unknown, or a thread T3 no longer has (dead, no exit record: I2).
        from . import t3
        return t3.evidence(node_id, key)["state"] in ("pending", "gone")
    if launcher.receipt(key).get("exited_at") is not None or no_agent_record(key) is not None:
        return False  # an exit record, or valid no-agent evidence: reconciliation settles it (a corrupt one holds)
    return open_evidence(node_id, key) is None


def uncertain_activations(node_ids: list | None = None) -> list[str]:
    """Every node's open activation that is uncertain (see `is_uncertain`)."""
    found = []
    for node_id in (state.all_nodes() if node_ids is None else node_ids):
        key = open_activation(node_id)
        if key and is_uncertain(node_id, key):
            found.append(key)
    return found


def held_activations(node_ids: list | None = None) -> list[str]:
    """Uncertain activations, then those with unknown evidence, each once (of the given nodes, or all)."""
    found = uncertain_activations(node_ids)
    found += [k for k in ambiguous_activations(node_ids) if k not in found]
    return found + [k for k in unknown_activations(node_ids) if k not in found]


def unknown_activations(node_ids: list | None = None) -> list[str]:
    """Activations whose process evidence is unknown right now: the activation the journal holds open for
    each node, or else its current local one."""
    from . import launcher
    found = []
    for node_id in (state.all_nodes() if node_ids is None else node_ids):
        key = open_activation(node_id) or state.runtime(node_id).get("activation")
        if key and key not in found and evidence_unknown(node_id, key, launcher.receipt(key)):
            found.append(key)
    return found


def open_activation(node_id: str) -> str | None:
    """The node's newest activation that the journal holds open: reserved or spawned with no terminal
    fact. None when every activation of the node is settled."""
    newest = None
    for key, found in all_facts(_programme(node_id)).items():
        if terminal_of(found) is None and any(e.get("node") == node_id for e in found):
            if newest is None or int(key.split("-")[1]) > int(newest.split("-")[1]):
                newest = key
    return newest
