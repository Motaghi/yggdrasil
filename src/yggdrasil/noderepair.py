"""User-only repair verbs for a node (Amendment A7.5): `ygg node release` and `ygg node reset`."""

from __future__ import annotations

import os
import signal
import time

from . import launcher, state, util
from .util import YggError

STALLED_STATES = ("stalled", "stalled-suspect")


def _user_only(verb: str) -> None:
    """Refused for any session marker (YGG_NODE, including l1, or YGG_ACTIVATION) and for every session
    `state.managed_session` counts as managed, such as one running inside a managed worktree with both markers
    dropped."""
    if util.env("YGG_NODE") is not None or util.env("YGG_ACTIVATION") is not None or state.managed_session():
        raise YggError(util.EXIT_REFUSED, verb + " is user-only")


def _uncertain_activations(node_id: str) -> list[str]:
    """The node's spawned activations the journal holds open (from the facts, never a stored mirror), and its
    reservations that its confirm step held as uncertain before any `spawned` fact (A11 AC-43 row 3: kept held,
    never aborted on their own, so the user's release is how they settle, A7.5); the evidence check that follows
    refuses any with live or unknown process evidence."""
    from . import lifecycle
    programme = (state.all_nodes().get(node_id) or {}).get("programme")
    keys = []
    for key in _open_keys(node_id):
        phases = {e.get("phase") for e in lifecycle.facts(programme, key)}
        if "spawned" in phases or launcher.receipt(key).get("outcome") == "uncertain":
            keys.append(key)
    return keys + [k for k in lifecycle.ambiguous_activations([node_id]) if k not in keys]


def _open_keys(node_id: str) -> list[str]:
    """The node's activations the journal holds open (reserved or spawned, no terminal fact)."""
    from . import lifecycle
    return [key for key, found in lifecycle.all_facts((state.all_nodes().get(node_id) or {}).get("programme")).items()
            if lifecycle.terminal_of(found) is None and any(e.get("node") == node_id for e in found)]


def _blocking_evidence(node_id: str, key: str) -> str | None:
    """`live` or `unknown` when process evidence forbids settling the activation, else None."""
    from . import lifecycle
    return lifecycle.open_evidence(node_id, key)


def release(node_id: str, outcome: str) -> str:
    """Settle the node's uncertain activation as `failed` or `aborted` when its process evidence is dead.
    Order: evidence check, revocation record (creation lock), then the `released` fact under the journal
    lock with first-terminal-wins, then local effects. An activation another terminal fact already settled
    keeps that outcome."""
    _user_only("node release")
    if outcome not in ("failed", "aborted"):
        raise util.usage("--as must be failed or aborted")
    node = state.require_node(node_id)
    from . import lifecycle
    if launcher.live_activations(node_id):
        raise YggError(util.EXIT_REFUSED, node_id + " has a live activation; reset it with --kill first")
    keys = _uncertain_activations(node_id)
    if not keys:
        raise YggError(util.EXIT_REFUSED, node_id + " has no uncertain activation to release")
    for key in keys:
        block = _blocking_evidence(node_id, key)
        if block == "live":
            raise YggError(util.EXIT_REFUSED, node_id + " has a live activation; reset it with --kill first")
        if block == "unknown":
            raise YggError(util.EXIT_REFUSED, node_id + ": process evidence is unknown; try again")
    unspawned = {key for key in keys
                 if "spawned" not in {e.get("phase") for e in lifecycle.facts(node.get("programme"), key)}}
    for key in keys:
        if lifecycle.facts(node.get("programme"), key):
            lifecycle.revoke(key, "released by the user")
    ambiguous = set(lifecycle.ambiguous_activations([node_id]))
    with util.locked():
        # Under the journal lock, after the revocation: a supervisor that recorded `spawned` before the revocation may
        # have made its agent (the revocation stops only a later one), so a release that raced it is refused, and the
        # fresh process evidence is read again (A7 I1, A7.5).
        for key in keys:
            phases = {e.get("phase") for e in lifecycle.facts(node.get("programme"), key)}
            block = _blocking_evidence(node_id, key)
            if (key in unspawned and "spawned" in phases) or block == "live":
                raise YggError(util.EXIT_REFUSED, node_id + " has a live activation; reset it with --kill first")
            if block == "unknown":
                raise YggError(util.EXIT_REFUSED, node_id + ": process evidence is unknown; try again")
        for key in keys:
            if key in ambiguous:
                # It already exited after a stop reached it: the user's call settles its work (failed: done,
                # aborted: offered again). Recorded once.
                if key in lifecycle.ambiguous_activations([node_id]):
                    lifecycle.commit(node_id, key, "resolved", outcome=outcome)
                continue
            if not lifecycle.commit(node_id, key, "released", released_as=outcome):
                lifecycle.mirror_terminal(node_id, key)
                continue  # another terminal outcome stands (I3)
            release_effects(node_id, key, outcome)
    return "released " + node_id + " as " + outcome + "\n"


