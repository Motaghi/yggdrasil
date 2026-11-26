"""The demo's scripted agent: the `claude` and `codex` the conductor starts in a demo.

It reads its node from `YGG_NODE` and runs that node's steps (script.STEPS) with `ygg` and `git`. It never
calls a model and never opens a network connection.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import script


def _log(message: str) -> None:
    sys.stderr.write("demo agent " + str(os.environ.get("YGG_NODE")) + ": " + message + "\n")
    sys.stderr.flush()


def _status() -> dict:
    """What `ygg status --json` prints, computed in this process (no new Python per check), by the same steps:
    the configuration read afresh and validated with the CLI's own call and arguments, then the status built,
    serialised strictly and redacted. Anything that would make `ygg status` fail (an invalid config, a status
    that can't be serialised) holds the step, as a failed `ygg status` did."""
    try:
        from .. import config, status, util
        # A fresh `ygg status` process reads the config files anew: drop this process's cached copies first.
        config.profile.cache_clear()
        config.file_layers.cache_clear()
        config.effective.cache_clear()
        config.validate(check_plugins=True, scan_tracker=True)  # as cli.main does for `status`
        data = json.loads(util.redact(json.dumps(status.build_status(), indent=2) + "\n"))
    except Exception as error:  # noqa: BLE001 (a check that can't be read holds, as a failed `ygg status` did)
        _log("status check held the step: " + repr(error))  # never a silent hold
        return {}
    return data if isinstance(data, dict) else {}


def _activation() -> dict:
    root, activation = os.environ.get("YGG_STATE_ROOT"), os.environ.get("YGG_ACTIVATION")
    if not root or not activation:
        return {}
    try:
        data = json.loads((Path(root) / "activations" / (activation + ".json")).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _review_id(record: dict) -> str:
    review = record.get("review")
    if isinstance(review, dict):
        review = review.get("id")
    return str(review or "")


def _holds(when: dict, record: dict) -> bool:
    if not when:
        return True
    current = _status()
    projects = {p.get("id"): p for p in current.get("projects") or []}
    packages = {p.get("id"): p for p in current.get("work_packages") or []}
    for project, expected in (when.get("stage_state") or {}).items():
        stage = (projects.get(project) or {}).get("stage") or {}
        if any(stage.get(key) != value for key, value in expected.items()):
            return False
    for package, expected in (when.get("package_state") or {}).items():
        if (packages.get(package) or {}).get("state") != expected:
            return False
    if "package_exists" in when and when["package_exists"] not in packages:
        return False
    if "testing" in when and str(record.get("testing_package") or "") != when["testing"]:
        return False
    if "has_criteria" in when:
        package = when["has_criteria"]
        project = (packages.get(package) or {}).get("project")
        if not project:
            return False
        path = Path(os.environ.get("YGG_FILES_ROOT", "")) / "projects" / project / "issues" / (package + ".md")
        try:
            text = path.read_text()
        except OSError:
            return False
        if not re.search(r"(?m)^kind: criteria\s*$", text):
            return False
    return True


def _hold(point: str) -> bool:
    """A test-mode hold point (named by the driver in YGG_DEMO_HOLD_POINTS): True when released."""
    folder = os.environ.get("YGG_DEMO_HOLD")
    names = [n.strip() for n in (os.environ.get("YGG_DEMO_HOLD_POINTS") or "").split(",") if n.strip()]
    if not folder or point not in names:
        return True
    hold = Path(folder)
    (hold / ("held-" + point)).write_text("")
    deadline = time.monotonic() + script.HOLD_SECONDS
    while not (hold / ("go-" + point)).exists():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


def _expand(value: str, node: str, record: dict) -> str:
    def head(_match) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=False).stdout.strip()

    root = os.environ.get("YGG_DEMO_ROOT", "")
    replacements = {
        "{node}": node,
        "{tmp}": os.environ.get("TMPDIR", "/tmp").rstrip("/"),
        "{home}": os.path.join(root, "home"),
        "{review}": _review_id(record),
        "{review_dir}": os.environ.get("YGG_REVIEW_DIR", ""),
    }
    if value == "{pid}":
        return script.pid_text(os.path.join(root, "home"))
    for key, replacement in replacements.items():
        value = value.replace(key, replacement)
    return re.sub(r"\{head\}", head, value)


def main() -> int:
    node = os.environ.get("YGG_NODE") or ""
    if sys.stdin is not None and not sys.stdin.closed and not sys.stdin.isatty():
        try:
            sys.stdin.read()  # the prompt; a scripted agent follows its script instead
        except OSError:
            pass
    steps = script.STEPS.get(node)
    if steps is None:
        _log("no script for this node")
        return 0
    try:
        pace = float(os.environ.get("YGG_DEMO_PACE") or 0)
    except ValueError:
        pace = 0.0
    record = _activation()
    root = Path(os.environ.get("YGG_DEMO_ROOT") or os.environ.get("TMPDIR") or ".")
    done_path = root / "agents" / (node + ".done")
    done = set(done_path.read_text().splitlines()) if done_path.exists() else set()
    # At the default pace each check of a step waits one pace first (each step still waits one pace before it
    # runs), and an activation that checks nothing still waits one: no activation ends just after it starts.
    waited = False
    for step in steps:
        once = step.get("once", True)
        if once and step["id"] in done:
            continue
        if pace:
            time.sleep(pace)
            waited = True
        if not _holds(step.get("when") or {}, record):
            break
        subject = step.get("done_if_head_subject")
        if subject and _head_subject() == subject:
            # The commit is already there: an earlier activation committed and stopped before recording the step
            # (a second `git commit` would fail with nothing to commit).
            _mark_done(done_path, done, step["id"])
            continue
        if step.get("hold") and not _hold(step["hold"]):
            _log("hold " + step["hold"] + " was not released in time")
            return 1
        if "write_file" in step:
            target = Path(_expand(step["write_file"]["path"], node, record))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_expand(step["write_file"]["text"], node, record))
        if "run" in step:
            argv = [_expand(value, node, record) for value in step["run"]]
            result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=120,
                                    stdin=subprocess.DEVNULL)
            for name in step.get("remove") or []:
                Path(_expand(name, node, record)).unlink(missing_ok=True)
            if result.returncode != 0:
                # Not marked done: a later activation tries it again once its condition holds.
                _log("step " + step["id"] + " exited " + str(result.returncode) + ": " +
                     (result.stderr or result.stdout).strip()[-400:])
                break
        if once:
            _mark_done(done_path, done, step["id"])
    if pace and not waited:
        time.sleep(pace)
    return 0


def _head_subject() -> str:
    return subprocess.run(["git", "log", "-1", "--format=%s"], capture_output=True, text=True,
                          check=False).stdout.strip()


def _mark_done(done_path: Path, done: set, step_id: str) -> None:
    """Record a finished step; written whole and renamed into place, so a stop mid-write loses nothing."""
    done.add(step_id)
    done_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = done_path.with_name(done_path.name + "." + str(os.getpid()) + ".tmp")
    temporary.write_text("".join(item + "\n" for item in sorted(done)))
    os.replace(temporary, done_path)


if __name__ == "__main__":
    sys.exit(main())
