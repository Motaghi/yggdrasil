"""Conductor tick and daemon, Heimdall liveness and admission."""

from __future__ import annotations

import fcntl
import os
import signal
import time
from pathlib import Path

from . import busy, config, launcher, retries, seams, state, tracker, util
from .util import YggError


# ---------------------------------------------------------------- admission

def load_sample() -> tuple[float | None, str]:
    path = util.env("YGG_HOST_LOAD_FILE") if util.overrides_allowed() else None
    if path:
        file = Path(path)
        try:
            value = float(file.read_text().strip())
            mtime = file.stat().st_mtime
        except (OSError, ValueError):
            return None, "missing load sample"
        age = util.now().timestamp() - mtime
        if age > float(config.get("load_max_age_seconds", 300)):
            return None, "stale load sample"
        return value, ""
    try:
        return os.getloadavg()[0] / (os.cpu_count() or 1), ""
    except OSError:
        return None, "no load sample"


def running_sessions() -> int:
    """Agent sessions that may be running now: activations the journal holds open whose launcher,
    supervisor or agent is alive or unknown (fresh evidence)."""
    from . import journal, lifecycle
    count = 0
    for programme in journal.all_programmes():
        for key, found in lifecycle.all_facts(programme).items():
            if lifecycle.terminal_of(found) is not None:
                continue
            node_id = next((e.get("node") for e in found if e.get("node")), None)
            if node_id and lifecycle.open_evidence(node_id, key) in ("live", "unknown"):
                count += 1
    return count


def concurrency_admit() -> tuple[bool, str]:
    """`max_concurrent_sessions` (unset: no limit) caps how many sessions run at once."""
    limit = config.get("max_concurrent_sessions")
    if not limit:
        return True, "no session limit"
    running = running_sessions()
    if running >= int(limit):
        return False, "sessions " + str(running) + " at the limit of " + str(limit)
    return True, "sessions " + str(running) + " of " + str(limit)


def admit() -> tuple[bool, str]:
    value, problem = load_sample()
    if value is None:
        return False, problem
    threshold = float(config.get("load_threshold", 0.85))
    if value > threshold:
        return False, "load " + str(value) + " above " + str(threshold)
    return True, "load " + str(value)


def settle_delay() -> float:
    return float(config.get("settle_delay_seconds", 30))


# ---------------------------------------------------------------- liveness

def is_live(data: dict) -> bool:
    pid = data.get("pid")
    if not util.pid_alive(pid):
        return False
    started = data.get("process_start_identity") or data.get("process_started_at")
    return started is None or util.same_start(pid, started)


def heartbeat(node_id: str, data: dict):
    if data.get("cli") == "codex" and data.get("role") == "reviewer":
        # A Codex reviewer has no hooks: its heartbeat is its newest output file, else its start (AC-39 row 3).
        found = busy.output_heartbeat(data.get("activation"))
        if found is not None:
            return found
        text = ""
    else:
        text = util.read_text(state.root() / "heartbeats" / node_id).strip()
    for value in (text, data.get("started_at")):
        if value:
            try:
                return util.parse_time(value)
            except ValueError:
                continue
    return None


