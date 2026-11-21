"""Plugin discovery through entry point groups (Amendment A3)."""

from __future__ import annotations

import sys
from importlib import metadata

from . import util

GROUPS = ("trackers", "launchers", "alert_forwarders")
SINGULAR = {"trackers": "tracker", "launchers": "launcher", "alert_forwarders": "alert_forwarder"}
SUPPORTED_API = {1}
BUILTINS = {"trackers": ("files", "linear"), "launchers": ("headless", "t3"), "alert_forwarders": ("jsonl", "auto")}


class Builtin:
    api_version = 1


def _extend_path() -> None:
    from . import config
    paths = list(config.get("plugin_paths") or [])
    for path in paths:
        if path and str(path) not in sys.path:
            sys.path.append(str(path))


def entries(group: str) -> list:
    """The entry points of a group, one per name. A name registered by two distributions is refused, so a
    second plugin can never silently replace the first."""
    _extend_path()
    found: dict = {}
    owners: dict = {}
    for entry in metadata.entry_points(group="yggdrasil." + group):
        owner = _distribution(entry)
        owners.setdefault(entry.name, set()).add(owner)
        found.setdefault(entry.name, entry)
    for name, names in owners.items():
        if len(names) > 1:
            raise util.usage("plugin name " + name + " is registered twice in yggdrasil." + group + " (" +
                             ", ".join(sorted(names)) + ")")
    return list(found.values())


def _distribution(entry) -> str:
    dist = getattr(entry, "dist", None)
    return dist.metadata["Name"] if dist is not None and dist.metadata else "unknown"


def listing() -> list[dict]:
    items = []
    for group in GROUPS:
        names = set()
        for entry in entries(group):
            names.add(entry.name)
            try:
                version = getattr(entry.load(), "api_version", None)
            except Exception:  # a broken plugin is listed without a version
                version = None
            items.append({"group": group, "name": entry.name, "distribution": _distribution(entry),
                          "api_version": version})
        for name in BUILTINS[group]:
            if name not in names:
                items.append({"group": group, "name": name, "distribution": "yggdrasil", "api_version": 1})
    return items


def available(group: str) -> list[str]:
    return sorted(set(BUILTINS[group]) | {e.name for e in entries(group)})


def check(group: str, name: str) -> str | None:
    """Return a problem message for the selected plugin, or None. Imports only the selected plugin. Name
    collisions are checked first, for built-in selections too, without importing anything."""
    found = entries(group)
    if name in BUILTINS[group]:
        return None
    selected = [e for e in found if e.name == name]
    if not selected:
        return "unknown " + SINGULAR[group] + " '" + name + "'; available: " + ", ".join(available(group))
    try:
        version = getattr(selected[0].load(), "api_version", None)
    except Exception as error:
        return "plugin " + name + ": cannot be imported (" + str(error) + ")"
    if version not in SUPPORTED_API:
        return "plugin " + name + ": api_version " + str(version) + " not supported"
    return None


def load(group: str, name: str):
    """The discovered (non-built-in) plugin object for a name, after the same checks as validation."""
    problem = check(group, name)
    if problem:
        raise util.usage(problem)
    selected = [e for e in entries(group) if e.name == name]
    return selected[0].load() if selected else None


def show(as_json: bool) -> str:
    import json
    items = listing()
    if as_json:
        return json.dumps(items) + "\n"
    return "".join(i["group"] + " " + i["name"] + " (" + i["distribution"] + ", api " + str(i["api_version"]) +
                   ")\n" for i in items)
