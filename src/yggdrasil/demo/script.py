"""The demo's step lists and pacing.

Each node's steps run in order. A step runs once its `when` condition holds; a step whose condition does not hold
ends the activation (the conductor starts the node again when work is owed to it), and a step that is done is
never run again. Placeholders: `{home}` programme alpha's git home, `{node}`,
`{review}` the review this reviewer activation was given, `{head}` the commit checked out in the working folder.
"""

from __future__ import annotations

import datetime

PROGRAMME = "alpha"
PROJECT = "alpha-p1"

# Seconds a scripted agent waits before each step at default pace; `--fast` sets 0.
PACE_SECONDS = 3
# The driver prints narration line k no earlier than this many seconds times k after start (default pace).
LINE_FLOOR_SECONDS = 15
# How long one wait of the driver may take before the step fails.
DEADLINE_FAST = 120
DEADLINE_DEFAULT = 300
# How long a hold point waits for its go file.
HOLD_SECONDS = 120

BRIEF = """# Brief

Add two small text files to the demo repository, one per stage:

- Stage 1: `greeting.txt`, reading `Hello from WP-1`.
- Stage 2: `farewell.txt`, reading `Goodbye from WP-2`.
"""

README = "# Demo home\n\nThe git home of the Yggdrasil demo's programme alpha.\n"


def pid_text(home: str) -> str:
    """The PID pm1 writes: one repo (the home, no github key), two stages, no agent commands or merge check."""
    import yaml
    end = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {
        "schema": "ygg.pid.v1", "project": PROJECT, "programme": PROGRAMME,
        "repos": [{"path": home}],
        "stages": [{"number": 1, "name": "Greeting", "planned_end": end},
                   {"number": 2, "name": "Farewell", "planned_end": end}],
        "tolerances": {"time": "P1D"},
        "business_case": {"why": "Show how Yggdrasil runs a project.", "benefit": "A newcomer sees it work.",
                          "cost": {"elapsed": "PT5M", "user_minutes": 0}, "risks": ["none"],
                          "stop_if": "the demo is stopped"},
    }
    return "---\n" + yaml.safe_dump(data, sort_keys=False) + "---\n# PID\n\nThe demo project: two packages in two stages.\n"


def _stage(number: int) -> dict:
    return {"stage_state": {PROJECT: {"number": number, "state": "executing"}}}


def _pm_stage(number: int, package: str, title: str) -> list[dict]:
    return [
        {"id": "plan-%d" % number, "when": _stage(number),
         "run": ["ygg", "tracker", "write", "--as", "pm1", "--project", PROJECT, "--stage", str(number),
                 "--kind", "stage-plan", "--body", "Stage %d: %s" % (number, title)]},
        {"id": "package-%d" % number,
         "run": ["ygg", "tracker", "write", "--as", "pm1", "--project", PROJECT, "--stage", str(number),
                 "--package", package, "--kind", "work-package", "--body", title]},
        {"id": "criteria-request-%d" % number,
         "run": ["ygg", "send", "--from", "pm1", "--to", "pm1su1", "--kind", "work",
                 "--body", "write criteria for " + package]},
        {"id": "authorise-%d" % number, "when": {"has_criteria": package},
         "run": ["ygg", "wp", "authorise", PROJECT, "--package", package, "--lane", "pm1tl1"]},
        {"id": "merge-%d" % number, "when": {"package_state": {package: "accepted"}},
         "run": ["ygg", "merge", "lane", PROJECT, "--lane", "pm1tl1", "--repo", "{home}"]},
        {"id": "end-%d" % number, "when": _stage(number), "remove": ["end-stage-%d.md" % number],
         "write_file": {"path": "end-stage-%d.md" % number, "text": "# End of stage %d\n\n%s is merged.\n"
                        % (number, package)},
         "run": ["ygg", "stage", "end", PROJECT, "--within-tolerance", "yes",
                 "--report", "end-stage-%d.md" % number]},
    ]


def _tl_package(number: int, package: str, name: str, text: str, subject: str, hold: str | None) -> list[dict]:
    first = {"id": "file-%d" % number, "when": {"package_state": {package: "authorised"}},
             "write_file": {"path": name, "text": text + "\n"}}
    if hold:
        first["hold"] = hold
    return [
        first,
        {"id": "add-%d" % number, "run": ["git", "add", name]},
        # A retried commit whose commit already landed (the agent stopped before marking it done) is done.
        {"id": "commit-%d" % number, "done_if_head_subject": subject, "run": ["git", "commit", "-q", "-m", subject]},
        {"id": "ready-%d" % number, "run": ["ygg", "wp", "ready", "--from", "pm1tl1", "--package", package]},
    ]


def _su_package(package: str, number: int) -> list[dict]:
    return [
        {"id": "criteria-%d" % number, "when": {"package_exists": package},
         "run": ["ygg", "tracker", "write", "--as", "pm1su1", "--project", PROJECT, "--package", package,
                 "--kind", "criteria", "--body", "The file is committed on the lane and reads as the brief says."]},
        {"id": "accept-%d" % number, "when": {"package_state": {package: "ready-for-test"}, "testing": package},
         "run": ["ygg", "acceptance", "record", "--from", "pm1su1", "--package", package, "--sha", "{head}",
                 "--verdict", "accept"]},
    ]


STEPS: dict[str, list[dict]] = {
    "pm1": [
        # Body and report files must be inside the agent's worktree; each is removed once its command ran.
        {"id": "pid", "remove": ["pid.md"], "write_file": {"path": "pid.md", "text": "{pid}"},
         "run": ["ygg", "tracker", "write", "--as", "pm1", "--project", PROJECT, "--kind", "pid",
                 "--body-file", "pid.md"]},
        {"id": "pid-ready", "run": ["ygg", "tracker", "write", "--as", "pm1", "--project", PROJECT,
                                    "--kind", "pid-ready", "--body", "The PID is ready for approval."]},
        *_pm_stage(1, "WP-1", "Add greeting.txt"),
        *_pm_stage(2, "WP-2", "Add farewell.txt"),
    ],
    "pm1tl1": [
        *_tl_package(1, "WP-1", "greeting.txt", "Hello from WP-1", "WP-1: add greeting.txt", "agent-pm1tl1-WP-1"),
        *_tl_package(2, "WP-2", "farewell.txt", "Goodbye from WP-2", "WP-2: add farewell.txt", None),
    ],
    "pm1su1": [*_su_package("WP-1", 1), *_su_package("WP-2", 2)],
    # One activation per review; the verdict names the review the activation was given.
    "pm1rv1": [
        {"id": "verdict", "once": False,
         "write_file": {"path": "{review_dir}/verdict.json", "text": '{"review": "{review}", "verdict": "approve"}'}},
    ],
}