def heimdall_scan() -> tuple[list[str], list[dict]]:
    """Liveness pass: flag stalls and return the liveness retries due, without launching anything.

    Retries go through the tick's launch loop, so they share admission, settle delays and the per-tick cap.
    """
    reconcile()
    from . import ratatoskr
    ratatoskr.unbind_unconsumed()
    stall = float(config.get("stall_threshold_minutes", 20)) * 60
    silence = float(config.get("max_silence_minutes", 60)) * 60
    limit = int(config.get("retry_limit", 2))
    moment = util.now()
    flagged = []
    relaunch = []
    stalled_now = []
    nodes = state.all_nodes()
    for node_id in nodes:
        runtime = state.runtime(node_id)
        if runtime.get("state") not in ("running", "stalled-suspect") or not runtime.get("activation"):
            continue
        suspect = runtime.get("state") == "stalled-suspect"  # flagged once; an exit still settles it (AC-02)
        activation = runtime["activation"]
        if runtime.get("outcome") in ("uncertain", "pending"):
            continue  # held or starting: reconciled by receipt only, never retried through the dead-process path
        data = launcher.receipt(activation)
        if not data:
            launcher.mark_uncertain(node_id, activation, "receipt missing")
            continue
        if data.get("launcher") == "t3":
            continue  # a T3 session has no local process: its run is settled from T3's thread (reconcile)
        if data.get("outcome") in ("uncertain", "pending"):
            continue
        if data.get("exited_at") is not None and (data.get("exit_code") == 0 or data.get("interrupted")):
            # Done, or ended by a signal: its work is owed again at once. A crash (a signal Yggdrasil didn't send)
            # counts against retry_limit through the relaunch chain (A11.1 AC-01), checked before any launch.
            state.set_state(node_id, "idle")
            continue
        if data.get("exited_at") is not None and launcher.classify(data) == "capacity":
            # A quota exit recorded after confirmation: capacity handling, before any retry accounting.
            with util.locked():
                launcher.update_receipt(activation, **{"class": "capacity"})
                launcher.apply_outcome(node_id, activation, "capacity")
            continue
        beat = heartbeat(node_id, data)
        age = (moment - beat).total_seconds() if beat else float("inf")
        live = is_live(data)
        if live:
            if age > silence and busy.current(activation, moment) is not None:
                continue  # inside an allowed tool call that hasn't reported back (AC-02 row 3, AC-39 row 1)
            if age > silence and not suspect:
                state.set_state(node_id, "stalled-suspect")
                flagged.append(node_id)
            elif age <= silence and suspect:
                state.set_state(node_id, "running")  # activity again: no longer suspect
            continue
        if suspect and data.get("exited_at") is None:
            continue  # gone without an exit record: reconcile holds it (uncertain), never a silent retry here
        if data.get("pid") is None and data.get("process") == "none":
            continue
        if age <= stall:
            continue
        used = int(data.get("retries_used", runtime.get("retries_used", 0)) or 0)
        chain_end = retries.stalled(node_id)
        if chain_end is None and used >= limit and _spent_outside_chain(node_id, activation, data):
            chain_end = activation  # a dead run at the end of its retries that the journal's chain can't see
        if chain_end is None:
            relaunch.append({"node": node_id, "reason": "relaunch", "retries_used": used + 1,
                             "expected_activation": activation})
        elif retries.mark_stalled(node_id, chain_end, alert=False):
            stalled_now.append((node_id, chain_end))  # A11.1 AC-01: one class-user alert, work stays owed
    for node_id in flagged:
        state.alert("ygg-stall", "Stalled node: " + node_id, node=node_id, nodes=[node_id])
    for node_id in nodes:
        chain_end = retries.stalled(node_id)
        if chain_end is None:
            continue
        current = state.runtime(node_id).get("activation")
        if current and launcher.receipt(current).get("exited_at") is None and \
                launcher.receipt(current).get("outcome") not in ("aborted",):
            continue  # a run in progress
        if retries.mark_stalled(node_id, chain_end, alert=False):  # idempotent per chain
            stalled_now.append((node_id, chain_end))
    retries.alert_stalled(stalled_now)  # nodes that stall in one pass share one alert (frozen A1 AC-60)
    return flagged, relaunch


def _spent_outside_chain(node_id: str, activation: str, data: dict) -> bool:
    """A run whose spent retry budget only its receipt records: it has no exit record, or no lifecycle facts in
    the journal (so the relaunch chain of A11.1 AC-01 doesn't count it)."""
    return data.get("exited_at") is None or not _has_lifecycle(node_id, activation, data)


def heimdall_tick() -> list[str]:
    """Standalone liveness pass (`ygg heimdall tick`): retries pass admission and the per-tick cap."""
    flagged, relaunch = heimdall_scan()
    launch_queue(relaunch)
    return flagged


# ---------------------------------------------------------------- tick

def _has_lifecycle(node_id: str, activation: str, data: dict) -> bool:
    from . import lifecycle
    return bool(data.get("lifecycle")) or bool(lifecycle.facts(_programme_of(node_id), activation))


def _programme_of(node_id: str):
    return (state.all_nodes().get(node_id) or {}).get("programme")


