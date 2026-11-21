"""The `ygg` command line."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from . import util
from .util import YggError


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise YggError(util.EXIT_USAGE, self.prog + ": " + message)


def build() -> Parser:
    parser = Parser(prog="ygg")
    sub = parser.add_subparsers(dest="command", parser_class=Parser)

    def group(name):
        p = sub.add_parser(name)
        return p.add_subparsers(dest="action", parser_class=Parser)

    programme = group("programme")
    p = programme.add_parser("create")
    p.add_argument("name"); p.add_argument("--domain", required=True); p.add_argument("--home", required=True)
    p.add_argument("--kind", required=True)

    project = group("project")
    p = project.add_parser("create")
    p.add_argument("name"); p.add_argument("--programme", required=True); p.add_argument("--pm", required=True)
    p = project.add_parser("shape")
    p.add_argument("name"); p.add_argument("--collapsed", action="store_true"); p.add_argument("--standard", action="store_true")
    p = project.add_parser("close")
    p.add_argument("name"); p.add_argument("--premature", action="store_true"); p.add_argument("--note")

    node = group("node")
    p = node.add_parser("add")
    p.add_argument("node"); p.add_argument("--role", required=True); p.add_argument("--parent", required=True)
    p.add_argument("--pairs-with")
    p = node.add_parser("release"); p.add_argument("node")
    p.add_argument("--as", dest="release_as", required=True, choices=["failed", "aborted"])
    p = node.add_parser("reset"); p.add_argument("node"); p.add_argument("--kill", action="store_true")

    t3_group = group("t3")
    p = t3_group.add_parser("settle"); p.add_argument("project_id")
    p.add_argument("--as", dest="settle_as", required=True, choices=["removed", "kept"])

    p = sub.add_parser("send")
    # --from and --kind are optional only for the user's short form (AC-54, lane 2); --urgent is AC-55 (lane 2).
    p.add_argument("--from", dest="sender"); p.add_argument("--to", required=True)
    p.add_argument("--kind")
    body = p.add_mutually_exclusive_group(required=True)
    body.add_argument("--body"); body.add_argument("--body-file", dest="body_file")
    p.add_argument("--relay-of"); p.add_argument("--idempotency-key"); p.add_argument("--urgent", action="store_true")

    p = sub.add_parser("inbox")
    p.add_argument("node"); p.add_argument("--ack")

    tracker = group("tracker")
    p = tracker.add_parser("write")
    p.add_argument("--as", dest="as_node", required=True); p.add_argument("--project", required=True)
    p.add_argument("--stage"); p.add_argument("--package"); p.add_argument("--worker")
    p.add_argument("--kind", required=True)
    # A multi-line body is written to a file with the Write tool and named here (the guard refuses quoted newlines).
    body = p.add_mutually_exclusive_group(required=True)
    body.add_argument("--body"); body.add_argument("--body-file", dest="body_file")

    routemap = group("routemap")
    p = routemap.add_parser("write")
    p.add_argument("--as", dest="as_node", required=True); p.add_argument("--project", required=True)
    p.add_argument("--file", required=True)

    pid = group("pid")
    p = pid.add_parser("approve"); p.add_argument("project")
    p = pid.add_parser("reject"); p.add_argument("project"); p.add_argument("--note", required=True)
    p = pid.add_parser("work-ahead"); p.add_argument("project")
    p.add_argument("--set", dest="setting", required=True); p.add_argument("--reason")

    stage = group("stage")
    p = stage.add_parser("end")
    # Without --within-tolerance and --report this is the user's form (AC-14, lane 2).
    p.add_argument("project"); p.add_argument("--within-tolerance", choices=["yes", "no"])
    p.add_argument("--justified", choices=["yes", "no"]); p.add_argument("--report")
    p = stage.add_parser("approve"); p.add_argument("project")
    p = stage.add_parser("reject"); p.add_argument("project"); p.add_argument("--note", required=True)

    wp = group("wp")
    p = wp.add_parser("authorise")
    p.add_argument("project"); p.add_argument("--package", required=True); p.add_argument("--lane", required=True)
    p = wp.add_parser("complete")
    p.add_argument("--from", dest="sender", required=True); p.add_argument("--package", required=True)
    p = wp.add_parser("work-ahead")
    p.add_argument("project"); p.add_argument("--package", required=True)
    p.add_argument("--as", dest="as_node", required=True); p.add_argument("--reason"); p.add_argument("--evidence")
    p = wp.add_parser("ready")  # A11 lane 2 (AC-12, AC-13, AC-93)
    p.add_argument("--from", dest="sender", required=True); p.add_argument("--package", required=True)
    p = wp.add_parser("reopen"); p.add_argument("project"); p.add_argument("--package", required=True)  # AC-65
    p = wp.add_parser("cancel"); p.add_argument("project")  # A11 AC-58, A11.1 AC-06, AC-11, AC-13, AC-14
    p.add_argument("--package", action="append"); p.add_argument("--finish"); p.add_argument("--yes")
    p = wp.add_parser("work-ahead-review")
    p.add_argument("project"); p.add_argument("--package", required=True)
    p.add_argument("--as", dest="as_node", required=True); p.add_argument("--verdict", required=True)
    p.add_argument("--note")

    review = group("review")
    p = review.add_parser("record")
    p.add_argument("--from", dest="sender", required=True); p.add_argument("--package", required=True)
    p.add_argument("--round", required=True, type=int); p.add_argument("--verdict", required=True)
    for name in ("retry", "drop"):  # A11 lane 2, user-only (AC-57)
        p = review.add_parser(name); p.add_argument("review_id")
    ruling = group("ruling")  # A11 lane 2 (AC-29, AC-89, AC-90)
    p = ruling.add_parser("record"); p.add_argument("--project", required=True); p.add_argument("--text", required=True)

    acceptance = group("acceptance")
    p = acceptance.add_parser("record")
    p.add_argument("--from", dest="sender", required=True); p.add_argument("--package", required=True)
    p.add_argument("--sha", required=True); p.add_argument("--verdict", required=True)

    issue = group("issue")
    p = issue.add_parser("raise")
    p.add_argument("--from", dest="sender", required=True); p.add_argument("--reason", required=True)

    exception = group("exception")
    p = exception.add_parser("raise")
    p.add_argument("--from", dest="sender", required=True); p.add_argument("--reason", required=True)
    p.add_argument("--plan", required=True)
    p = exception.add_parser("decide")
    p.add_argument("id"); p.add_argument("--plan", required=True, choices=["approve", "reject", "close"])

    blocker = group("blocker")
    p = blocker.add_parser("raise")
    p.add_argument("--from", dest="sender", required=True); p.add_argument("--reason", required=True)
    p.add_argument("--forecast", action="store_true")

    p = sub.add_parser("pause"); p.add_argument("programme", nargs="?")
    p = sub.add_parser("resume"); p.add_argument("scope", nargs="?")
    p = sub.add_parser("stop"); p.add_argument("scope", nargs="?")

    p = sub.add_parser("attach"); p.add_argument("--session", required=True); p.add_argument("--takeover", action="store_true")
    p = sub.add_parser("detach"); p.add_argument("--session", required=True)

    p = sub.add_parser("status"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("pending"); p.add_argument("--json", action="store_true")

    hook = group("hook")
    hook.add_parser("pretool")
    hook.add_parser("posttool")  # A11 seam: PostToolUse and PostToolUseFailure (posttool.py)
    hook.add_parser("lane-pre-commit")
    p = hook.add_parser("lane-commit-msg"); p.add_argument("message_file")

    heimdall = group("heimdall")
    heimdall.add_parser("tick"); heimdall.add_parser("admit")
    conductor = group("conductor")
    conductor.add_parser("tick"); conductor.add_parser("run")

    p = sub.add_parser("launch"); p.add_argument("node")
    p = sub.add_parser("_supervise"); p.add_argument("activation")

    test = group("test")
    p = test.add_parser("set-next"); p.add_argument("kind"); p.add_argument("number", type=int)
    p = test.add_parser("cancel"); p.add_argument("project"); p.add_argument("package")  # driver.seed_cancel
    p = test.add_parser("seed-inbox"); p.add_argument("node"); p.add_argument("--acked", type=int, required=True)  # AC-53
    test.add_parser("faults")
    p = test.add_parser("alert"); p.add_argument("kind")  # A11 amendment 25 (driver.raise_alert)
    p.add_argument("fields", nargs=argparse.REMAINDER)
    p = test.add_parser("acceptance"); p.add_argument("project"); p.add_argument("package")  # driver.seed_acceptance
    p.add_argument("sha")
    p = test.add_parser("activation"); p.add_argument("node")
    p.add_argument("--state", default="running", choices=["running", "queued"])
    p.add_argument("--process", default="none", choices=["live", "dead", "none"])
    p.add_argument("--heartbeat"); p.add_argument("--retries-used", type=int, default=0)
    p.add_argument("--outcome", default="confirmed", choices=["confirmed", "uncertain"])
    p.add_argument("--launcher", default="headless", choices=["headless", "t3"])
    p.add_argument("--phase", choices=["reserved", "spawned"])
    p.add_argument("--pid", type=int); p.add_argument("--process-start")

    agents = group("agents")
    p = agents.add_parser("allow-standard", help="show the standard agent permissions; --yes agrees (user-only)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--programme")
    p = agents.add_parser("allow-minimal", help="agree that agents get the minimal permissions (user-only)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--programme")
    agents.add_parser("revoke", help="withdraw your agreement (user-only)")
    agents.add_parser("revoke-minimal", help="the older name of revoke (user-only)")

    envelope = group("envelope")
    p = envelope.add_parser("grant"); p.add_argument("scope"); p.add_argument("--until"); p.add_argument("--through-stage")
    p.add_argument("--allow-local-merge", action="store_true")
    p = envelope.add_parser("revoke"); p.add_argument("scope")

    decision = group("decision")
    p = decision.add_parser("raise")
    p.add_argument("--from", dest="sender", required=True); p.add_argument("--question", required=True)
    p.add_argument("--option", action="append"); p.add_argument("--recommended", required=True)
    p.add_argument("--reversible", required=True, choices=["yes", "no"]); p.add_argument("--undo")
    p.add_argument("--aligns", required=True, choices=["yes", "no"]); p.add_argument("--scope")
    p.add_argument("--action"); p.add_argument("--target")
    p = sub.add_parser("decide"); p.add_argument("id"); p.add_argument("--option", required=True); p.add_argument("--note")

    hardstop = group("hardstop")
    p = hardstop.add_parser("check"); p.add_argument("--node", required=True); p.add_argument("--action", required=True)
    p.add_argument("--target", required=True)

    event = group("event")
    p = event.add_parser("ci"); p.add_argument("--repo", required=True); p.add_argument("--branch", required=True)
    p.add_argument("--status", required=True, choices=["passed", "failed"])
    p = event.add_parser("boat"); p.add_argument("--node", required=True); p.add_argument("--job", required=True)
    p.add_argument("--status", required=True)

    relay = group("relay")
    for name in ("grant", "revoke"):
        p = relay.add_parser(name); p.add_argument("--from-domain", required=True); p.add_argument("--to-domain", required=True)
    standard = group("standard")
    p = standard.add_parser("check"); p.add_argument("--file")

    morning = group("morning-go")
    p = morning.add_parser("build"); p.add_argument("--programme", required=True); p.add_argument("--reviewer-node", required=True)
    p = morning.add_parser("decide"); p.add_argument("id"); p.add_argument("verdict", choices=["ok", "not-ok"])

    backlog = group("backlog")
    p = backlog.add_parser("order"); p.add_argument("project"); p.add_argument("--order", required=True)
    p.add_argument("--rationale"); p.add_argument("--as", dest="as_node"); p.add_argument("--theme")

    p = sub.add_parser("digest"); p.add_argument("--date", required=True)
    northstar = group("northstar")
    p = northstar.add_parser("resolve"); p.add_argument("project")
    p = northstar.add_parser("add-refusal"); p.add_argument("project"); p.add_argument("--text", required=True)

    p = sub.add_parser("push-guard"); p.add_argument("repo"); p.add_argument("remote"); p.add_argument("url", nargs="?")
    merge = group("merge")
    p = merge.add_parser("lane"); p.add_argument("project"); p.add_argument("--lane", required=True); p.add_argument("--repo", required=True)
    # --repo optional and --print: A11 lane 4 (AC-27, AC-85, amendments 14 and 29).
    p = merge.add_parser("main"); p.add_argument("project"); p.add_argument("--repo")
    p.add_argument("--print", dest="print_only", action="store_true")
    p = merge.add_parser("check"); p.add_argument("project"); p.add_argument("--repo")  # A11 lane 4 (AC-27)
    lane = group("lane")  # A11 lane 1 (AC-66)
    p = lane.add_parser("sync"); p.add_argument("project"); p.add_argument("--lane", required=True)
    p = sub.add_parser("lane-check"); p.add_argument("hook_args", nargs=argparse.REMAINDER)  # A11 lane 1 (AC-16)

    p = sub.add_parser("resolve-model"); p.add_argument("node"); p.add_argument("--package")

    p = sub.add_parser("init"); p.add_argument("--preset"); p.add_argument("--non-interactive", action="store_true")
    p.add_argument("--answers"); p.add_argument("--out"); p.add_argument("--force", action="store_true")
    p.add_argument("--track-records", action="store_true")
    cfg = group("config")
    p = cfg.add_parser("explain"); p.add_argument("key", nargs="?"); p.add_argument("--project")
    p.add_argument("--package"); p.add_argument("--json", action="store_true")
    p = cfg.add_parser("validate"); p.add_argument("--project")
    plugins = group("plugins")
    p = plugins.add_parser("list"); p.add_argument("--json", action="store_true")

    p = sub.add_parser("profile"); p.add_argument("action", choices=["show"])
    # A11 verbs behind release switches (lane 4). `ygg dashboard <programme>` and `ygg dashboard serve <programme>`.
    p = sub.add_parser("dashboard"); p.add_argument("words", nargs="+"); p.add_argument("--no-open", action="store_true")
    p.add_argument("--port", type=int)
    p = sub.add_parser("summary"); p.add_argument("--since")
    p = sub.add_parser("doctor"); p.add_argument("--json", action="store_true")  # A11 AC-24
    p = sub.add_parser("alerts"); p.add_argument("--for-user", action="store_true")  # A11 seam; lane 4 extends
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("rehydrate")
    return parser


def dispatch(args) -> tuple[int, str]:
    from . import commands, ratatoskr, seams, state
    command, action = args.command, getattr(args, "action", None)
    state.check_user_verb(command, action)
    if command == "programme" and action == "create":
        return 0, commands.programme_create(args.name, args.domain, args.home, args.kind)
    if command == "project" and action == "create":
        return 0, commands.project_create(args.name, args.programme, args.pm)
    if command == "node" and action == "add":
        return 0, commands.node_add(args.node, args.role, args.parent, args.pairs_with)
    if command == "node" and action in ("release", "reset"):
        from . import noderepair
        if action == "release":
            return 0, noderepair.release(args.node, args.release_as)
        return 0, noderepair.reset(args.node, args.kill)
    if command == "t3" and action == "settle":
        from . import t3
        return 0, t3.settle(args.project_id, args.settle_as)
    if command == "send":
        if args.body is None:
            args.body = commands.read_body_file(args.body_file)
        if args.sender is None or args.kind is None:
            # The user's short form (AC-54): from the user, kind work unless named, to any node.
            state.check_user_only("send without --from")
            state.require_node(args.to)
            msg = ratatoskr.send("user", args.to, args.kind or "work", args.body, internal=True,
                                 urgent=args.urgent, origin="user")
            return 0, msg + " delivered\n"
        msg = ratatoskr.send(args.sender, args.to, args.kind, args.body, relay_of=args.relay_of, urgent=args.urgent)
        return 0, msg + " delivered\n"
    if command == "inbox":
        return 0, ratatoskr.inbox(args.node, args.ack)
    if command == "tracker" and action == "write":
        if args.body is None:
            args.body = commands.read_body_file(args.body_file)
        return 0, commands.tracker_write(args.as_node, args.project, args.kind, args.body, args.stage,
                                         args.package, args.worker)
    if command == "routemap" and action == "write":
        return 0, commands.routemap_write(args.as_node, args.project, args.file)
    if command == "pid":
        if action == "approve":
            return 0, commands.pid_approve(args.project)
        if action == "work-ahead":
            from . import workahead
            return 0, workahead.set_setting(args.project, args.setting, args.reason)
        return 0, commands.pid_reject(args.project, args.note)
    if command == "stage":
        if action == "end":
            if args.within_tolerance is None or args.report is None:
                from . import stageclose
                return 0, stageclose.user_stage_end(args.project)
            return 0, commands.stage_end(args.project, args.within_tolerance, args.report, args.justified)
        if action == "approve":
            return 0, commands.stage_approve(args.project)
        return 0, commands.stage_reject(args.project, args.note)
    if command == "wp":
        if action == "authorise":
            return 0, commands.wp_authorise(args.project, args.package, args.lane)
        if action == "ready":
            return 0, commands.wp_ready(args.sender, args.package)
        if action == "cancel":
            from . import cancel
            try:
                return 0, cancel.cancel(args.project, list(args.package or []), args.finish, args.yes)
            except cancel.PreviewRefusal as preview:
                return util.EXIT_REFUSED, preview.stdout
        if action == "reopen":
            return 0, commands.wp_reopen(args.project, args.package)
        if action in ("work-ahead", "work-ahead-review"):
            from . import workahead
            if action == "work-ahead":
                return 0, workahead.write_record(args.project, args.package, args.as_node, args.reason,
                                                 args.evidence)
            return 0, workahead.write_review(args.project, args.package, args.as_node, args.verdict, args.note)
        return 0, commands.wp_complete(args.sender, args.package)
    if command == "review":
        if action in ("retry", "drop"):
            from . import review
            return 0, (review.retry if action == "retry" else review.drop)(args.review_id)
        return 0, commands.review_record(args.sender, args.package, args.round, args.verdict)
    if command == "ruling":
        from . import review
        return 0, review.record_ruling(args.project, args.text)
    if command == "acceptance":
        return 0, commands.acceptance_record(args.sender, args.package, args.sha, args.verdict)
    if command == "issue":
        return 0, commands.issue_raise(args.sender, args.reason)
    if command == "exception" and action == "raise":
        return 0, commands.exception_raise(args.sender, args.reason, args.plan)
    if command == "blocker":
        return 0, commands.blocker_raise(args.sender, args.reason, args.forecast)
    if command == "pause":
        return 0, commands.pause(args.programme)
    if command == "resume":
        return 0, commands.resume(args.scope)
    if command == "stop":
        from . import stop
        return 0, stop.stop(args.scope)
    if command == "attach":
        return 0, commands.attach(args.session, args.takeover)
    if command == "detach":
        return 0, commands.detach(args.session)
    if command == "status":
        from . import status
        data = status.build_status()
        # Status shows configured values (delivery defaults among them): token-shaped strings never print.
        return 0, util.redact((json.dumps(data, indent=2) + "\n") if args.json else status.human(data))
    if command == "pending":
        from . import pending
        data = pending.build()
        return 0, (json.dumps(data, indent=2) + "\n") if args.json else pending.human(data)
    if command == "hook" and action == "pretool":
        from . import hooks
        return hooks.main_pretool(), ""
    if command == "hook" and action == "posttool":
        from . import posttool
        return posttool.main_posttool(), ""
    if command == "hook" and action in ("lane-pre-commit", "lane-commit-msg"):
        from . import lanecheck
        if action == "lane-pre-commit":
            return lanecheck.pre_commit(), ""
        return lanecheck.commit_msg(args.message_file), ""
    if command == "heimdall":
        from . import conductor
        if action == "tick":
            flagged = conductor.heimdall_tick()
            return 0, ("stalled: " + ", ".join(flagged) + "\n") if flagged else ""
        ok, why = conductor.admit()
        if not ok:
            raise YggError(util.EXIT_ADMISSION, "admission held: " + why)
        return 0, "admitted: " + why + "\n"
    if command == "conductor":
        from . import conductor
        if action == "tick":
            result = conductor.tick()
            return 0, json.dumps(result) + "\n"
        return conductor.run(), ""
    if command == "launch":
        from . import interlock, launcher
        interlock.reconcile()  # stop and pause intents are reconciled before an explicit launch
        result = launcher.launch(args.node)
        code = {"confirmed": 0, "failed": 1, "capacity": util.EXIT_ADMISSION,
                "uncertain": util.EXIT_UNCERTAIN}.get(result["outcome"], 1)
        # `started` is internal (tick accounting); the printed result keeps its published fields.
        return code, json.dumps({k: v for k, v in result.items() if k != "started"}) + "\n"
    if command == "_supervise":
        from . import launcher
        return launcher.supervise(args.activation), ""
    if command == "test":
        from . import testmode
        if action == "cancel":
            from . import cancel
            return 0, cancel.seed(args.project, args.package)
        if action == "seed-inbox":
            return 0, ratatoskr.seed_inbox(args.node, args.acked)
        if action == "set-next":
            return 0, testmode.set_next(args.kind, args.number)
        if action == "faults":
            from . import faults
            return 0, faults.listing()
        if action == "alert":
            return 0, testmode.alert(args.kind, args.fields)
        if action == "acceptance":
            return 0, testmode.acceptance(args.project, args.package, args.sha)
        return 0, testmode.activation(args)
    if command == "envelope":
        from . import envelopes
        if action == "grant":
            return 0, envelopes.grant(args.scope, args.until, args.through_stage, args.allow_local_merge)
        return 0, envelopes.revoke(args.scope)
    if command == "decision":
        from . import decisions
        return 0, decisions.raise_decision(args)
    if command == "decide":
        from . import decisions
        return 0, decisions.decide(args.id, args.option, args.note)
    if command == "hardstop":
        return commands.hardstop_check(args.node, args.action, args.target)
    if command == "event":
        if action == "ci":
            return 0, commands.event_ci(args.repo, args.branch, args.status)
        return 0, commands.event_boat(args.node, args.job, args.status)
    if command == "project" and action == "shape":
        return 0, commands.project_shape(args.name, args.collapsed, args.standard)
    if command == "morning-go":
        from . import morning_go
        if action == "build":
            return 0, morning_go.build(args.programme, args.reviewer_node)
        return 0, morning_go.decide(args.id, args.verdict)
    if command == "backlog":
        from . import backlog
        return 0, backlog.order(args.project, args.order, args.rationale, args.as_node, args.theme)
    if command == "digest":
        from . import reporting
        return 0, reporting.digest(args.date)
    if command == "northstar":
        from . import reporting
        if action == "resolve":
            return 0, reporting.northstar_resolve(args.project)
        return 0, reporting.northstar_add_refusal(args.project, args.text)
    if command == "push-guard":
        from . import gitops
        return gitops.main_push_guard(args.repo, args.remote, args.url), ""
    if command == "merge":
        from . import gitops
        if action == "lane":
            return 0, gitops.merge_lane(args.project, args.lane, args.repo)
        if action == "check":
            from . import mergecheck
            return 0, mergecheck.check_verb(args.project, args.repo)
        return 0, gitops.merge_main(args.project, args.repo, args.print_only)
    if command == "lane":
        from . import gitops
        return 0, gitops.lane_sync(args.project, args.lane)
    if command == "lane-check":
        from . import lanecheck
        return lanecheck.main(args.hook_args), ""
    if command == "dashboard":
        from . import dashboard
        return 0, dashboard.command(args.words, args.no_open, args.port)
    if command == "summary":
        from . import summary
        return 0, summary.build(args.since)
    if command == "doctor":
        from . import doctor
        return 0, doctor.show(bool(getattr(args, "json", False)))
    if command == "alerts":
        from . import alertkinds
        return 0, alertkinds.show(args.for_user, args.json)
    if command == "resolve-model":
        from . import models
        return 0, json.dumps(models.resolve(args.node, args.package)) + "\n"
    if command == "init":
        from . import init
        return 0, init.run(args.preset, args.non_interactive, args.answers, args.out, args.force, args.track_records)
    if command == "config":
        from . import config
        if action == "explain":
            config.validate()
            if args.project:
                util.check_id(args.project, "project id")
            return 0, config.explain(args.key, args.project, args.package, args.json)
        config.validate(args.project)
        return 0, "configuration ok\n"
    if command == "plugins":
        from . import plugins
        return 0, plugins.show(args.json)
    if command == "relay":
        return 0, ratatoskr.relay_grant(args.from_domain, args.to_domain, revoke=action == "revoke")
    if command == "standard":
        from . import standard
        return 0, standard.check(args.file)
    if command == "exception" and action == "decide":
        return 0, commands.exception_decide(args.id, args.plan)
    if command == "project" and action == "close":
        return 0, commands.project_close(args.name, args.premature, args.note)
    if command == "profile":
        from . import config, util as u
        loaded = config.profile()
        return 0, (u.dump_yaml(u.redact_profile(loaded)) if loaded else "none\n")
    if command == "rehydrate":
        from . import rehydrate
        return 0, rehydrate.rehydrate_all()
    if command == "agents":
        from . import permissions
        if action == "allow-standard":
            return 0, permissions.allow_standard(args.yes, args.programme)
        if action == "allow-minimal":
            return 0, permissions.allow_minimal(args.yes, args.programme)
        if action in ("revoke", "revoke-minimal"):
            return 0, permissions.revoke(action)
        raise util.usage("ygg agents allow-standard [--yes] | allow-minimal | revoke")
    raise YggError(util.EXIT_USAGE, "unknown command")


NO_PREVALIDATION = {"init", "hook", "_supervise", "plugins", "profile", "lane-check"}
# Safety commands act on local evidence first; their validation never reads the tracker (I7).
SAFETY_COMMANDS = {"stop", "pause", "resume"}
def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = build().parse_args(argv)
        if not args.command:
            build().print_help()
            return 1
        if args.command not in NO_PREVALIDATION and not (args.command == "config"):
            from . import config
            config.validate(check_plugins=True, scan_tracker=args.command not in SAFETY_COMMANDS)
        code, output = dispatch(args)
        if output:
            sys.stdout.write(output)
        return code
    except YggError as error:
        print(error.message, file=sys.stderr)
        return error.code
    except subprocess.TimeoutExpired as error:
        print("deadline exceeded: " + str(error), file=sys.stderr)
        return util.EXIT_UNCERTAIN


if __name__ == "__main__":
    sys.exit(main())