def release_effects(node_id: str, key: str, outcome: str) -> None:
    """The local effects of a committed `released` fact (caller holds the lock): the record settles as
    failed or aborted and the runtime returns to idle (a stopped node stays stopped; admission follows the
    active intents). Idempotent; recovery repeats it from the fact when a release stopped after its commit."""
    record = launcher.receipt(key)
    if record.get("released_as") != outcome or record.get("exited_at") is None:
        moment = util.iso()
        record.update(released_as=outcome, released_at=record.get("released_at") or moment, abort_step=None)
        if record.get("exited_at") is None:
            record.update(exited_at=moment, exit_code=None)
        if outcome == "aborted":
            record.update(outcome="aborted", phase="aborted", abort_reason="released by the user")
        else:
            record.update(outcome="confirmed", **{"class": "failed"})
        launcher.save_receipt(key, record)
    runtime = state.runtime(node_id)
    if runtime.get("activation") in (None, key):
        changed = dict(runtime, outcome="aborted" if outcome == "aborted" else "confirmed")
        if changed.get("state") != "stopped":
            changed["state"] = "idle"
        if changed != runtime:
            state.save_runtime(node_id, changed)


def _kill(pid: int) -> None:
    """Kill a verified process, and its group only when the group is provably its own (it leads it)."""
    try:
        own_group = os.getpgid(pid) == pid
    except (ProcessLookupError, PermissionError, OSError):
        own_group = False
    try:
        if own_group:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _verified_supervisor(key: str, spawned: dict) -> tuple[int, str] | None:
    """The supervisor of `key` as a kill target, only when the journal's pid matches the one the supervisor
    recorded locally, the start time matches, and the process's real environment names this activation
    and state root."""
    from . import lifecycle
    record = launcher.receipt(key)
    pid = spawned.get("supervisor_pid")
    started = record.get("supervisor_started_at")
    if not isinstance(pid, int) or record.get("supervisor_pid") != pid or not started or \
            started != spawned.get("supervisor_started_at"):
        return None
    if lifecycle.evidence(pid, started) != "alive":
        return None
    environment = lifecycle.process_environment(pid)
    roots = {str(util.state_root()), str(util.state_root().resolve())}
    if not environment or environment.get("YGG_SUPERVISOR_FOR") != key or environment.get("YGG_STATE_ROOT") not in roots:
        return None
    exact = util.process_start(pid)  # verified by its environment: later checks use the exact identity
    return (pid, exact) if exact else None


def _reset_targets(node_id: str, open_keys: list[str]) -> tuple[list[tuple[int, str | None]], list[str], list[str]]:
    """(kill targets, activations with an unverifiable supervisor, activations with unknown evidence).

    Targets are the node's live recorded agents, each open activation's verified live supervisor, and any
    unrecorded agent of an open activation found by the orphan scan (matched by its real environment and
    nonce). A recorded agent or supervisor whose evidence is unknown, or an orphan scan that cannot answer,
    makes the activation unknown."""
    from . import lifecycle
    programme = (state.all_nodes().get(node_id) or {}).get("programme")
    targets: list[tuple[int, str | None]] = []
    unverified: list[str] = []
    unknown: list[str] = []
    for record in launcher.live_activations(node_id):
        seen = lifecycle.evidence(record["pid"], lifecycle.recorded_start(record))
        if seen == "unknown":
            unknown.append(str(record.get("activation")))  # a pid whose start time cannot be matched
        elif seen == "alive":
            exact = lifecycle.signal_identity(record["pid"], str(record.get("activation")), record)
            if exact is None:
                unverified.append(str(record.get("activation")))  # matches only in some zone: held
            else:
                targets.append((int(record["pid"]), exact))
    for key in open_keys:
        found = lifecycle.facts(programme, key)
        data = launcher.receipt(key)
        if data.get("pid") is not None and data.get("exited_at") is None:
            seen = lifecycle.evidence(data.get("pid"), lifecycle.recorded_start(data))
            if seen == "unknown":
                if key not in unknown:
                    unknown.append(key)
                continue
            if seen == "alive" and all(t[0] != int(data["pid"]) for t in targets):
                exact = lifecycle.signal_identity(data["pid"], key, data)
                if exact is None:
                    if key not in unverified:
                        unverified.append(key)
                    continue
                targets.append((int(data["pid"]), exact))
        spawned = lifecycle.phase_fields(found, "spawned")
        if not spawned:
            continue
        supervisor = lifecycle.evidence(spawned.get("supervisor_pid"), spawned.get("supervisor_started_at"))
        if supervisor == "unknown":
            unknown.append(key)
            continue
        if supervisor == "alive":
            verified = _verified_supervisor(key, spawned)
            if verified is None:
                unverified.append(key)
                continue
            targets.append(verified)
        if data.get("pid") is None:
            status, pids = lifecycle.orphan_agents(key, spawned.get("agent_nonce"), spawned.get("supervisor_started_at"))
            if status == "unknown":
                unknown.append(key)
                continue
            for pid in pids:
                targets.append((pid, util.process_start(pid)))
    return targets, unverified, unknown