def _reconcile_lifecycle(node_id: str, activation: str, runtime: dict, data: dict) -> None:
    """Settle an activation that has lifecycle facts: by its facts and the process evidence, never by
    elapsed time."""
    from . import lifecycle
    found = lifecycle.facts(_programme_of(node_id), activation)
    if found and lifecycle.terminal_of(found) is None and data.get("exited_at") is not None and \
            "spawned" in {e.get("phase") for e in found}:
        # The supervisor recorded the exit but not its fact (a crash in between): the record is the evidence.
        lifecycle.commit(node_id, activation, "exited", exit_code=data.get("exit_code"), from_record=True,
                         spawn_failed=bool(data.get("spawn_failed")), **launcher.exit_fields(data, activation))
        found = lifecycle.facts(_programme_of(node_id), activation)
    done = lifecycle.mirror_terminal(node_id, activation) if found else None
    if done is None and found:
        from . import t3
        if t3.is_t3(node_id, activation):
            t3.reconcile(node_id, activation)  # ruling 106: T3's own report of the thread decides
            return
    if done == "aborted":
        if runtime.get("outcome") in ("uncertain", "pending") or data.get("outcome") != "aborted":
            _resolve_aborted_uncertain(node_id, activation)
        return
    if done == "released":
        # A release that stopped after its fact committed: its local effects follow from the fact.
        from . import noderepair
        released = lifecycle.phase_fields(found, "released").get("released_as") or "failed"
        with util.locked():
            noderepair.release_effects(node_id, activation, released)
        return
    if done == "exited":
        if data.get("outcome") in ("pending", "uncertain") and data.get("exited_at") is not None:
            launcher.finalize(node_id, activation, launcher.classify(data) or "failed",
                              resolving=("pending", "uncertain"))
        elif data.get("outcome") not in ("pending", "uncertain", None):
            repair_resolved(node_id, activation)
        return
    if data.get("outcome") not in ("pending", "uncertain", None):
        # Confirmed by its launcher. With an exit record, today's liveness path takes over (a failure is a
        # retry, I9). Without one, the same evidence rules apply as for an unconfirmed activation: a dead
        # agent whose supervisor is gone is held as uncertain before any retry can admit it (I2).
        if data.get("exited_at") is None:
            seen = lifecycle.settle(node_id, activation, data, found)
            if seen in ("unsettled", "corrupt"):
                _hold_spawned(node_id, activation, seen)
            return
        if runtime.get("state") == "running" and data.get("exit_code") == 0:
            state.set_state(node_id, "idle")
        return
    seen = lifecycle.settle(node_id, activation, data, found)
    if seen in ("pending", "unknown", "aborted"):
        return
    if seen in ("unsettled", "corrupt"):
        _hold_spawned(node_id, activation, seen)
        return
    data = launcher.receipt(activation)
    if seen == "exited":
        resolving = ("pending", "uncertain")
        launcher.finalize(node_id, activation, launcher.classify(data) or "failed", resolving=resolving)
        return
    if seen == "running":
        launcher.finalize(node_id, activation, "confirmed", resolving=("pending", "uncertain"))
        return
    launcher.hold_uncertain(node_id, activation, "spawned, no agent evidence and no exit record")


def _hold_spawned(node_id: str, activation: str, seen: str) -> None:
    why = "corrupt no-agent record" if seen == "corrupt" else "spawned, no agent evidence and no exit record"
    if seen == "corrupt":
        state.alert("ygg-corrupt-evidence", "Corrupt no-agent record for " + activation + "; held as uncertain",
                    activation=activation, node=node_id)
    launcher.mark_uncertain(node_id, activation, why, allow_confirmed=True)


def settle_orphaned_reservations(nodes: dict) -> None:
    """Reservations whose node no longer points at them (a crash between the reservation fact and the
    runtime write, or a later launch): settle them from their facts so none stays open."""
    from . import journal, lifecycle
    for programme in journal.all_programmes():
        for activation, found in lifecycle.all_facts(programme).items():
            if lifecycle.terminal_of(found) is not None:
                continue
            node_id = next((e.get("node") for e in found if e.get("node")), None)
            if node_id not in nodes:
                continue
            if state.runtime(node_id).get("activation") == activation:
                continue  # settled by the per-node pass
            data = launcher.receipt(activation)
            seen = lifecycle.settle(node_id, activation, data, found)
            if seen in ("pending", "running", "unknown", "unsettled", "corrupt"):
                # The node owns this activation again (after a wipe its runtime is gone): it stays blocked,
                # and the per-node pass settles it from here on.
                _restore_ownership(node_id, activation, data)
            if seen in ("unsettled", "corrupt"):
                _hold_spawned(node_id, activation, seen)


