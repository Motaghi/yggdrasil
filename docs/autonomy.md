# Autonomy

Yggdrasil is built so that you decide the direction and the agents do the rest, inside limits you set. This page explains what needs your approval, how to let work continue while you are away, and how you check that work afterwards.

These are approval points in Yggdrasil's own flow. They are enforced for agents that use `ygg` as intended; an agent that follows planted instructions can act outside them (see [Security](security.md#known-gaps)).

## What needs your approval

You approve three things, through your main session:

- **The project plan.** Build work doesn't start until you approve it. Approving it also allows commits and pushes on the project's own `ygg/...` branches in the repos it names.
- **Each phase end.** The project manager writes a phase review and the plan for the next phase. Until you approve, only planning and criteria writing continue on that project. Other projects keep running.
- **Every problem needing your call.** When a project is about to break, or has broken, one of the limits in its plan (time, scope, quality or risk), the project manager stops that part of the work and sends you a plan for what to do. You approve it, reject it or close the project.

Inside those limits the project manager runs alone. Smaller concerns go to the project manager, not to you.

The main-session skill asks questions only in turns you started, with the recommended answer first, and every question and answer is recorded in the tracker. A question waiting for you blocks only the part of the work that needs the answer.

## Autonomy envelopes

An autonomy envelope is your permission for work to continue without you for a while. You grant one when you approve a project plan or a phase, or at any other time. It covers a whole programme until a time, or one project until a time or up to a phase.

Your main session grants it for you. The command behind it looks like this:

```bash
ygg envelope grant website-redesign-p1 --until 2026-12-01T07:00:00Z
ygg envelope grant website-redesign-p1 --through-stage 3
```

`--through-stage 3` covers the ends of phases 1 and 2; the end of phase 3 waits for you.

Inside an active envelope:

- The project manager may approve a phase end itself, if it says the phase is within the plan's limits. That phase end is queued for your morning check.
- On a question you haven't answered, the recommended option is taken automatically when the agent that raised it marked it reversible (with an undo note) and in line with the North Star, and named no hard-stop action. This is logged as a **provisional decision**, and the work goes on.
- No agent merges into main: `ygg merge main` is yours alone, envelope or not. `--allow-local-merge` is recorded with the envelope but changes nothing in this release.
- Hard stops the guards catch, and real problems, still stop that part of the work and alert you.

Whether a decision is reversible and fits the North Star are claims the agent makes when it raises the decision. Code checks only that a reversible decision has an undo note and that a decision naming a hard-stop `--action` never becomes provisional. A decision about pushing, deleting or spending that leaves out `--action` is not recognised, and this build has no North Star to check fit against. Grant envelopes with that in mind.

When an envelope is granted, Yggdrasil records the current commit of every branch involved, so everything done since can be shown exactly.

You can revoke an envelope at any time with `ygg envelope revoke`. After that, no build work starts on the project until you approve again.

Spending money is never covered by an envelope. Nothing detects spending by itself, and nothing caps model usage; see [Security](security.md#known-gaps).

## Standing permissions

Each programme can hold a short list of standing permissions in `autonomy.yaml`, kept in the tracker next to the North Star. It counts only when its `actor` is in `authority.actors`; like every tracker file, that field is not a signature. The allowed permissions are `land-lane`, `push-lane`, `merge-main-local`, `self-approve-stage` and `provisional-decision`. A file that lists `spend` is ignored. In this release the permissions are copied into each envelope when it is granted (`merge-main-local` only when the envelope is granted with `--allow-local-merge`) and shown in `ygg status --json`, but no command acts on them yet.

## The morning go

When an envelope ends, the programme reviewer writes the **morning go**: a list of everything that happened without you. The reviewer is a separate session that did none of the work. It always runs on the core Claude model, whatever the programme's or plan's model map says.

The list is built from the tracker records, not from the project manager's own account. It has one item for each phase end the project manager approved, each provisional decision and each merge, each with the exact range of commits it produced. The reviewer reads the original brief, the records and the changes, takes the project manager's report as one input, and lists anything the report left out.

You mark each item ok or not ok. Open items show up on your next turn in the main session and in `ygg status --json` (`morning_go_queue`); the daily digest doesn't list them. An item marked not ok creates a rework record, suspends the work items that depend on it and produces a plan to revert its commits. Running that revert is a hard stop that waits for you.

## The North Star

A North Star is a short, testable statement of what a programme's work is for: why it exists, its principles, non-goals and refusals. Building one by conversation, and checking decisions against it, is not in this build. What exists is `ygg northstar resolve <project>`, which shows a project's inherited refusals, and `ygg northstar add-refusal <project> --text <text>`, which adds one.
