"""The versioned tracker standard (A1.5)."""

from __future__ import annotations

from pathlib import Path

import yaml

from . import util

PACKAGED = Path(__file__).parent / "data" / "tracker-standard.yaml"
REQUIRED = ("work_package", "worker_task", "decision", "stage", "report", "document")


def load(path: str | None = None) -> dict:
    source = path or util.env("YGG_STANDARD_FILE") or str(PACKAGED)
    try:
        data = yaml.safe_load(Path(source).read_text())
    except (OSError, yaml.YAMLError) as error:
        raise util.usage("cannot read tracker standard " + str(source) + ": " + str(error))
    if not isinstance(data, dict):
        raise util.usage("tracker standard " + str(source) + " is not a mapping")
    return data


def current() -> int:
    try:
        return int(load().get("current", 1))
    except (util.YggError, ValueError, TypeError):
        return 1


def version(number: int) -> dict:
    data = load()
    versions = data.get("versions") or {}
    return versions.get(str(number)) or versions.get(number) or {}


def lane_prefix(number: int | None = None) -> str:
    entry = version(number or current())
    return str((entry.get("work_package") or {}).get("label_prefix", "lane:"))


def check(path: str | None = None) -> str:
    data = load(path)
    versions = data.get("versions") or {}
    if str(data.get("current")) not in {str(k) for k in versions}:
        raise util.usage("tracker standard: current version " + str(data.get("current")) + " has no entry")
    for key, entry in versions.items():
        for section in REQUIRED:
            if section not in (entry or {}):
                raise util.usage("tracker standard version " + str(key) + " is missing section '" + section + "'")
    return "tracker standard ok (current " + str(data.get("current")) + ")\n"
