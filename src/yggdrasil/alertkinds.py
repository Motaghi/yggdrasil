"""The alert-kind table (A11 test conventions, "Alert classes"; AC-43). Data only: lane 4 owns delivery.

Every alert goes to `alerts.jsonl` with an id, a class and a `next_step`. The kinds below are class `user`;
every other kind is class `log`. Two kinds depend on their fields: `ygg-stage-ready` is `user` only when
`escalated` is true (the PM's close activation ended without ending the stage), and `ygg-uncertain` is `user`
only when `age_seconds` is 300 or more (still held 300 s past its reservation). `state.alert` stamps the class
and next step from this table; lanes import `classify` and `NEXT_STEPS`, never copy the texts.
"""

from __future__ import annotations

USER, LOG = "user", "log"

NEXT_STEPS = {
    "ygg-launch-refused": "Ask your main session to fix the reason shown; Yggdrasil then tries again by itself.",
    "ygg-pid-ready": "Read the plan and approve it with your main session.",
    "ygg-decision-waiting": "Answer the decision with your main session.",
    "ygg-stage-approval": "Read the stage report and approve it with your main session.",
    "ygg-stage-ready": "Ask your main session to end the stage.",
    "ygg-stage-close-refused": "Ask your main session what blocked the close.",
    "ygg-merge-ready": 'Tell your main session "merge this" when you\'re ready.',
    "ygg-project-closed": "Nothing to do; the work is finished.",
    "ygg-needs-l1": "Open your main session so it can pick the project up again.",
    "ygg-uncertain": "Ask your main session to check the agent before anything restarts.",
    "ygg-stall": "Ask your main session to look at the stalled agent.",
    "ygg-stop-held": "Ask your main session to check the agent, then release and resume it.",
    "ygg-merge-check-failed": "Nothing to do yet; the team is fixing it.",
    "ygg-agreement-needed": "Ask your main session to show you the agent rules, then agree.",
    "ygg-review-held": "Ask your main session whether to retry that review or drop it.",
    "ygg-review-fallback": ("Nothing to do now: the hub's Codex pool is unavailable, so this review ran on the same "
                            "model family. Ask your main session to check the hub if it keeps happening."),
    "ygg-exception": "Read the exception report and decide with your main session.",
    "ygg-blocker": "Ask your main session what the team needs from you.",
    "ygg-protection-missing": "Protect main, or ask your main session to set branch protection to warn.",
    "ygg-tracker-corrupt": "Stop and ask your main session to repair the tracker.",
    "ygg-cancel-interrupted": 'Tell your main session "finish the cancel".',  # A11.1 AC-13
    "ygg-clone-replaced": ("Ask your main session why the lane's clone keeps running out of time; inspect the kept "
                           "boxes before you move them."),  # v1.1.1
}
USER_KINDS = frozenset(NEXT_STEPS)
UNCERTAIN_USER_AFTER_SECONDS = 300

# Log-class kinds A11 names (no next step for the user).
LOG_KINDS = frozenset({"ygg-alert-undelivered", "ygg-corrupt-evidence",
                       "ygg-codex-sign-in"})  # v1.1.1: a Codex launch with no sign-in route

# Message templates for `ygg test alert <kind>` (test mode), so a test-raised alert reads like the real one.
# A lane that raises a kind writes its own message; these only fill the fixture verb.
TEST_MESSAGES = {
    "ygg-launch-refused": "Launch of {node} refused: {reason}",
    "ygg-stall": "Node {node} stalled",
    "ygg-uncertain": "Activation of {node} is uncertain",
    "ygg-stop-held": "Stop of {node} held for you",
}


def _true(value) -> bool:
    return value is True or str(value).strip().lower() in ("true", "yes", "1")


def classify(kind: str, fields: dict | None = None) -> str:
    """The class of one alert: `user` or `log`."""
    fields = fields or {}
    if kind not in USER_KINDS:
        return LOG
    if kind == "ygg-stage-ready":
        return USER if _true(fields.get("escalated")) else LOG
    if kind == "ygg-uncertain":
        try:
            age = float(fields.get("age_seconds"))
        except (TypeError, ValueError):
            return LOG
        return USER if age >= UNCERTAIN_USER_AFTER_SECONDS else LOG
    return USER


def next_step(kind: str) -> str | None:
    return NEXT_STEPS.get(kind)


def test_message(kind: str, fields: dict) -> str:
    template = TEST_MESSAGES.get(kind)
    if template is None:
        return kind + (" " + ", ".join(k + "=" + str(v) for k, v in sorted(fields.items())) if fields else "")
    values = {"node": "", "reason": "test"}
    values.update({k: str(v) for k, v in fields.items()})
    return template.format_map(values)


def show(for_user: bool, as_json: bool) -> str:
    """`ygg alerts [--for-user] [--json]`: the alerts in alerts.jsonl, oldest first; `--for-user` keeps the
    class-`user` ones. An alert written before A11 has no class: it is classed from its kind on read."""
    import json
    from . import state
    rows = []
    for row in state.alerts():
        row = dict(row)
        row.setdefault("class", classify(str(row.get("kind")), row))
        row.setdefault("next_step", next_step(str(row.get("kind"))))
        if for_user and row["class"] != USER:
            continue
        rows.append(row)
    if as_json:
        return json.dumps(rows, indent=2) + "\n"
    return "".join(str(r.get("ts")) + "  " + str(r.get("kind")) + " (" + r["class"] + ")  " +
                   str(r.get("message", "")) + "\n" for r in rows)