def reset(node_id: str, kill: bool = False) -> str:
    """Return a stalled or stalled-suspect node to idle, never rewriting a terminal outcome.

    Order: under the creation lock, collect every live target (agents of the node, the open activation's
    supervisor, an orphan agent) and write revocation records; release it; kill the targets and verify their
    death; then, under the journal lock, commit `exited` (class `reset-kill`) for the open activation when it
    has no terminal fact, and `chain-reset`."""
    _user_only("node reset")
    node = state.require_node(node_id)
    from . import journal, lifecycle
    runtime = state.runtime(node_id)
    if runtime.get("state") not in STALLED_STATES:
        raise YggError(util.EXIT_REFUSED, node_id + " is not stalled; reset applies to stalled or "
                                                    "stalled-suspect nodes")
    programme = node.get("programme")
    open_keys = _open_keys(node_id)
    with util.locked(util.CREATION_LOCK):
        targets, unverified, unknown = _reset_targets(node_id, open_keys)
        if (targets or unverified or unknown) and not kill:
            raise YggError(util.EXIT_REFUSED, node_id + " has a live activation; use --kill")
        if unknown:
            # Evidence that cannot be read is never treated as dead: nothing is signalled or settled.
            raise YggError(util.EXIT_UNCERTAIN, node_id + ": process evidence for " + ", ".join(unknown) +
                           " is unknown; nothing killed")
        if unverified:
            # A pid named only by tracker data, or one that no longer matches the local record, is never
            # signalled: the node stays held and the mismatch is reported.
            raise YggError(util.EXIT_UNCERTAIN, node_id + ": the supervisor of " + ", ".join(unverified) +
                           " could not be verified (pid, start time and environment must match); nothing killed")
        attempts = {}
        for key in open_keys:
            lifecycle.write_local(lifecycle.revoked_path(key), {"activation": key, "reason": "reset",
                                                                "at": util.iso()})
            attempts[key] = lifecycle.mark_signalled(key, "reset")  # whoever records the exit, it is held
    for pid, _ in targets:
        _kill(pid)
    for key in open_keys:
        lifecycle.mark_sent(key, "reset", attempts[key])  # killed outright (SIGKILL)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(lifecycle.evidence(p, st) == "alive" for p, st in targets):
        time.sleep(0.05)
    # Settled only once every relevant process is verified dead: the killed targets, and a fresh scan of
    # the node's recorded agents, supervisors and unrecorded (orphan) agents finds nothing alive or unknown.
    remaining, unverified, unknown = _reset_targets(node_id, open_keys)
    if any(lifecycle.evidence(p, st) != "dead" for p, st in targets) or remaining or unverified or unknown:
        raise YggError(util.EXIT_UNCERTAIN, node_id + ": the activation processes could not be verified ended")
    with util.locked():
        moment = util.iso()
        for key in open_keys:
            lifecycle.commit(node_id, key, "exited", exit_code=-int(signal.SIGKILL), reset_kill=True)
        journal.add(programme, "chain-reset", node=node_id)
        for pid, _ in targets:
            for record in [launcher.receipt(k) for k in open_keys]:
                if record.get("pid") == pid and record.get("exited_at") is None:
                    launcher.update_receipt(record["activation"], exited_at=moment, exit_code=-int(signal.SIGKILL),
                                            reset_at=moment, **{"class": "reset-kill"})
        current = runtime.get("activation")
        if current and launcher.receipt(current).get("exited_at") is None:
            launcher.update_receipt(current, exited_at=moment, exit_code=None, reset_at=moment)
        runtime = state.runtime(node_id)
        runtime["state"] = "idle"
        runtime["retries_used"] = 0
        state.save_runtime(node_id, runtime)
    return "reset " + node_id + "\n"
