# Launchers

A launcher is how the conductor starts an agent session. This build has two launchers, `headless` and `t3`.

| Launcher | What it runs | Status |
|---|---|---|
| `headless` (default) | The Claude Code or Codex CLI directly. Codex uses your own login; Claude Code runs with Yggdrasil's own settings folder and needs a sign-in it can use (see [Getting started](getting-started.md#what-you-need)) | In this build |
| `t3` | A thread in T3 Code, for Claude Code sessions | In this build, with limits; see [T3](#t3) |

Every start gets an idempotency key (the activation id). If Yggdrasil can't tell whether a start went through, it records the outcome as `uncertain` and never retries blind: the next attempt first checks whether the action already happened.

If a start fails because of rate limits or an exhausted quota, the node waits and is tried again later (`held-capacity`). This doesn't raise a stall alert. By default nothing limits how many sessions run at once; set `max_concurrent_sessions` to cap it (see [Configuration](configuration.md#key-settings)). Nothing limits what sessions cost; see [Security](security.md#known-gaps).

## Headless

The headless launcher runs Claude Code or Codex in the node's working folder. GPT-family models run on Codex and the others on Claude Code. In v1.1 only project reviewers may run on Codex; a project manager, team lead, worker or senior user on a GPT-family model is refused ("Codex builds are not in this release"). Each start:

- refuses to start unless `ygg` is on the conductor's `PATH`, and resolves it to an absolute path;
- for Claude Code, writes a settings file that registers Yggdrasil's guard hook for every tool call by that absolute path, with `disableAllHooks: false`, and passes it with `--settings`;
- in a git worktree, sets that worktree's own `core.hooksPath` (a worktree-level setting, so the home repository's own hook settings are untouched) to Yggdrasil's lane hooks folder, `<state folder>.safety/hooks`. It holds `pre-commit`, `pre-merge-commit`, `commit-msg`, `pre-push` and `reference-transaction`, and each one runs `ygg lane-check` with the same absolute `ygg`: the lane commit check, the commit message check, the check that the lane keeps its accepted commits, and the push check (see [Security](security.md#what-agents-may-do-without-asking)). The folder is outside the state folder, so an emptied state folder doesn't remove it, and each conductor pass rewrites any hook that was changed. The same worktree settings carry the profile's `commit_identity` name and email, and turn off commit signing and editors so git never waits;
- starts the agent with an allowlisted environment (see [Security](security.md#what-the-code-checks));
- writes a receipt `activations/<activation-id>.json` in the state folder, recording the process, its start and, later, its exit.

If the hooks can't be set up, the start is refused (exit code 3) and nothing runs.

The Claude Code command is `claude -p --settings <file> --model <model> --effort <effort>`, plus `--setting-sources` and, by default, `--add-dir` for the working folder, so the agent reads its project's `CLAUDE.md` but not the project's own settings (see `agent_project_settings` in [Configuration](configuration.md#key-settings)). The prompt goes to Claude Code on standard input, never on the command line. The effort is also in `YGG_EFFORT`. No permission flags are passed: what the agent may do without asking comes from the permission rules you agreed to, which Yggdrasil writes into the settings file (see [Security](security.md#what-agents-may-do-without-asking)).

The Codex command is `codex exec` with `--sandbox workspace-write`, the model and the effort (`model_reasoning_effort`), and the prompt on standard input. Codex has no guard hook. In v1.1 the only Codex sessions are project reviewers. A Codex reviewer runs with an empty scratch folder as its writable workspace, so its checkout stays read-only, and gets no write access to Yggdrasil's state or tracker folders; it writes only its verdict file.

When a profile sets `hub_base_url`, the headless launcher exports it to every session as `ANTHROPIC_BASE_URL`, and `launch_env` adds further variables (never `YGG_` ones). The core assumes neither. Neither applies to T3 sessions, which run in the T3 server's environment (see [Configuration](configuration.md#agent-environment)).

## T3

The `t3` launcher starts a Claude Code session as a thread in T3 Code. What turns it on is the path of the `t3` command, in `t3_bin` or `YGG_T3_BIN`; Yggdrasil never looks for `t3` on `PATH`. Set `launcher: t3` as well to make the choice plain (`ygg init` writes it if you choose T3 Code). With `t3_bin` set, only `launcher: headless` in your own config or profile keeps every session headless.

A session goes through T3 only when the `t3` command and the T3 server report the same version, and that version is supported (`t3_supported_cli_versions` and `t3_supported_server_versions`, `0.1.9` in this build). Otherwise the session starts headless, and the reason is logged in `launcher.jsonl` in the state folder. Codex sessions always start headless.

A node's first session starts its thread; later sessions send into the same thread. In this release, urgent messages are not sent into a running T3 thread: they go to the node's inbox, like every other message. T3 sessions get the same agreed permission rules as headless ones, and have their own known gaps; see [Security](security.md#known-gaps). T3 support is experimental in this release.

## How agents get their identity

- **Environment.** Each headless session gets `YGG_NODE` (its node id), `YGG_ACTIVATION`, `YGG_PROGRAMME`, `YGG_PROJECT`, `YGG_MODEL` and `YGG_EFFORT`, set after everything else so nothing overrides them. A T3 session gets `YGG_NODE`, `YGG_ACTIVATION`, `YGG_PROGRAMME` and `YGG_PROJECT`, plus the state, tracker, config and profile paths, through the settings file in its worktree.
- **Prompt.** The session is told its node id, role and activation, and pointed at its rehydration document and inbox. Then it gets its role prompt (project manager, team lead, senior user, worker or programme reviewer).

With `YGG_NODE` set, every `--from` and `--as` must be that node or one of its children, and the commands reserved for you are refused. These markers are environment variables, not credentials; see [Security](security.md#known-gaps).

Workers are handled differently. A worker registered as a node (`ygg node add --role worker`) runs as its own Claude Code session with its own `YGG_NODE`; in v1.1 a worker can't run on Codex. A worker started through Claude Code's Agent tool runs inside its team lead's session and gets its id in the prompt, so its identity rests on the prompt alone.

## Where sessions run

Every session below the main one starts inside its programme's home: a lane worktree for team leads, the integration worktree for project managers, and a detached checkout of the lane being tested for senior users. An existing worktree is reused only if it belongs to the programme's home and is on the expected branch; anything else at that path makes the launch refuse. For `vault-docs` programmes, sessions work in staging folders. The home's own checkout is never used as a working folder.