def _restore_ownership(node_id: str, activation: str, data: dict) -> None:
    with util.locked():
        runtime = state.runtime(node_id)
        current = runtime.get("activation")
        if current and current != activation:
            other = launcher.receipt(current)
            if other.get("terminal") is None and other.get("outcome") in ("pending", "uncertain"):
                return  # it already points at another unsettled activation
        runtime.update(state="running", activation=activation, launcher=data.get("launcher", "headless"),
                       outcome=data.get("outcome") if data.get("outcome") in ("pending", "confirmed", "uncertain")
                       else "pending")
        state.save_runtime(node_id, runtime)


def _reconcile_pending(node_id: str, activation: str, runtime: dict, data: dict) -> None:
    """Resolve a startup reservation whose launching process may have exited before confirming it.

    A recorded exit or a live process is classified exactly as confirm does. A recorded process that is gone
    without an exit record, or no process at all, is classified only after the startup grace: the first as
    failed (the liveness path), the second as uncertain (held, alerted).
    """
    grace = float(config.get("startup_grace_seconds", 60))
    if not data:
        launcher.mark_uncertain(node_id, activation, "receipt missing")
        return
    recorded = data.get("outcome")
    if recorded not in ("pending", None):
        repair_resolved(node_id, activation)
        return
    outcome = launcher.classify(data)
    reserved = data.get("reserved_at") or data.get("started_at")
    try:
        age = (util.now() - util.parse_time(reserved)).total_seconds() if reserved else float("inf")
    except ValueError:
        age = float("inf")
    if outcome == "aborted":
        with util.locked():
            launcher.update_receipt(activation, outcome="aborted")
        _reconcile_pending(node_id, activation, runtime, launcher.receipt(activation))
        return
    if data.get("exited_at") is None and age <= grace:
        if outcome != "confirmed":
            return  # still inside the startup window: the launching process may yet confirm it
    if outcome is None:
        launcher.mark_uncertain(node_id, activation, "no process started within the startup grace")
        return
    launcher.finalize(node_id, activation, outcome)


def repair_resolved(node_id: str, activation: str) -> None:
    """Bring the runtime in line with a receipt that is already resolved (the runtime write was lost).

    Uses the same idempotent steps as finalize and abort: a confirmed receipt's class (capacity included)
    and an aborted receipt's reason decide the runtime, wake and journal effects.
    """
    with util.locked():
        current = state.runtime(node_id)
        data = launcher.receipt(activation)
        if current.get("activation") != activation:
            return
        if data.get("outcome") == "aborted":
            launcher.abort_effects(node_id, activation, data.get("abort_reason"))
        elif data.get("outcome") == "confirmed" and current.get("outcome") != "confirmed":
            launcher.apply_outcome(node_id, activation, launcher.receipt_outcome(data))


def _resolve_aborted_uncertain(node_id: str, activation: str) -> None:
    """An aborted receipt is definitive: no agent started. Release the node (uncertainty is derived, so
    nothing else is cleared)."""
    with util.locked():
        if launcher.receipt(activation).get("outcome") != "aborted":
            launcher.update_receipt(activation, outcome="aborted")
        launcher.abort_effects(node_id, activation, launcher.receipt(activation).get("abort_reason"))


