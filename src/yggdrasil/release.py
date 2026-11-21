"""Release switches (A11 amendment 28, D2; AC-99).

The product reads the package's own `data/release.yaml`. In test mode only (`YGG_TEST_MODE=1`) it reads the
file `YGG_RELEASE_FILE` names instead, and `YGG_TEST_RELEASE_OFF` (comma-separated switch names) turns
switches off. `dashboard_serve` is on only when `dashboard` is on. A switch missing from the file, or a file
that can't be read, counts as off: a feature never ships on by accident.

Each feature's lane calls `release.on(<switch>)` at its entry point, and `release.refuse(<switch>)` where the
switched-off behaviour is a refusal (the texts below are the signed ones).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import util

PACKAGED = Path(__file__).parent / "data" / "release.yaml"
SWITCHES = ("dashboard", "dashboard_serve", "summary", "doctor", "confirm_window", "retry_pm_reset",
            "t3_live_send", "retry_cap", "stage_close")
# The command a switched-off verb names in its refusal (`ygg <command> is not in this release`, exit 3).
COMMANDS = {"dashboard": "ygg dashboard", "dashboard_serve": "ygg dashboard serve", "summary": "ygg summary",
            "doctor": "ygg doctor"}


def source() -> Path:
    named = util.env("YGG_RELEASE_FILE") if util.test_mode() else None
    return Path(named) if named else PACKAGED


def _value(raw) -> bool:
    if raw is True or raw is False:
        return raw  # an unquoted YAML on/off
    return str(raw).strip().lower() == "on"


def switches() -> dict[str, bool]:
    try:
        data = yaml.safe_load(source().read_text())
    except (OSError, yaml.YAMLError):
        data = None
    data = data if isinstance(data, dict) else {}
    values = {name: _value(data.get(name, "off")) for name in SWITCHES}
    if util.test_mode():
        for name in (util.env("YGG_TEST_RELEASE_OFF") or "").split(","):
            if name.strip() in values:
                values[name.strip()] = False
    values["dashboard_serve"] = values["dashboard_serve"] and values["dashboard"]
    return values


def on(name: str) -> bool:
    if name not in SWITCHES:
        raise ValueError("unknown release switch " + name)
    return switches()[name]


def not_in_release(name: str) -> util.YggError:
    return util.YggError(util.EXIT_REFUSED, COMMANDS[name] + " is not in this release")


def refuse(name: str) -> None:
    """Raise the switched-off refusal for a verb whose switch is off."""
    if not on(name):
        raise not_in_release(name)
