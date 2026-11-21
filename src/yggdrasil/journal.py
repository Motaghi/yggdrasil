"""Programme journals in the tracker: in-flight state that rehydrate replays."""

from __future__ import annotations

import copy
import os
import pickle
import re
from pathlib import Path

import yaml

from . import tracker, util

_BLOCK = re.compile(r"^```yaml[ \t]*\n(.*?)^```", re.S | re.M)
# One parse per journal file while the file is provably unchanged: keyed on its device, inode, size and
# modification and change times, re-checked with a fresh stat on every read (inside a lock as well), and
# dropped on every write through this module. The parse itself is never handed out: every public reader
# returns a detached copy (copy.deepcopy, which keeps shared and recursive aliases intact).
_PARSED: dict = {}   # file -> (stat key, revision, entries, pickled form in test mode)
_TRUSTED: dict = {}  # file -> (stat key, allowed actors, trusted entries, skipped count)


def _stat_key(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _pickled(value) -> bytes | None:
    """The parse as bytes for the test-mode guard (pickle follows recursive aliases), or None if it can't."""
    try:
        return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    except (pickle.PicklingError, RecursionError, TypeError):
        return None


def _forget(programme: str | None) -> None:
    name = os.path.abspath(path_for(programme))
    _PARSED.pop(name, None)
    _TRUSTED.pop(name, None)


def _parsed(programme: str | None) -> tuple[tuple | None, int, list[dict]]:
    """(stat key, revision, entries) of the journal file, parsed at most once while it is unchanged. The
    entries are shared: read them, never change them. A missing journal is empty, with no key."""
    path = path_for(programme)
    name = os.path.abspath(path)
    try:
        key = _stat_key(os.stat(path))
    except (FileNotFoundError, NotADirectoryError):
        _PARSED.pop(name, None)
        _TRUSTED.pop(name, None)
        return None, 0, []
    cached = _PARSED.get(name)
    if cached and cached[0] == key:
        if cached[3] is not None and _pickled(cached[2]) != cached[3]:
            raise RuntimeError("a reader changed cached journal entries of " + name)
        return key, cached[1], cached[2]
    text = util.normalise_newlines(util.read_text(path))
    try:
        unchanged = _stat_key(os.stat(path)) == key
    except OSError:
        unchanged = False
    header, _ = util.split_frontmatter(text)
    value = (header or {}).get("revision")
    revision_value = value if isinstance(value, int) and not isinstance(value, bool) else 0
    found = _parse_blocks(text)
    if unchanged:  # a file that changed while it was read is parsed again next time
        _PARSED[name] = (key, revision_value, found, _pickled(found) if util.test_mode() else None)
    else:
        _PARSED.pop(name, None)
        key = None
    _TRUSTED.pop(name, None)
    return key, revision_value, found


def path_for(programme: str | None) -> Path:
    if programme:
        return tracker.root() / "initiatives" / programme / "documents" / "journal.md"
    return tracker.root() / "_ygg" / "journal.md"


def entries(programme: str | None) -> list[dict]:
    """The journal's entries, detached from the cache: a caller may change and save them."""
    return copy.deepcopy(_parsed(programme)[2])


def file_key(programme: str | None) -> tuple | None:
    """(file, stat key) of the journal as it is now, without reading it; None when it is missing. Equal keys
    mean an unchanged file, so a result derived from one keyed snapshot still holds."""
    path = path_for(programme)
    try:
        return os.path.abspath(path), _stat_key(os.stat(path))
    except (FileNotFoundError, NotADirectoryError):
        return None


def keyed_snapshot(programme: str | None) -> tuple[tuple | None, int, list[dict]]:
    """(file key, revision, detached entries) from one read; the key is None when the file changed while it
    was read (nothing derived from it may then be reused)."""
    key, revision_value, found = _parsed(programme)
    marker = (os.path.abspath(path_for(programme)), key) if key is not None else None
    return marker, revision_value, copy.deepcopy(found)


def snapshot(programme: str | None) -> tuple[int, list[dict]]:
    """(revision, entries) from one read of the journal file, so the two always describe the same state.
    The entries are detached from the cache."""
    _, revision_value, found = _parsed(programme)
    return revision_value, copy.deepcopy(found)


def _parse_blocks(text: str) -> list[dict]:
    found = []
    for block in _BLOCK.findall(text):
        try:
            value = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def revision(programme: str | None) -> int:
    """The journal's revision: it advances on every save, whatever the save changed (an append, or a clear
    in the middle), so a reader can tell any change apart from none."""
    return _parsed(programme)[1]


def save(programme: str | None, items: list[dict]) -> None:
    header = {"schema": "ygg.journal.v1", "programme": programme, "standard_version": util.STANDARD_VERSION,
              "actor": "ygg-app", "revision": revision(programme) + 1}
    text = util.render_frontmatter(header, "".join("```yaml\n" + util.dump_yaml(i) + "```\n\n" for i in items))
    _forget(programme)
    try:
        tracker.write_text(path_for(programme), text)
    finally:
        _forget(programme)


def add(programme: str | None, kind: str, **fields) -> None:
    with util.locked():
        items = entries(programme)
        entry = {"kind": kind, "at": util.iso(), "actor": "ygg-app"}
        entry.update(fields)
        items.append(entry)
        save(programme, items)


def add_many(programme: str | None, records: list[tuple[str, dict]], at: str | None = None) -> None:
    """Append several entries in one durable write, so a crash keeps all of them or none."""
    with util.locked():
        items = entries(programme)
        moment = at or util.iso()
        for kind, fields in records:
            entry = {"kind": kind, "at": moment, "actor": "ygg-app"}
            entry.update(fields)
            items.append(entry)
        save(programme, items)


def replace(programme: str | None, kind: str, match: dict, **fields) -> None:
    """Clear every uncleared `kind` entry matching `match` and append the new entry, in one durable write,
    so a crash can never leave neither (or both) on record."""
    with util.locked():
        items = entries(programme)
        moment = util.iso()
        for item in items:
            if item.get("kind") == kind and not item.get("cleared_at") and \
                    all(item.get(k) == v for k, v in match.items()):
                item["cleared_at"] = moment
        entry = {"kind": kind, "at": moment, "actor": "ygg-app"}
        entry.update(fields)
        items.append(entry)
        save(programme, items)


def clear_where(programme: str | None, predicate) -> None:
    """Clear every uncleared entry the predicate accepts, in one durable write."""
    with util.locked():
        items = entries(programme)
        moment = util.iso()
        changed = False
        for item in items:
            if not item.get("cleared_at") and predicate(item):
                item["cleared_at"] = moment
                changed = True
        if changed:
            save(programme, items)


def clear(programme: str | None, kind: str, **match) -> None:
    with util.locked():
        items = entries(programme)
        changed = False
        for item in items:
            if item.get("kind") == kind and not item.get("cleared_at") and \
                    all(item.get(k) == v for k, v in match.items()):
                item["cleared_at"] = util.iso()
                changed = True
        if changed:
            save(programme, items)


def all_programmes() -> list[str | None]:
    return [p["id"] for p in tracker.programmes()] + [None]


FORMATS = {
    "inbox": {"msg_id": re.compile(r"^m-[0-9]{4,9}$")},
    # Legacy kinds, trusted only so `upgrade` can rewrite them into their current form; nothing else reads
    # them.
    "held-launch": {},
    "uncertain": {"key": re.compile(r"^act-[0-9]{4,9}$")},
    "wake": {"wake_id": re.compile(r"^w-[0-9a-f]{12}$"), "reason": re.compile(r"^[A-Za-z0-9%._~:@-]{1,2400}$")},
    "timebox-signal": {"key": re.compile(r"^[a-z0-9][a-z0-9-]{0,62}:[0-9]{1,4}:[0-9]{1,6}$")},
    "relay-grant": {}, "morning-go-open": {}, "counter": {},
    # Reasons are percent-encoded since round 13; the legacy plain form (criteria:WP-1, message:m-0001,
    # boat:job17:done) is still trusted. Decoding is the same for both: unquote leaves a legacy reason as is.
    "event": {"event_id": re.compile(r"^e-[0-9a-f]{12}$"), "reason": re.compile(r"^[A-Za-z0-9%._~:@-]{1,2400}$")},
    "envelope-ended": {"key": re.compile(r"^EN-[0-9]{4,9}$")},
    "pause": {"scope": re.compile(r"^(\*|[a-z0-9][a-z0-9-]{0,62})$")},
    "stop": {"scope": re.compile(r"^(\*|[a-z0-9][a-z0-9-]{0,62})$")},
    "checkpoint": {"pm": re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$"), "lane": re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")},
    "safety-root-established": {"id": re.compile(r"^sf-[0-9a-f]{16}$")},
    "resume": {"scope": re.compile(r"^(\*|[a-z0-9][a-z0-9-]{0,62})$")},
    "activation": {"key": re.compile(r"^act-[0-9]{4,9}$"),
                   "phase": re.compile(r"^(reserved|spawned|exited|aborted|released|resolved)$")},
    "chain-reset": {},
    "t3-close": {"close_id": re.compile(r"^tc-[0-9a-f]{12}$"), "project": re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")},
    "worktree-close": {"close_id": re.compile(r"^wc-[0-9a-f]{12}$"), "project": re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$"),
                       "phase": re.compile(r"^(removing|removed)$")},
    "t3-project": {"project": re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$"),
                   "project_id": re.compile(r"^[0-9A-Za-z][0-9A-Za-z._:-]{0,127}$")},
}
NODE_FIELDS = ("node", "sender")


def trusted_entries(programme: str | None, raw: list[dict] | None = None) -> tuple[list[dict], int]:
    """Entries written by the app actor or an authority actor with valid ids; returns (entries, skipped).
    `raw` lets a caller filter entries it already read in one snapshot."""
    from .commands import authority_actors
    allowed = set(authority_actors()) | {"ygg-app"}
    if raw is None:
        key, _, raw = _parsed(programme)
        name = os.path.abspath(path_for(programme))
        cached = _TRUSTED.get(name)
        if key is not None and cached and cached[0] == key and cached[1] == allowed:
            return copy.deepcopy(cached[2]), cached[3]
        trusted, skipped = _filter(raw, allowed)
        if key is not None:
            _TRUSTED[name] = (key, allowed, trusted, skipped)
        return copy.deepcopy(trusted), skipped
    return _filter(raw, allowed)


def _filter(raw: list[dict], allowed: set) -> tuple[list[dict], int]:
    trusted, skipped = [], 0
    for entry in raw:
        kind = entry.get("kind")
        if entry.get("actor") not in allowed or kind not in FORMATS:
            skipped += 1
            continue
        ok = all(isinstance(entry.get(field), str) and pattern.match(entry[field])
                 for field, pattern in FORMATS[kind].items())
        for field in NODE_FIELDS:
            if field in entry and not (isinstance(entry[field], str) and util.ID_RE.match(entry[field])):
                ok = False
        if ok:
            trusted.append(entry)
        else:
            skipped += 1
    return trusted, skipped