def reconcile() -> None:
    """Resolve activations by their receipt (the idempotency key) before anything may launch.

    An uncertain activation stays held until its receipt shows a process or an exit; a pending one (a
    launch whose confirm never ran) is classified from its recorded process and exit. Outcomes are recorded
    through the same finalize step confirm uses, journal first.
    """
    launcher.finish_aborts()  # interrupted aborts are completed whatever the runtime now says
    nodes = state.all_nodes()
    settle_orphaned_reservations(nodes)
    for node_id in nodes:
        runtime = state.runtime(node_id)
        activation = runtime.get("activation")
        if not activation:
            continue
        data = launcher.receipt(activation)
        if _has_lifecycle(node_id, activation, data):
            _reconcile_lifecycle(node_id, activation, runtime, data)
            continue
        if data.get("outcome") == "aborted" and runtime.get("outcome") in ("uncertain", "pending"):
            _resolve_aborted_uncertain(node_id, activation)
            continue
        if runtime.get("outcome") == "uncertain" or data.get("outcome") == "uncertain":
            outcome = launcher.classify(data)
            if outcome == "aborted":
                _resolve_aborted_uncertain(node_id, activation)
                continue
            if outcome is None:
                if runtime.get("state") not in ("running", "stopped"):
                    with util.locked():
                        current = state.runtime(node_id)
                        if current.get("activation") == activation:
                            current.update(state="running", outcome="uncertain")
                            state.save_runtime(node_id, current)
                continue  # still uncertain: held, never relaunched
            with util.locked():
                current = state.runtime(node_id)
                if current.get("activation") != activation:
                    continue
                if launcher.receipt(activation).get("outcome") != "uncertain":
                    launcher.update_receipt(activation, outcome="uncertain")
                launcher.finalize(node_id, activation, outcome, resolving=("uncertain",))
            continue
        if runtime.get("outcome") == "pending" or data.get("outcome") == "pending":
            _reconcile_pending(node_id, activation, runtime, data)
            continue
        if runtime.get("state") == "running" and data.get("exited_at") is not None and data.get("exit_code") == 0:
            state.set_state(node_id, "idle")


def blocker(node: dict, items: list[dict], now=None, *, liveness_retry: bool = False) -> str | None:
    """Why a node that owes work is not a launch candidate, in the fixed order: process evidence (journal
    ownership first: an open activation with live or unknown evidence), an uncertain activation, a stop or
    pause interlock, a decision, the launch gate, the work-ahead gate (a team lead whose owed work is all
    withheld), then a capacity backoff (load is checked by admission, outside this). None when nothing
    blocks it. A due liveness retry (`liveness_retry`) is the node's own dead activation, so its runtime
    does not block it."""
    from . import lifecycle
    key = lifecycle.open_activation(node["id"])
    if key is not None:
        seen = lifecycle.open_evidence(node["id"], key)
        if seen in ("live", "unknown"):
            return "process"
        if not liveness_retry:
            return "uncertain-activation"  # the journal holds it open with nothing alive: held until settled
    if lifecycle.ambiguous_activations([node["id"]]):
        return "uncertain-activation"  # a stopped session that may have finished: the user settles it first
    runtime = state.runtime(node["id"])
    # A cached "stopped" state never blocks: stopping comes from the active stop intents (below), so an
    # interrupted resume cannot leave a node blocked by stale local state.
    if not liveness_retry and runtime.get("state") in ("running", "stalled", "stalled-suspect"):
        return "process"
    if retries.stalled(node["id"]) is not None:
        return "stalled"  # A11.1 AC-01, AC-02: the failure chain reached retry_limit; a resume doesn't lift it
    from . import cancel
    if cancel.reserve_check(node):
        return "cancel-interrupted"  # A11.1 AC-13: no reservation in a lane with an interrupted cancel
    if state.paused(node.get("programme")) or launcher.stopped(node):
        return "interlock"
    from . import decisions
    if decisions.blocking(node["id"], state.all_nodes()):
        return "decision"
    try:
        launcher.readiness(node)
    except YggError:
        return "gate"
    if node.get("role") == "tl":
        from . import workahead
        if workahead.screen(node["id"], list(items) + _retry_probe(runtime, liveness_retry)).blocks:
            return "work-ahead"
    moment = now or util.now()
    for item in items:
        if item.get("kind") == "capacity" and item.get("backoff_until"):
            try:
                if util.parse_time(str(item["backoff_until"])) > moment:
                    return "held-capacity"  # not before its retry time
            except ValueError:
                continue
    return None


def _retry_probe(runtime: dict, liveness_retry: bool) -> list[dict]:
    """A due liveness retry as a screening-only item: it continues its activation's packages, so the
    work-ahead gate withholds it exactly when it withholds those packages. Never part of a work set."""
    if not liveness_retry or not runtime.get("activation"):
        return []
    return [{"kind": "retry", "id": str(runtime["activation"]), "reason": "relaunch", "probe": True}]


