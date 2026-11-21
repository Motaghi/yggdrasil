"""Owed-retry selection: linear in the number of attempts, selecting exactly what the pairwise rule selects."""

import random
import time

from yggdrasil import reducer


def _attempt(node, sequence, work, exit_code=1, **extra):
    record = {"node": node, "sequence": sequence, "terminal": "exited", "phases": ["reserved", "spawned"],
              "end": {"exit_code": exit_code}, "retries_used": 0}
    if work is not None:
        record["work"] = work
    record.update(extra)
    return record


def _fold(activations):
    return {"activations": activations, "chain_resets": {}}


def _reference(fold, limit):
    """The pairwise rule: an attempt that ran owes its retry unless any later attempt that ran settles it."""
    found = []
    ran = {}
    for key, record in fold["activations"].items():
        end = record.get("end") or {}
        ran_nothing = record.get("terminal") == "aborted" or end.get("released_as") == "aborted" or \
            bool(end.get("spawn_failed")) or bool(end.get("interrupted")) or \
            (bool(end.get("ambiguous")) and record.get("resolution") != "failed")
        if record.get("terminal") is None and "spawned" not in record.get("phases", []):
            ran_nothing = True
        if record.get("node") and not ran_nothing:
            ran.setdefault(record["node"], []).append((key, record))
    for node, attempts in ran.items():
        attempts.sort(key=lambda pair: pair[1]["sequence"])
        for index, (key, record) in enumerate(attempts):
            if not any(reducer._supersedes(later, key) for _, later in attempts[index + 1:]):
                found += reducer._retry_owed(fold, node, key, record, limit)
    return found


def test_selection_at_8000_attempts_is_fast():
    # Worst case for a pairwise scan: every later attempt carries a retry list that names nobody.
    activations = {"act-%05d" % n: _attempt("tl1", n, {"retries": []}) for n in range(8000)}
    started = time.perf_counter()
    owed = reducer._relaunches(_fold(activations), limit=10)
    elapsed = time.perf_counter() - started
    assert len(owed) == 8000
    assert elapsed < 0.25, elapsed


def test_selection_matches_the_pairwise_rule():
    rng = random.Random(20261118)
    for _ in range(300):
        activations = {}
        keys = []
        for n in range(rng.randint(1, 14)):
            key = "act-%04d" % n
            shape = rng.random()
            if shape < 0.15:
                work = None                                   # no work set: settles every earlier retry
            elif shape < 0.25:
                work = {"messages": []}                       # no retry list: settles every earlier retry
            else:
                work = {"retries": rng.sample(keys, min(len(keys), rng.randint(0, 2)))}
            extra = {}
            roll = rng.random()
            if roll < 0.1:
                extra = {"terminal": "aborted"}
            elif roll < 0.2:
                extra = {"end": {"exit_code": 1, "interrupted": True}}
            elif roll < 0.25:
                extra = {"terminal": None, "phases": ["reserved"]}
            elif roll < 0.3:
                extra = {"end": {"exit_code": 1, "ambiguous": True}, "resolution": rng.choice(["failed", None])}
            node = rng.choice(["tl1", "tl2"])
            activations[key] = _attempt(node, n, work, exit_code=rng.choice([0, 1, 1]), **extra)
            keys.append(key)
        fold = _fold(dict(rng.sample(sorted(activations.items()), len(activations))))
        assert reducer._relaunches(fold, 3) == _reference(fold, 3)
