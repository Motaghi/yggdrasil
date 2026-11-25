# Concepts

This page explains how Yggdrasil is put together. You don't need any of it to use Yggdrasil through your main session, but it helps when you want to know why something happened.

## The four levels

Yggdrasil arranges sessions in a tree. You sit above it and talk only to the top.

| Level | Example id | Role |
|---|---|---|
| You | `user` | You give direction, answer questions and approve plans, phases and problems. |
| L1 | `l1` | The main session. The only place you are spoken to. It runs the approval commands on your behalf. |
| L2 | `pm1` | A project manager. Writes and keeps the project plan, the route map, phase plans and work items, and reports to you. |
| L3 | `pm1tl1`, `pm1su1` | A team lead, who manages the work, paired with a user-side reviewer, who writes the quality criteria before work starts and tests against them afterwards. One reviewer may cover several team leads. |
| L4 | `pm1tl1w2` | A worker, who does the actual work. A worker usually runs inside its team lead's session; one registered with `ygg node add --role worker` runs as its own session instead (see [Launchers](launchers.md)). |

For one project, the tree looks like this:

```text
you
  L1 main session (the only one that talks to you)
    pm1      project manager
      pm1tl1   team lead, runs workers pm1tl1w1, pm1tl1w2
      pm1su1   user-side reviewer, paired with pm1tl1
```

Each programme also has one **programme reviewer** (for example `rv1`). It writes the morning summary after overnight work and nothing else, and it is never a session that did the work.

A project with only one line of work can drop the team lead and run as project manager, user-side reviewer and workers. This "collapsed shape" happens only when you ask for it or accept the main session's suggestion. Four levels is the default.

**How a user-side reviewer gets a ruling into the record.** A user-side reviewer cannot record a ruling itself. It raises a decision for you (`ygg decision raise --from pm1su1 ...`), you answer it with `ygg decide`, and you record the ruling with `ygg ruling record`. Linking an answered decision to a recorded ruling automatically is planned for v1.2.

## Programmes and projects

A **programme** is one area of work with a home folder: for example `website-redesign` with its git repo, or `research` with a folder of documents. It has a domain and a work-product kind:

- `git-repo`: the home is a git repo. Each team lead works in its own worktree on its own branch.
- `shared-tree`: the home is a folder other sessions also use. Same worktree layout, and the home's own checkout is never touched.
- `vault-docs`: the home is a folder of documents with no branches. Each line of work writes to a staging folder, and the project manager moves accepted work into place. This kind is a preview in v1.1: its work packages get no automatic per-package review.

A programme without a declared kind cannot start any sessions.

A **project** belongs to one programme and has one project manager. It has a project plan with phases and limits (time, scope, quality, risk), work items, and a route map. Several projects can run in one programme.

## Nodes

Every session in the tree is a **node** with an id. Ids are lower-case letters, digits and hyphens. You register nodes with `ygg node add`, or the main session and project managers do it for you. A user-side reviewer is paired with team leads through `--pairs-with`.

## The message rules

Nodes send messages through Yggdrasil's own message command (`ygg send`, called Ratatoskr). Every message, delivered or refused, is logged. `ygg send` enforces these rules:

- A node may message its parent, its own children and its siblings (nodes with the same parent). Nothing else.
- A team lead talks to its own workers, never another team lead's workers.
- Project managers in the same domain may message each other. Across domains they may not.
- Only the main session can carry material from one domain to another, and only with your approval or a standing permission you gave (`ygg relay grant`), which you can withdraw (`ygg relay revoke`). A relay is detected when the message names the original with `--relay-of`; quoted text without it is not detected.
- Programme reviewers send no messages.

Messages land in the recipient's inbox. `ygg send` never talks to an AI session directly; the conductor hands inbox content to a session when it starts it.

## The conductor

The conductor is a plain program with no AI in it. You run it with `ygg conductor run`, and each pass (a "tick") it:

1. reads new messages and tracker events;
2. decides which nodes to wake, using a fixed table (for example: a work item was authorised, so wake its team lead and paired reviewer);
3. checks that the computer has room for another session;
4. starts the sessions through a launcher.

It starts each node at most once at a time. A message that arrives while a node is running wakes it again after the run ends. By default there is no fixed limit on how many sessions run at once (`max_concurrent_sessions` sets one); the conductor checks the computer's load before each start and holds new sessions while load is high. One tick also stops after a set number of starts or a set time, whichever comes first (see [Configuration](configuration.md)).

The conductor also watches for sessions that have gone quiet. A session that has died is restarted from its records, up to a retry limit. A session that is alive but silent for too long is flagged, not restarted. Stalled sessions are reported together in one alert.

## Disposable sessions, rebuilt from the tracker

No session in Yggdrasil lives for long. Each one starts on an event, rebuilds what it needs from the tracker and the repo, does its step, writes the result back and ends. This has three effects:

- Nothing burns tokens while waiting.
- A crashed session loses nothing that was written down.
- You can resume anywhere. Your main session can be a brand-new thread on a different host.

Each node has a short rehydration document in the tracker (at most 150 lines) with its role, open decisions, next actions, inbox and recent records. The tracker also keeps a journal per programme with unread messages, held starts and anything uncertain, so `ygg rehydrate` can rebuild the conductor's whole state from the tracker alone. Raw transcripts and message logs stay on your computer; what matters is summarised into the tracker through progress reports.

## Where your work lives

The tracker holds the management side: plans, decisions, reports, approvals. The work itself lives where it belongs, in git branches and documents, and the tracker links to it. Branches follow a fixed pattern: each team lead works on `ygg/<project>/lane/<node>`, and accepted work is merged into `ygg/<project>/_integration`. Agents are kept off main because `ygg merge main` is yours alone, and by the push check and GitHub branch protection; see [Security](security.md#main-is-protected-by-the-server).

## Words

By default Yggdrasil uses plain words: project plan, phase, progress report, problem needing your call. Its flow is inspired by PRINCE2®, and you can switch to PRINCE2 terms with `vocabulary: prince2`. See [PRINCE2](prince2.md).