def candidates() -> list[str]:
    from . import reducer
    nodes = state.all_nodes()
    owed = reducer.owed_all()
    ordered = sorted(owed.items(), key=lambda item: str(item[1][0].get("at") or ""))
    return [node_id for node_id, items in ordered if node_id in nodes and blocker(nodes[node_id], items) is None]


def launch_states(reasons: dict | None = None) -> dict[str, str]:
    """node -> `held-capacity`, `held-load` or `queued`, derived on every read from facts, live evidence and
    the current load sample (nothing stored). An owed node blocked only by its capacity backoff is
    held-capacity; a candidate, or a due liveness retry, that nothing else blocks is held-load when
    admission refuses on the current load and queued when it accepts (the tick cap may still defer it). A
    due liveness retry blocked by an interlock, a decision or the gate is queued (status shows the block). Admission here is the same as the conductor's: the load sample, then
    the concurrent-session cap. `reasons`, when given, receives why each held node is held."""
    from . import reducer
    nodes = state.all_nodes()
    owed = reducer.owed_all()
    found = {}
    admitted = None
    why_held = ""
    due = retries_due()
    for node_id in set(owed) | set(due):
        node = nodes.get(node_id)
        if node is None:
            continue
        retry = node_id in due
        why = blocker(node, owed.get(node_id, []), liveness_retry=retry)
        if why == "held-capacity":
            found[node_id] = why
            if reasons is not None:
                reasons[node_id] = "capacity backoff"
        elif why is None:
            if admitted is None:
                admitted, why_held = admit()
                if admitted:
                    admitted, why_held = concurrency_admit()
            found[node_id] = "queued" if admitted else "held-load"
            if not admitted and reasons is not None:
                reasons[node_id] = why_held
        elif retry and why in ("interlock", "decision", "gate", "work-ahead"):
            found[node_id] = "queued"
    return found


def launch_holds() -> dict[str, str]:
    """The held nodes of `launch_states`."""
    return {n: s for n, s in launch_states().items() if s != "queued"}


def retry_due(node_id: str) -> int | None:
    """The retry count a liveness retry of the node would carry, when one is due now (the same conditions
    the liveness pass relaunches on, read without side effects); None otherwise."""
    runtime = state.runtime(node_id)
    if runtime.get("state") != "running" or not runtime.get("activation"):
        return None
    if runtime.get("outcome") in ("uncertain", "pending"):
        return None
    data = launcher.receipt(runtime["activation"])
    if not data or data.get("outcome") in ("uncertain", "pending") or data.get("launcher") == "t3":
        return None
    if data.get("exited_at") is not None and (data.get("exit_code") == 0 or data.get("interrupted") or
                                              launcher.classify(data) == "capacity"):
        return None  # a stop or reset interrupted it: its work is owed again, it is not retried
    if retries.stalled(node_id) is not None:
        return None
    if is_live(data) or (data.get("pid") is None and data.get("process") == "none"):
        return None
    beat = heartbeat(node_id, data)
    age = (util.now() - beat).total_seconds() if beat else float("inf")
    if age <= float(config.get("stall_threshold_minutes", 20)) * 60:
        return None
    used = int(data.get("retries_used", runtime.get("retries_used", 0)) or 0)
    if used >= int(config.get("retry_limit", 2)) and _spent_outside_chain(node_id, runtime["activation"], data):
        return None
    return used + 1


def retries_due() -> dict[str, int]:
    found = {}
    for node_id in state.all_nodes():
        count = retry_due(node_id)
        if count is not None:
            found[node_id] = count
    return found


def owed_retries(node_id: str) -> int:
    """The liveness retry count a woken launch carries: that of the retry or capacity item it resumes."""
    from . import reducer
    counts = [int(i.get("retries_used") or 0) for i in reducer.owed(node_id) if i["kind"] in ("retry", "capacity")]
    return max(counts) if counts else 0


