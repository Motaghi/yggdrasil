"""The safety folder: stop and pause interlocks, revocation and no-agent records, and the locks.

It lives outside the state root, so a full state-root wipe leaves it alone: an interlock written while the
tracker was down still holds after the wipe. It is created on first use, durably, and recorded in the
global journal (`safety-root-established`); once established, a missing or unreadable folder is an error
for admission and never reads as "no interlock".
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from . import util

SUBDIRS = ("interlocks", "revoked", "no-agent", "signalled", "locks")
ID_FILE = "SAFETY_ROOT_ID"
ESTABLISHED = "safety-root-established"


def root() -> Path:
    """`YGG_SAFETY_ROOT` under the test-mode rule (no managed session), else a sibling of the state root."""
    override = util.env("YGG_SAFETY_ROOT")
    if override and util.test_mode() and util.env("YGG_NODE") is None and util.env("YGG_ACTIVATION") is None:
        return Path(override)
    state = util.state_root()
    return state.parent / (state.name + ".safety")


def layout() -> Path:
    """The folder and its subfolders, created idempotently with every new directory entry flushed."""
    base = root()
    for name in SUBDIRS:
        util.ensure_dir(base / name)
    return base


def folder_id() -> str | None:
    text = util.read_text(root() / ID_FILE).strip()
    return text or None


def _recorded_id() -> str | None:
    """The folder id the global journal records: trusted entries only (app actor, valid format), cleared
    entries skipped. More than one distinct id is a conflict and fails closed."""
    from . import journal
    entries, _ = journal.trusted_entries(None)
    ids = {e["id"] for e in entries if e.get("kind") == ESTABLISHED and not e.get("cleared_at")}
    if len(ids) > 1:
        raise util.YggError(util.EXIT_REFUSED, "safety folder record conflict: the journal names " +
                            ", ".join(sorted(ids)))
    return next(iter(ids), None)


_BOOTSTRAPPED: set = set()


def bootstrap() -> None:
    """Once per process and folder, when the journal lock is first taken (outside the creation lock): make
    sure the folder's establishment fact is committed. A tracker that cannot be written now leaves it for
    later; admission refuses until it is committed."""
    key = str(root())
    if key in _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED.add(key)
    try:
        ensure_established()
    except util.YggError:
        pass


def ensure_established() -> str:
    """First use: create the folder durably, write its id, and record it in the global journal. Each step
    is idempotent; a tracker that cannot be written leaves the fact for a later call. Never called while
    the creation lock is held (it may write the journal)."""
    from . import journal
    layout()
    current = folder_id()
    recorded = _recorded_id()
    if current is None and recorded is None:
        current = "sf-" + uuid.uuid4().hex[:16]
        util.atomic_write(root() / ID_FILE, current + "\n")
    if current is not None and recorded is None:
        try:
            journal.add(None, ESTABLISHED, id=current)
        except util.YggError:
            pass  # the tracker is not writable now; the fact is committed by a later call
    return current or ""


def check_admission() -> None:
    """Admission fails closed unless the journal holds a committed establishment fact naming this folder:
    a missing, unreadable or different folder, or a fact that could not be committed yet, refuses."""
    recorded = _recorded_id()
    if recorded is None:
        ensure_established()
        recorded = _recorded_id()
    if recorded is None:
        raise util.YggError(util.EXIT_REFUSED, "safety folder " + str(root()) +
                            " is not established in the journal yet")
    try:
        current = (root() / ID_FILE).read_text().strip()
    except OSError:
        current = None
    if current != recorded:
        raise util.YggError(util.EXIT_REFUSED, "safety folder " + str(root()) + " missing or unreadable")


def _scope_dir(scope: str | None) -> Path:
    return root() / "interlocks" / (scope or "*")


def write_marker(kind: str, scope: str | None) -> str:
    """Write a fresh stop or pause intent marker, flushed with its directory, and return its id. The
    caller holds the creation lock."""
    intent = "si-" + uuid.uuid4().hex[:16]
    directory = _scope_dir(scope)
    util.ensure_dir(directory)
    util.atomic_write(directory / (intent + ".json"),
                      json.dumps({"kind": kind, "scope": scope or "*", "intent": intent,
                                  "created_at": util.iso()}) + "\n")
    return intent


def publish_marker(kind: str, scope: str | None) -> str:
    """Write a stop or pause intent marker under the creation lock (local files only) and return its id."""
    with util.locked(util.CREATION_LOCK):
        return write_marker(kind, scope)


def markers(scope: str | None = None) -> list[dict]:
    """Intent markers, all scopes or one scope."""
    base = root() / "interlocks"
    found = []
    directories = [_scope_dir(scope)] if scope is not None else sorted(p for p in base.glob("*") if p.is_dir())
    for directory in directories:
        for path in sorted(directory.glob("si-*.json")):
            data = util.read_json(path, None)
            if isinstance(data, dict):
                found.append(dict(data, path=str(path)))
    return found


def marker_ids(scope: str | None = None) -> list[str]:
    """The intent ids of the markers present now, read under the creation lock (a snapshot for resume)."""
    with util.locked(util.CREATION_LOCK):
        return [str(m.get("intent")) for m in markers(scope) if m.get("intent")]


def remove_markers(intents: list[str]) -> list[str]:
    """Remove exactly the markers whose intent ids are listed (those a committed resume clears), under the
    creation lock. A marker published after the resume's snapshot is never touched."""
    removed = []
    wanted = set(intents)
    with util.locked(util.CREATION_LOCK):
        for item in markers():
            if item.get("intent") in wanted:
                Path(item["path"]).unlink(missing_ok=True)
                util.fsync_dir(Path(item["path"]).parent)
                removed.append(str(item["intent"]))
    return removed


def lock_inode(name: str) -> int:
    return os.stat(layout() / "locks" / (name + ".lock")).st_ino
