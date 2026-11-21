"""Emergency stop (A1.6): a local safety interlock first, tracker persistence last."""

from __future__ import annotations

from . import interlock, safety, state, tracker, util


def stop(scope: str | None) -> str:
    """Steps, in order (none of 1 to 3 waits on the tracker):

    1. under the creation lock, write the intent marker;
    2. still under it, choose targets from live evidence (activation files and the orphan scan);
    3. signal them, escalating to SIGKILL after `stop_grace_seconds`; then complete older stop intents;
    4. commit `stop {scope, intent}` to the journal, waiting at most 5 s for the journal lock, then persist
       older offline intents and finish interrupted resumes.

    A failed step 4 exits 8 with `tracker write failed`; a busy journal exits 8 with `tracker write pending`
    (reconciliation persists the intent later)."""
    state.check_user_only("stop")
    if scope is not None:
        util.check_id(scope, "scope")
    errors: list[str] = []
    unresolved: list = []
    prepared = interlock.prepare(scope)  # supplemental only: an unreadable tracker is not a store error
    with util.locked(util.CREATION_LOCK):
        intent = safety.write_marker("stop", scope)
        targets = interlock.collect_targets(prepared, errors, scope, unresolved)
    from . import faults
    faults.boundary("before-signal")  # the marker is written, no signal sent yet
    live = interlock.signal_targets(targets)
    locks = interlock.release_locks(targets)  # each stopped agent's own stale lane lock, once its group is gone
    # No cached state is written: status and admission derive "stopped" from the active intent.
    interlock.complete_stops(exclude=intent)  # older stop intents: local signals, before any tracker write
    message = "stopped " + str(len(live)) + " session(s)"
    if locks:
        print("\n".join(locks))
    problem = None
    try:
        # Every journal and tracker write of the stop happens inside one bounded wait for the journal lock
        # (re-entrant below), so neither the checkpoints nor reconciliation can wait on it without bound.
        with util.locked(timeout=interlock.JOURNAL_WAIT_SECONDS):
            from . import journal
            home = scope if scope and tracker.programme(scope) is not None else None
            journal.add(home, "stop", scope=scope or "*", intent=intent)
            try:
                _checkpoints(targets)
            except (util.YggError, OSError, ValueError):
                problem = "tracker write failed"
            interlock.reconcile(local=False)
    except util.LockTimeout:
        problem = "tracker write pending"
    except (util.YggError, OSError, ValueError):  # an unreachable tracker never undoes the signals
        problem = "tracker write failed"
    if unresolved:
        # Never signalled: the stop marker keeps the node blocked, and reconciliation retries the stop.
        message += "; identity unresolved for " + ", ".join(key + " pid " + str(pid) for key, pid in unresolved)
    if problem:
        detail = ("; state store error: " + "; ".join(dict.fromkeys(errors))) if errors else ""
        raise util.YggError(util.EXIT_TRACKER, message + "; " + problem + detail)
    if unresolved:
        raise util.YggError(util.EXIT_UNCERTAIN, message)
    if errors:
        raise util.YggError(util.EXIT_USAGE, message + "; state store error: " + "; ".join(dict.fromkeys(errors)))
    return message + "\n"


def _checkpoints(targets: list) -> None:
    """A stopped checkpoint on each stopped node's project (tracker writes, after the stop is journalled)."""
    nodes = state.all_nodes()
    done = set()
    for node_id, activation, _, _ in targets:
        node = nodes.get(node_id) or {}
        if node_id in done or not node.get("project"):
            continue
        done.add(node_id)
        header = {"kind": "checkpoint", "node": node_id, "reason": "stopped", "activation": activation,
                  "standard_version": util.STANDARD_VERSION, "actor": "ygg-app", "created_at": util.iso()}
        with util.locked():
            tracker.write_update(node["project"], "-checkpoint-" + node_id,
                                 util.render_frontmatter(header, "Stopped by the user.\n"))