def tick_limits() -> tuple[str, int, float]:
    """Ruling D4: the per-tick launch cap type and both limits come from configuration."""
    return (str(config.get("tick_cap", "both")), int(config.get("max_launches_per_tick", 5)),
            float(config.get("tick_budget_seconds", 240)))


def cap_reached(cap: str, launched: int, elapsed: float, settle: float, max_launches: int, budget: float) -> bool:
    """Whether a tick must stop before its next launch. The first launch of a tick is never deferred.

    `count` stops once `max_launches` sessions started; `time` stops when the next launch would begin
    after `budget` seconds (elapsed plus one settle delay); `both` stops at whichever comes first.
    """
    if launched == 0:
        return False
    by_count = launched >= max_launches
    by_time = elapsed + settle > budget
    if cap == "count":
        return by_count
    if cap == "time":
        return by_time
    return by_count or by_time


def launch_refused(node_id: str, error: YggError | None) -> None:
    """Report a launch the conductor could not make: one `ygg-launch-refused` alert with the reason, again
    only when the reason changes. A pause or stop refusal is expected and not reported; a later launch of
    the node clears the record."""
    path = state.root() / "launch-refused.json"
    with util.locked():
        known = util.read_json(path, {}) or {}
        if error is None or error.code == util.EXIT_PAUSED:
            if node_id in known:
                known.pop(node_id)
                util.write_json(path, known)
            return
        if known.get(node_id) == error.message:
            return
        known[node_id] = error.message
        util.write_json(path, known)
    state.alert("ygg-launch-refused", "Launch of " + node_id + " refused: " + error.message, node=node_id,
                reason=error.message, code=error.code)


def launch_queue(queue: list[dict], started: float | None = None) -> dict:
    """Launch queued items in order under Heimdall admission, settle delays and the per-tick cap.

    Items are liveness retries (`reason: relaunch`) or woken nodes. Both count toward the cap and appear in
    `launched` once a process was created; a failed process creation uses no slot and is listed in `failed`.
    Nothing is stored for a held or deferred item: held-load and queued are derived from the load sample on
    every read. A held retry stays on the liveness path (found again by the next tick); the retry itself is
    owed from the activation's exit fact, so rehydration needs no record of it.
    """
    launched, held, failed = [], [], []
    cap, max_launches, budget = tick_limits()
    # A4.2: the time cap counts the time already spent in the tick, reconciliation and events included.
    started = time.monotonic() if started is None else started
    for index, item in enumerate(queue):
        node_id = item["node"]
        if index > 0 and launched:
            if cap_reached(cap, len(launched), time.monotonic() - started, settle_delay(), max_launches, budget):
                break  # the rest stay queued (never held-load) for the next tick
            time.sleep(settle_delay())
        ok, why = admit()
        if ok:
            ok, why = concurrency_admit()
        if not ok:
            for rest in queue[index:]:
                rest_id = rest["node"]
                if rest.get("reason") == "relaunch":
                    held.append(rest_id)
                    continue
                if state.runtime(rest_id).get("state") != "running":
                    held.append(rest_id)  # its owed work is durable; held-load is derived from the load
            break
        try:
            if item.get("reason") == "relaunch":
                result = launcher.launch(node_id, reason="relaunch", retries_used=item["retries_used"],
                                         expected_activation=item["expected_activation"])
            else:
                # A resumed retry or capacity backoff keeps its retry count (from the owed item).
                result = launcher.launch(node_id, explicit=False, retries_used=owed_retries(node_id))
        except YggError as error:
            launch_refused(node_id, error)
            continue
        launch_refused(node_id, None)
        if isinstance(result, dict) and result.get("started") is False:
            failed.append(node_id)
            continue
        launched.append(node_id)
    output = {"launched": launched, "held": held}
    if failed:
        output["failed"] = failed
    return output


def recover_records() -> None:
    """Finish what a record write implied when the process stopped right after it: a project transition
    saved with its approval, and a lane dispatch message owed by an authorisation. Each is idempotent; an
    unwritable tracker leaves it for the next tick."""
    from . import commands, ratatoskr
    for step in (commands.apply_transitions, ratatoskr.deliver_dispatches):
        try:
            step()
        except YggError:
            pass


