"""`ygg doctor [--json]` (A11 lane 4: AC-24; release switch `doctor`).

Says which tracker and launcher are active and which of the built-in options are ready, with a reason when one
isn't, whether the lane check has a leak denylist, and how a Codex session would sign in (`codex_sign_in`, from the
environment that session would get; never a key or a token). Read-only: it makes no network call and raises no
alert.
"""

from __future__ import annotations

import json

from . import config, release, util

DENYLIST_MISSING = "Leak check: no denylist set; lane commits are checked for secret-shaped tokens only."


def _linear_ready() -> tuple[bool, str]:
    """Whether a Linear token or app credential is stored. Keychain entries are looked up without reading the
    secret (no prompt), and nothing here raises an alert."""
    import subprocess
    try:
        from .linear import auth
        store = auth.store()
        if isinstance(store, auth.KeychainStore):
            if util.test_mode():
                return False, "no Linear token"  # test runs never read the host's real Keychain
            for account in ("access_token", "client_id"):
                found = subprocess.run(["security", "find-generic-password", "-s", store.service, "-a", account],
                                       capture_output=True, text=True, timeout=5, check=False)
                if found.returncode == 0:
                    return True, ""
        elif store.get("access_token") or (store.get("client_id") and store.get("client_secret")):
            return True, ""
    except Exception:  # noqa: BLE001 (any failure to read the store means no usable token)
        return False, "no Linear token"
    return False, "no Linear token"


def _t3_ready() -> tuple[bool, str]:
    from . import t3
    return (True, "") if t3.binary() else (False, "t3 command not found")


def _option(name: str, ready: bool, reason: str) -> dict:
    return {"name": name, "ready": True} if ready else {"name": name, "ready": False, "reason": reason}


def report() -> dict:
    from . import codexsignin, tracker
    trackers = [_option("files", True, ""), _option("linear", *_linear_ready())]
    launchers = [_option("headless", True, ""), _option("t3", *_t3_ready())]
    denylist = config.get("leak_denylist") or (config.profile() or {}).get("leak_denylist")
    return {"tracker": {"active": tracker.selected(), "options": trackers},
            "launcher": {"active": str(config.get("launcher", "headless") or "headless"), "options": launchers},
            "leak_check": {"denylist": util.redact(str(util.redact_profile(denylist))) if denylist else None},
            "codex_sign_in": codexsignin.doctor_report()}


def show(as_json: bool) -> str:
    release.refuse("doctor")
    data = report()
    if as_json:
        return json.dumps(data, indent=2) + "\n"
    lines = []
    for part, title in (("tracker", "Tracker"), ("launcher", "Launcher")):
        lines.append(title + ": " + data[part]["active"] + " (active)")
        for option in data[part]["options"]:
            lines.append("  " + option["name"] + ": " + ("ready" if option["ready"] else
                                                          "not ready, " + option["reason"]))
    if data["leak_check"]["denylist"]:
        lines.append("Leak check: denylist " + data["leak_check"]["denylist"] + ".")
    else:
        lines.append(DENYLIST_MISSING)
    lines.append("codex sign-in: " + data["codex_sign_in"]["route"])
    if data["codex_sign_in"]["detail"]:
        lines.append("  " + data["codex_sign_in"]["detail"])
    return "\n".join(lines) + "\n"
