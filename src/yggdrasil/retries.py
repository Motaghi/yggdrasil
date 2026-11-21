"""The relaunch chain (A11.1 AC-01, AC-02; lane 2).

Every automatic relaunch counts against `retry_limit`: a failure (non-zero exit) and a crash (an exit by a signal
Yggdrasil didn't send) each count as a try. A signal Yggdrasil sent (a stop, a reset that kills), a capacity exit,
an aborted start and a release as aborted neither count nor reset the count; an exit 0 starts it again. When a node
has run `retry_limit` plus 1 times in a row and each run failed or crashed, it is stalled: nothing starts it again,
its work stays owed, and one class-user `ygg-stall` alert is raised for it. A resume never restarts a stalled node.

The chain is derived from the journal's activation facts, so it survives an emptied state root.
"""

from __future__ import annotations

from . import config, state, util


def _ours(key: str, end: dict) -> bool:
    if end.get("reset_kill"):
        return True
    from . import lifecycle
    try:
        return lifecycle.signal_state(str(key)) in ("intent", "sent")
    except (util.YggError, OSError, ValueError):
        return False


def outcome(key: str, record: dict) -> str | None:
    """`fail` (counts), `ok` (resets), or None (neither) for one terminal activation fact."""
    terminal, end = record.get("terminal"), record.get("end") or {}
    if terminal is None:
        return None
    if terminal != "exited" or "spawned" not in (record.get("phases") or []):
        return None  # aborted, released, or never started: neither counts nor resets
    if end.get("spawn_failed") or end.get("capacity") or end.get("ambiguous"):
        return None
    if end.get("crashed"):
        return "fail"  # lane 3's exit record: a signal Yggdrasil didn't send
    code = end.get("exit_code")
    if code == 0:
        return "ok"
    if end.get("interrupted") and _ours(key, end):
        return None  # a signal Yggdrasil sent
    return "fail"


def chain(node_id: str) -> tuple[int, str | None]:
    """(consecutive failed or crashed runs at the end of the node's history, the newest of them)."""
    from . import reducer
    found = []
    for programme_fold in reducer.folds().values():
        # `ygg node reset` writes a chain-reset fact (A7.5, the recovery path for a stalled node): only runs after
        # it count. A stop, its release and a resume write none, so they neither count nor reset (A11.1 AC-01).
        reset = programme_fold["chain_resets"].get(node_id, -1)
        for key, record in programme_fold["activations"].items():
            if record.get("node") == node_id and int(record.get("sequence", 0) or 0) > reset:
                found.append((record.get("sequence", 0), key, record))
    count, last = 0, None
    for _, key, record in sorted(found, key=lambda item: (int(item[0] or 0), str(item[1]))):
        result = outcome(key, record)
        if result == "ok":
            count, last = 0, None
        elif result == "fail":
            count, last = count + 1, key
    return count, last


def limit() -> int:
    return int(config.get("retry_limit", 2))


def stalled(node_id: str) -> str | None:
    """The newest failed activation of a node whose chain reached `retry_limit` plus 1, else None."""
    count, last = chain(node_id)
    return last if count >= limit() + 1 else None


def mark_stalled(node_id: str, key: str, *, alert: bool = True) -> bool:
    """The node shows `stalled`; one `ygg-stall` alert per chain (keyed by its newest failed activation).

    With `alert` False the alert is left to the caller (`alert_stalled`), which raises one alert for every node
    that stalled in the same pass (frozen A1 AC-60). True when this chain's alert is still due. Nothing records the
    chain as notified here: only `alert_stalled` does, after the alert is written, so a pass interrupted before its
    alert leaves the chain due for the next pass."""
    runtime = state.runtime(node_id)
    if runtime.get("state") != "stalled":
        state.set_state(node_id, "stalled")
    if _notified().get(node_id) == key:
        return False
    if alert:
        alert_stalled([(node_id, key)])
    return True


def _notified() -> dict:
    return util.read_json(state.root() / "stall-alerts.json", {}) or {}


def dedup_key(node_id: str, key: str) -> str:
    """The stable identity of one chain's stall notice: the node and the chain's newest failed activation."""
    return "stall:" + node_id + ":" + key


def _alerted_keys() -> set:
    """Every chain identity a written `ygg-stall` alert carries (its `dedup` keys)."""
    found = set()
    for entry in state.alerts():
        if entry.get("kind") == "ygg-stall":
            found.update(str(k) for k in entry.get("dedup") or [] if k)
    return found


def alert_stalled(stalled_now: list[tuple[str, str]]) -> bool:
    """One class-user `ygg-stall` alert for the chains in `stalled_now` not yet notified: a single node keeps its own
    `node` and `activation` fields; several are listed together in one alert. Exactly once per chain (A11.1 AC-01):
    under the lock, a chain whose stable identity a written alert already carries is only recorded as notified (the
    recovery of an interruption after the append), the alert is written with the identities of the rest, and they
    are recorded after it. True when an alert was written."""
    path = state.root() / "stall-alerts.json"
    with util.locked():
        known = util.read_json(path, {}) or {}
        due = [(node_id, key) for node_id, key in dict(stalled_now).items() if known.get(node_id) != key]
        if not due:
            return False
        alerted = _alerted_keys()
        recovered = [(node_id, key) for node_id, key in due if dedup_key(node_id, key) in alerted]
        due = [pair for pair in due if pair not in recovered]
        if due:
            text = " stopped after " + str(limit() + 1) + " failed runs in a row; "
            keys = [dedup_key(node_id, key) for node_id, key in due]
            if len(due) == 1:
                node_id, key = due[0]
                state.alert("ygg-stall", node_id + text + "its work stays owed", node=node_id, nodes=[node_id],
                            activation=key, escalated=True, dedup=keys)
            else:
                names = [node_id for node_id, _ in due]
                state.alert("ygg-stall", "Stalled nodes: " + ", ".join(names) + text.rstrip("; ") + " each; their "
                            "work stays owed", nodes=names, activations=[key for _, key in due], escalated=True,
                            dedup=keys)
        known.update(dict(due + recovered))
        util.write_json(path, known)
    return bool(due)