def tick() -> dict:
    started = time.monotonic()
    state.root()
    util.atomic_write(state.root() / "conductor.heartbeat", util.iso() + "\n")
    from . import interlock, rehydrate, upgrade
    interlock.complete_stops()  # pending stops first, locally: nothing below may delay a signal
    repair = getattr(launcher, "repair_hooks", None)
    if repair is not None:
        try:
            repair()  # lane 3: the hooks folder in the safety folder, re-created against its hash (A11 AC-96)
        except YggError:
            pass
    tracker.refresh()  # a remote tracker's cache is brought up to its revision (one request when idle)
    upgrade.run(tick=True)  # legacy records upgraded, replaced stores deleted; stamps compacted hourly
    interlock.reconcile(local=False)  # persist offline intents, finish interrupted resumes
    recover_records()
    rehydrate.replay_journal()  # repairs only what local state is missing (same trusted-entry rule)
    from . import lifecycle
    lifecycle.alert_stop_holds()  # a held stop is reported once, wherever its exit was recorded
    _, relaunch = heimdall_scan()  # reconciles first
    from . import t3
    t3.finish_closes()  # closed lanes whose T3 project or worktree could not go yet
    from . import events
    events.tick()
    seams.run_steps(BEFORE_LAUNCH_STEPS)
    from . import cancel
    cancel.step()  # A11.1 AC-13: one ygg-cancel-interrupted alert per interrupted cancel
    queue = relaunch + [{"node": node_id} for node_id in candidates()]
    result = launch_queue(queue, started)
    events.after_launches()
    seams.run_steps(AFTER_LAUNCH_STEPS)
    return result


# EXTENSION POINT (A11 seam): named conductor steps, run in this order at their place in tick(). A lane fills
# only its own slot with a "module:function" reference to a function() in a file it owns. A step's refusal
# (YggError) doesn't stop the steps after it. Switched features check release.on(<switch>) inside their step.
BEFORE_LAUNCH_STEPS = (
    ("busy_ceiling", "busy:ceiling_step"),  # lane 2: busy records past busy_max_minutes fall back to the stall rules (AC-02, AC-39)
    ("reviews", "review:step"),  # lane 2: open, relaunch, hold and route package reviews; stale reviews (AC-12, AC-13,
                              #         AC-56 to AC-61)
    ("stage_close", "stageclose:step"),  # lane 2: ready-to-close, PM close task, auto close; release.on("stage_close")
                              #         (AC-14, AC-62, AC-63)
    ("merge_check", "mergecheck:tick_step"),    # lane 4: start and finish merge checks, ready-to-merge (AC-27, AC-83, AC-84)
    ("needs_l1", "stageclose:needs_l1_step"),  # lane 2: ygg-needs-l1 after needs_l1_minutes (AC-80)
)
AFTER_LAUNCH_STEPS = (
    ("alert_delivery", "alertdelivery:tick_step"),  # lane 4: deliver user-class alerts within alert_tick_budget_seconds (AC-28, AC-86,
                               #         AC-87)
    ("summary", None),         # lane 4: the hourly summary; release.on("summary") (AC-07, AC-44)
    ("dashboard", "dashboard:tick_step"),       # lane 4: write the dashboard file; release.on("dashboard") (AC-08, AC-45 to AC-48)
)


def run() -> int:
    path = state.root() / "conductor.lock"
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise YggError(util.EXIT_LOCK, "conductor lock held by another conductor (" + str(path) + ")")
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()) + "\n")
    handle.flush()
    stop = {"flag": False}

    def finish(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    # Ruling 109: under Linear the fallback tick is 30 seconds unless the user configures one.
    configured = str((config.explain_all().get("tick_seconds") or (None, "core"))[1]) != "core"
    interval = float(config.get("tick_seconds", 15)) if configured or tracker.selected() != "linear" else 30.0
    while not stop["flag"]:
        try:
            tick()
        except YggError:
            pass
        wait_between_ticks(interval, lambda: stop["flag"])
    return 0


def wait_between_ticks(interval: float, stopped) -> None:
    """Wait `interval` seconds of wall time, checking `stopped()` at least every 0.2 s. The wait ends at a
    monotonic deadline, so sleeps that run long (a throttled background service) can't stretch it."""
    deadline = time.monotonic() + interval
    while not stopped():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))
