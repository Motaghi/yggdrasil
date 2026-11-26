# CLI reference

This page lists every `ygg` command in this build. Normally your main session runs them for you. You can run any of them yourself from your own shell.

Commands marked **you** are reserved for you, and commands marked **not in agents** start or drive sessions. Both are refused whenever `YGG_NODE` or `YGG_ACTIVATION` is set, that is, from every session the conductor started. Your main session runs without those markers, so it can run them for you. A Codex builder runs `ygg` through its relay, which carries only a fixed list of commands. Of the commands open to agents, a Codex builder cannot run `pause`, `backlog` or `northstar`, nor `plugins`, `profile`, `standard`, `digest`, `morning-go` or `dashboard`; everything else open to an agent of its role is open to it.

## Setup and configuration

| Command | Purpose |
|---|---|
| `ygg init [--preset starter\|solo-builder\|team] [--non-interactive] [--answers <file>] [--out <path>] [--force] [--track-records]` | **You.** Guided setup that writes a commented config. It adds `ygg-state/`, `ygg-state.safety/` and `ygg-tracker/` to the `.gitignore` next to the config; `--track-records` leaves `ygg-tracker/` out, so you can keep the records in git. |
| `ygg doctor [--json]` | Show which tracker and launcher are active and whether each built-in option is ready, with the reason when one isn't (for Linear: whether credentials are stored; for T3: whether the `t3` command is set), whether the lane check has a leak denylist, and how a Codex session started now would sign in: the line `codex sign-in: <route>`, with a detail line under it when there is one, and in `--json` the key `codex_sign_in` with `route` and `detail`. The route is `provider key` (your Codex config's model provider reads its key from a variable, and that variable reaches agents with a value; the detail names the provider), `provider` (a provider with no `env_key`), `api key` (`CODEX_API_KEY` reaches agents), `yggdrasil login` (Yggdrasil's own Codex login is usable; the detail is its folder) or `none` (the detail is the next step, for example `set <variable> for the conductor`, `list <variable> under agent_env_passthrough so agents get it` or `run ygg codex login, set CODEX_API_KEY, or use a model provider with its own key`). It is worked out from the environment an agent would get, in Codex's own order, and adds a note when the installed Codex version is not probed. It never prints a key or a token. It makes no network call. It is meant only to read, with one exception: with `linear_token_store: file`, some invalid token files (a link, not a regular file, or one whose details can't be read) raise a `ygg-tracker-auth` alert and write the `linear-auth-failed` marker in the state folder; doctor then shows Linear as not ready. |
| `ygg --version` | Print the version (`ygg 1.1.1`) and exit. It reads no config, profile or state. |
| `ygg config explain [<key>] [--project <p>] [--package <id>] [--json]` | Show effective settings and the layer each came from. |
| `ygg config validate [--project <p>]` | Check every configuration layer. |
| `ygg plugins list [--json]` | List installed tracker, launcher and alert-forwarder plugins. |
| `ygg profile show` | Print the loaded profile with secret-looking values hidden, or `none`. |
| `ygg standard check [--file <path>]` | Validate the packaged tracker standard, or another file. |

## Codex sign-in

Yggdrasil's own Codex sign-in: one login folder for your user (`agent_codex_login_dir`, by default `~/.config/yggdrasil/agent-codex`) that every Codex session links to. See [Getting started](getting-started.md#what-you-need) for this and the other sign-in routes.

| Command | Purpose |
|---|---|
| `ygg codex login [--device-auth \| --with-api-key]` | **You.** Sign Codex agents in. The login folder should be a new one that only Yggdrasil uses; never point it at your own `~/.codex`. It creates the folder if it is missing (mode 0700). An existing folder is accepted only when it is empty or an earlier `ygg codex login` set it up; any other folder is refused with exit 3, and nothing in it is changed (`refused: agent_codex_login_dir <L> is not empty and is not a Yggdrasil Codex login folder; choose a new folder`). It then sets the folder to 0700, writes `config.toml` in it holding only `cli_auth_credentials_store = "file"`, so Codex keeps the login in the folder's `auth.json` and never in your keychain, and then runs `codex login` there with the option you gave. `--device-auth` is Codex's sign-in for a machine without a browser; `--with-api-key` makes Codex read an API key from standard input and store it. You may log in while Codex sessions run: they pick up the new login the next time they read it, but a login to a different account makes running sessions of the old account fail at their next refresh. |
| `ygg codex status` | **You.** Run `codex login status` for the login folder. Prints `no Yggdrasil Codex login` and exits 1 when the folder does not exist. |
| `ygg codex logout` | **You.** Run `codex logout` for the login folder, with a 60-second limit. It is refused (exit 3, `refused: Codex sessions are running; stop them or let them end, then log out`) while any Codex session that links to the login is running. When the folder does not exist it prints `no Yggdrasil Codex login` and exits 0 without running Codex. |

All three run the installed `codex` with `CODEX_HOME` set to the login folder, without `CODEX_API_KEY` or `CODEX_ACCESS_TOKEN` in its environment, and with your terminal's input and output, so the sign-in goes through Codex's own flow; Yggdrasil never reads, copies, prints or sends the login. Each exits with Codex's own exit code. When the installed Codex version is not one Yggdrasil has probed, each first prints `ygg: this Codex version is not probed for the shared sign-in` on standard error and runs anyway; when the version is probed but not on this platform (codex-cli 0.170.0 on Linux), the line is `ygg: Codex's shared sign-in is not probed on linux; use a model provider with its own key`, because no Codex session there uses the login. They refuse with exit 3 when `codex` is not found, when the login folder lies inside a folder agents or Yggdrasil's state can reach (see `agent_codex_login_dir` in [Configuration](configuration.md#profile-keys)), or when it is not a real folder of yours. `ygg codex login` with a non-default folder whose parent does not exist is refused too. A logout that does not finish within 60 seconds is stopped and exits 7; check with `ygg codex status`. Every `codex` command is refused from agent sessions, like the other commands marked **you**.

## Demo

`ygg demo [--fast] [--keep] [--no-open]` and `ygg demo --cleanup <folder>`. **You.** A live, offline run of the real conductor with scripted stand-in agents, in a temporary folder, about two minutes long. It needs `git` on your `PATH` and no AI account. The agents never call a model, and the demo makes no outside connection. It is refused (exit 3) from every agent session, like the other commands marked **you**.

The demo works in its own folder under your `TMPDIR` (`ygg-demo-...`, mode 0700) with its own config, profile, state, tracker, home folder for git and git settings. It never reads or writes yours, nor your Claude or Codex folders. Programme `alpha` and project `alpha-p1` get a brief, an approved plan, two work packages that are reviewed and approved, a merge into `main` and a close. Seven lines say where it is: `demo: 1/7 brief written`, `2/7 plan approved`, `3/7 WP-1 ready for review`, `4/7 WP-2 ready for review`, `5/7 reviews approved`, `6/7 merged into main` and `7/7 project closed`. After line 1 it prints where the dashboard page is. The page refreshes every 2 seconds. When the demo runs in a terminal, it tries to open the page in your browser unless you pass `--no-open`; on Linux this also needs `DISPLAY` or `WAYLAND_DISPLAY`. Otherwise it only prints the page's path. If the dashboard is not in this release, it says `demo: dashboard not in this release` and carries on.

| Option | What it does |
|---|---|
| `--fast` | No pause between the scripted steps, so the run is shorter than the default two minutes. |
| `--keep` | Keep the temporary folder when the run ends. A failed run always keeps it. |
| `--no-open` | Don't open the dashboard in a browser. |
| `--cleanup <folder>` | Stop and remove a demo folder left behind by a demo that was killed. It first checks that the folder is a private demo folder under your `TMPDIR`, and it refuses, touching nothing, if it isn't (`demo: <folder> is not a demo folder` or `demo: <folder> is not a safe demo folder: <reason>`). It also refuses while that demo is still running. |

When the demo stops, on success, on failure or on Ctrl-C, a hangup or a termination signal, it stops the conductor and every agent it started, then removes its folder (unless `--keep` or a failure). A demo that was killed so hard it could not clean up is reported by the next `ygg demo`, which prints the `ygg demo --cleanup <folder>` command to run and carries on. Until you run that command, a killed demo's conductor keeps running in the background. A failure prints `demo: failed at <setup, step n or cleanup>: <reason>` on stderr and keeps the folder.

Exit codes: 0 success; 1 failure or a usage error; 3 refused as a user-only command; 129, 130, 131 and 143 after a hangup, interrupt, quit or termination signal.

## Programmes, projects and nodes

| Command | Purpose |
|---|---|
| `ygg programme create <name> --domain <domain> --home <path> --kind git-repo\|shared-tree\|vault-docs` | **You.** Create a programme and its tracker record. |
| `ygg project create <project> --programme <name> --pm <node>` | **You.** Create a project and its project manager node. |
| `ygg project shape <project> --collapsed\|--standard` | **You.** Record the project's shape. |
| `ygg project close <project> [--premature --note <text>]` | **You.** Close a project. |
| `ygg node add <node> --role tl\|su\|worker\|reviewer --parent <node-or-programme> [--pairs-with <tl>[,<tl>]]` | Register a node. Adding a reviewer is reserved for you. |
| `ygg node release <node> --as failed\|aborted` | **You.** Settle a node's start whose outcome is uncertain, once its process is gone. `aborted` offers its work again; `failed` doesn't. Refused while the process may still be alive. |
| `ygg node reset <node> [--kill]` | **You.** Return a stalled node to idle. If it still has a live process, `--kill` is needed, and the process is killed and checked before the reset. |

## Status and decisions

| Command | Purpose |
|---|---|
| `ygg status [--json]` | One-screen summary per project, or the full state as JSON (`ygg.status.v1`). From an agent session it shows only that session's programme. |
| `ygg pending [--json]` | Open decisions and approvals. From an agent session, only that session's programme. |
| `ygg decide <decision-id> --option <label> [--note <text>]` | **You.** Answer a decision. |
| `ygg decision raise --from <node> --question <text> --option <label> ... --recommended <label> --reversible yes\|no [--undo <text>] --aligns yes\|no [--scope lane:<node>\|package:<id>] [--action <category> --target <text>]` | Raise a decision for you. |
| `ygg digest --date <yyyy-mm-dd>` | Write the daily digest. |
| `ygg summary [--since <span>]` | For each project with a work-item change in the span (default `1h`; for example `30m`, `2h`, `2d`), or with a decision waiting for you whatever its age: the work items finished and those being checked, then how many decisions wait for you. Prints `No change in ...` when there is neither. From an agent session, only that session's programme. |
| `ygg alerts [--for-user] [--json]` | List the alerts in `alerts.jsonl`, oldest first, with each one's class. `--for-user` keeps only the ones meant for you. `--json` also gives each one's next step. |
| `ygg dashboard <programme> [--no-open]` | Write the programme's dashboard page, `dashboard/<programme>.html` in the state folder, print its path and open it (unless `--no-open`). The page is a plain file, with a column each for to do, working, being checked, done and needs you. The conductor also rewrites it on every pass. When `dashboard_refresh_seconds` is set, the page reloads itself at that interval. |
| `ygg dashboard serve <programme> --port <n>` | **You.** Serves the page on 127.0.0.1 only, read only. Not in this release: the `dashboard_serve` switch ships off, so the command is refused. A served page reloads itself when `dashboard_refresh_seconds` is set. |

## Messages

| Command | Purpose |
|---|---|
| `ygg send --from <node> --to <node> --kind <kind> (--body <text> \| --body-file <path>) [--relay-of <msg-id>] [--idempotency-key <key>] [--urgent]` | Send a message to a node's inbox. `--body-file` reads a multi-line body from a UTF-8 file; an agent may name only a file in its own worktree. `--idempotency-key` is accepted but not used in this build. Only you and the project manager may send `--urgent`. |
| `ygg send --to <node> (--body <text> \| --body-file <path>) [--kind <kind>] [--urgent]` | **You.** The short form: a message from you, of kind `work` unless you name one. |
| `ygg inbox <node> [--ack <msg-id>]` | List or acknowledge inbox messages. |
| `ygg relay grant --from-domain <d> --to-domain <d>`, `ygg relay revoke --from-domain <d> --to-domain <d>` | **You.** Standing permission to relay between two domains. |
| `ygg agents allow-standard [--yes] [--programme <name>]`, `ygg agents allow-minimal [--programme <name>]`, `ygg agents revoke` | **You.** Show the standard agent permission rules by role (with `--yes`, agree to them), agree to the strict minimum instead, or withdraw your agreement. An agreement covers the programmes that exist when you make it; `--programme` adds a programme created later. Recorded in `_ygg/agents-agreement.json` in the files tracker folder. Needed before a launch whose agent has its own Claude settings folder. `revoke-minimal` is an older name for `revoke`. See [Security](security.md#what-agents-may-do-without-asking). |

## Plans, phases and work items

| Command | Purpose |
|---|---|
| `ygg pid approve <project>`, `ygg pid reject <project> --note <text>` | **You.** Approve or reject the project plan. Approval checks branch protection on each named repository. |
| `ygg stage end <project> --within-tolerance yes\|no [--justified yes\|no] --report <path>` | Project manager ends a phase; `--justified` is the business-case check. From an agent session, the report must be inside the tracker or the session's own work area. |
| `ygg stage end <project>` | **You.** End the current phase yourself, once all its work items are accepted. Refused when the project's `stage_close` is `pm-only`. |
| `ygg stage approve <project>`, `ygg stage reject <project> --note <text>` | **You.** Approve or reject a phase end. |
| `ygg backlog order <project> --order <id>,... [--rationale <text>] [--as <node>] [--theme <text>]` | Reorder the backlog. From a reviewer it is a proposal; `--theme` is yours. A Codex builder cannot run it. |
| `ygg wp authorise <project> --package <id> --lane <node>` | Authorise a work item for a team lead. Needs the reviewer's criteria first. |
| `ygg wp ready --from <node> --package <id>` | The lane's team lead hands a work item in for review: it opens a review by the project reviewer. Refused for document-folder (`vault-docs`) projects. |
| `ygg wp complete --from <node> --package <id>` | The lane's team lead marks a work item done. With a project reviewer it opens a review, like `wp ready`; without one (document-folder projects) the item goes straight to its senior user for testing. |
| `ygg wp reopen <project> --package <id>` | Project manager only. Send an accepted, stopped or merged work item back to its lane, through a new review. |
| `ygg wp cancel <project> --package <id> [--package <id> ...]` | Project manager or you. Cancel work items on one lane and reset the lane to its newest accepted commit. Refused if a commit it would drop is already in the integration branch or main. On a Codex lane the box, uncommitted changes included, is quarantined (kept, never cleaned) and the lane gets a fresh clone at that commit; see [Security](security.md#quarantined-boxes). |
| `ygg wp cancel <project> --finish <lane-node> [--yes <code>]` | **You.** Finish an interrupted cancel. Without `--yes` it lists every path and commit it would drop and gives a code; run it again with `--yes <code>` to go ahead. On a Codex lane it quarantines the box the cancel recorded instead of cleaning it, and builds the fresh clone at the commit the cancel recorded; see [Security](security.md#quarantined-boxes). |
| `ygg wp work-ahead <project> --package <id> --as <pm-node> [--reason <text>] [--evidence <text>]` | Project manager only. Record a request to work ahead on a later phase's work item. |
| `ygg wp work-ahead-review <project> --package <id> --as <node> --verdict <verdict> [--note <text>]` | The paired senior user or a programme reviewer reviews a work-ahead record, as itself. Never the project manager. |
| `ygg pid work-ahead <project> --set off\|conditional [--reason <text>]` | **You.** Change the approved plan's work-ahead setting. |
| `ygg review record --from <node> --package <id> --round <n> --verdict pass\|changes` | Record one round of the review loop. No other agent session may record it in the reviewer's name. |
| `ygg review retry <review-id>`, `ygg review drop <review-id>` | **You.** Start a held review again, or drop an open or held review. |
| `ygg ruling record --project <project> --text <text>` | **You.** Record your ruling on the project. Its running sessions get it as an urgent message. |
| `ygg acceptance record --from <node> --package <id> --sha <sha> --verdict accept\|reject` | Reviewer accepts or rejects the exact commit tested. No other agent session may record it in the reviewer's name. |
| `ygg routemap write --as <pm-node> --project <project> --file <path>` | Write the route map. From an agent session, the file must be inside the tracker or the session's own work area. |
| `ygg tracker write --as <node> --project <project> [--stage <n>] [--package <id>] [--worker <node>] --kind <kind> (--body <text> \| --body-file <path>)` | Write a tracker record on behalf of a node. `--body-file` reads a multi-line body (a PID, a work package) from a UTF-8 file; an agent may name only a file in its own worktree, such as `.ygg/notes/pid.md` (never committed). No other agent session may write criteria in a senior user's name. |

## Concerns, problems and blockers

| Command | Purpose |
|---|---|
| `ygg issue raise --from <node> --reason <text>` | Team lead or reviewer raises a concern to the project manager. |
| `ygg exception raise --from <pm-node> --reason <text> --plan <text>` | Project manager raises a problem needing your call. |
| `ygg exception decide <id> --plan approve\|reject\|close` | **You.** Decide a problem. |
| `ygg blocker raise --from <node> --reason <text> [--forecast]` | Raise a blocker now, or one forecast to block soon. |
| `ygg hardstop check --node <node> --action <category> --target <text>` | Ask for a hard-stop check and alert you. From an agent session, `--node` must be the session itself. |

## Autonomy and the North Star

| Command | Purpose |
|---|---|
| `ygg envelope grant <programme-or-project> (--until <iso8601> \| --through-stage <n>) [--allow-local-merge]` | **You.** Grant an autonomy envelope. |
| `ygg envelope revoke <programme-or-project>` | **You.** Revoke it. |
| `ygg morning-go build --programme <name> --reviewer-node <node>` | Assemble the morning go. |
| `ygg morning-go decide <item-id> ok\|not-ok` | **You.** Mark a morning-go item. |
| `ygg northstar resolve <project>`, `ygg northstar add-refusal <project> --text <text>` | Show the project's inherited refusals, or add one. A Codex builder cannot run either. |

## Git

| Command | Purpose |
|---|---|
| `ygg merge lane <project> --lane <node> --repo <path>` | Merge a lane into the integration branch, only with an acceptance naming its exact head. |
| `ygg merge check <project> [--repo <path>]` | You or the project manager. Run the merge check now: a trial merge of the integration branch into main, then the repository's own pre-commit checks on it. The conductor also starts it by itself once the last phase is approved and every work package is merged. `--repo` may be left out when the approved plan names one repository. |
| `ygg merge main <project> [--repo <path>] [--print]` | **You.** Merge the integration branch into your local main. It needs a passed merge check for the current main and integration heads, and it merges whatever the merge mode is. `--print` changes nothing: it tells you to open a pull request from the integration branch, or prints the git command to merge it yourself. Pushing main is a hard stop. |
| `ygg lane sync <project> --lane <node>` | You or the project manager. Merge main into a lane with Yggdrasil's own git, recorded as a sync. A conflict is aborted and the lane is left as it was. |
| `ygg push-guard <repo-path> <remote> [<url>]` | Pre-push check. Called by the git hook with git's remote name and URL, not by you. |
| `ygg lane-check pre-commit\|pre-merge-commit\|commit-msg <file>\|pre-push <remote> [<url>]\|reference-transaction <state>` | The lane check. Called by the lane hooks in each agent worktree, not by you. |

## Conductor, launches and safety

| Command | Purpose |
|---|---|
| `ygg conductor run`, `ygg conductor tick` | **Not in agents.** Run the conductor in the foreground, or do one pass. |
| `ygg heimdall tick`, `ygg heimdall admit` | **Not in agents.** One liveness pass, or check whether a launch would be admitted. |
| `ygg launch <node>` | **Not in agents.** Start one node through the launcher. |
| `ygg resolve-model <node> [--package <id>]` | Print the model, effort and pair model for a node as JSON. |
| `ygg event ci --repo <path> --branch <branch> --status passed\|failed` | **Not in agents.** Feed a CI result into the wake table. It queues wakes; it does not record acceptance. |
| `ygg event boat --node <node> --job <id> --status <status>` | **Not in agents.** Feed the result of an offloaded job into the wake table. |
| `ygg attach --session <id> [--takeover]`, `ygg detach --session <id>` | **Not in agents.** Take or release the main session's lock. `--takeover` is yours. |
| `ygg rehydrate` | **Not in agents.** Rebuild the conductor's state from the tracker alone. |
| `ygg pause [<programme>]` | Pause everything or one programme. Nothing new launches, and a Claude Code session halts at its next tool call. A running Codex builder keeps working until it ends, but its `ygg` calls are refused while paused; a call already being served finishes. Use `ygg stop` to end a Codex builder now. You and any Claude Code session may pause; a Codex agent cannot, because its relay does not carry `pause`. |
| `ygg resume [<programme-or-node>]` | **You.** Lift a pause or a stop. |
| `ygg stop [<programme>\|<node>]` | **You.** Emergency stop. It signals each headless agent's process group, Claude Code and Codex alike, and kills what is left after a grace period. A stopped Codex builder's `ygg` calls are refused until you resume; a call it was already running is cancelled and reported as stopping, with its outcome unknown. See [Security](security.md#stopping-things). |
| `ygg t3 settle <t3-project-id> --as removed\|kept` | **You.** Settle a T3 project removal that was started and never confirmed, which holds launches in its worktree. `removed`: the project is gone, and the close finishes. `kept`: Yggdrasil leaves the project and its worktree alone from now on. |
| `ygg hook pretool` | Claude Code guard hook. Reads the hook JSON on stdin; called by Claude Code, not by you. |
| `ygg hook posttool` | Claude Code's after-the-call hook: it notes that a call finished, for the busy and retry tracking. It never blocks anything. Called by Claude Code, not by you. |
| `ygg hook lane-pre-commit`, `ygg hook lane-commit-msg <file>` | Older entry points for the lane commit check and commit message check. The lane hooks use `ygg lane-check`. |
| `ygg _supervise <activation>` | **Not in agents.** Internal: the process that watches one agent. Started by `ygg launch`. |
| `ygg test set-next ...`, `ygg test activation ...`, `ygg test cancel ...`, `ygg test seed-inbox ...`, `ygg test alert ...`, `ygg test acceptance ...` | Test-suite helpers, refused unless `YGG_TEST_MODE=1` is set in your own shell. `ygg test faults` only lists the names of the test fault points. |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Usage or validation error |
| 2 | Hook denied the tool call |
| 3 | Refused by policy (identity, reserved for you, message rules, cross-domain, writer rule, approval kind, gate, launch precondition, Codex sign-in folder) |
| 4 | Denied by a guard (hard stop, push guard) |
| 5 | Lock held (main session or conductor) |
| 6 | Admission held |
| 7 | Outcome uncertain |
| 8 | Tracker unreachable or unavailable, a tracker write failed, or Linear sign-in failed |
| 64 | Paused |
