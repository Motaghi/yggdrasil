# Yggdrasil

Yggdrasil lets you hand a big job to a team of AI agents and stay in charge. You describe what you want in a normal conversation. The agents plan it, split it up, do the work and check each other, and they stop and ask you at the approval points you set.

You talk to one AI session, your **main session** (the docs call it L1). Plans, decisions, reports and approvals are written down in a tracker, so you can see what happened and why.

Status: version 1.1.1; see the [release notes](docs/releases/v1.1.1.md). Read [Safety](#safety) before you run it on anything that matters.

## Who it's for

Yggdrasil is for people who already use Claude Code and Codex and want AI agents to carry whole projects, not just single tasks, with clear approval points. You need a Mac or Linux computer that can stay on while work runs, Claude Code and Codex installed, each with a sign-in Yggdrasil can use (agents do not use your own Codex login), and a few minutes a day to answer questions and approve plans. The full list is in [Getting started](docs/getting-started.md#what-you-need), which also covers the ways to sign the agents in (your own subscription, a proxy, an API key, or for Codex Yggdrasil's own shared login made with `ygg codex login`) and what OpenAI and Anthropic advise for each.

## What you get

- **One main session.** You talk to one AI session. It asks you the questions that need your answer, and you can start or resume it in any chat.
- **A team behind it.** Each project gets a project manager that plans the work and reports to you. Under it, team leads run the work and hand pieces to worker agents, while a paired reviewer writes down what "good" means before work starts and tests the result afterwards. In projects on a git repository, a project reviewer on the configured pair model checks each piece of work before it counts as done; with the default setup that is a model from the other family. Projects on a folder of documents (`vault-docs`) are a preview in v1.1: their work gets no automatic per-piece review.
- **A written record.** Plans, decisions, reports and approvals are written to a **tracker**: plain files on your computer that you can read at any time, or a Linear team. Agents are told to keep their notes there too, so if a session stops, the next one picks up from the tracker. Nothing forces an agent to record everything it does, and stop and pause markers live in a local folder, not the tracker.
- **Separate working copies.** Each team gets its own git branch and worktree, and agents are told to work there, not in your own checkout. This is a convention, not a wall: agents run as your OS user, and one that goes outside its worktree can still change your checkout (see [Known gaps](docs/security.md#known-gaps)). A Codex team lead and its workers instead share a private clone of their lane, and on macOS Codex's sandbox lets them write only there and in their relay folder; their commits reach the project only through a receive step (see [Security](docs/security.md#what-agents-may-do-without-asking)).
- **Overnight work inside limits you set.** You can let work continue while you are away, for a set time or up to a set point. In the morning you get a separate summary to accept or reject. The limits are accident guards, not containment, and nothing caps spending; see [Security](docs/security.md).

## Get started

You do this in a conversation with your AI tool.

1. **Ask it to install Yggdrasil.** Say: "Install Yggdrasil with `uv tool install git+https://github.com/Motaghi/yggdrasil@v1.1.1`, copy the `skills/yggdrasil-l1` folder from that repository into my skills folder, and set up `ygg conductor run` as a background service with `YGG_CONFIG` pointing at my config file." A **skill** is a small instruction pack that teaches your AI tool a new job; this one teaches it how to be your main session. The **conductor** is the small program that starts the agents when there is work for them. It must keep running for Yggdrasil to work. The `@v1.1.1` pins this release; leave it off (`uv tool install git+https://github.com/Motaghi/yggdrasil`) to install the newest code on the main branch instead.
2. **Answer about five setup questions.** What the work mostly is, which AI tool you have, where to keep the records, how much the agents may do without asking, and which words you prefer. Yggdrasil picks defaults from your answers.
3. **Say "Start a Yggdrasil project".** The session asks you a few short questions about what you want, writes that up as a project brief, and sets the project up.

Prefer the terminal, or want the details? See [Getting started](docs/getting-started.md).

## Try the demo

Want to see Yggdrasil work before you set anything up? `ygg demo` runs a small project from start to finish on your computer in about two minutes. It needs no AI account and no login, and it makes no outside connection. It runs the real conductor, but the agents are scripted stand-ins that do a fixed job and never call a model. You need `git` installed.

```bash
uv tool install git+https://github.com/Motaghi/yggdrasil@v1.1.1
ygg demo
```

The demo works in a temporary folder, and it never reads or changes your config, profile, state, tracker, git settings or Claude and Codex folders. Seven progress lines show a brief written, a plan approved, two pieces of work sent for review and approved, the result merged and the project closed. It also writes a dashboard page that refreshes every two seconds. When the demo runs in a terminal, it tries to open the page in your browser (add `--no-open` to skip that); on Linux this also needs `DISPLAY` or `WAYLAND_DISPLAY`. Otherwise it prints the page's path. `--fast` skips the pauses between steps, `--keep` leaves the temporary folder in place when it finishes, and `ygg demo --cleanup <folder>` stops and removes a folder that a killed demo left behind. If the demo is killed without cleaning up, its background process keeps running until you run `ygg demo --cleanup <folder>`. Only you can start it, not an agent session. See the [CLI reference](docs/cli-reference.md#demo) for the details.

## What a day looks like

- **You answer a few questions** when it suits you. Each comes with a recommended answer, and a question waiting for you holds up only the work that depends on it.
- **You approve plans and phases.** Build work waits until you approve the project plan, and each phase waits for your OK before the next one starts, unless you granted an envelope for it.
- **You read a morning summary** after overnight work. A separate reviewer lists everything that was done without you, and you mark each item ok or not ok.

## Words

Yggdrasil's flow is inspired by PRINCE2®, a project method built around phases and clear approval points. You don't need to know it. The default plain words are project plan, phase and progress report; if you already work with PRINCE2, you can switch to its terms. PRINCE2® is a registered trademark of PeopleCert group. Yggdrasil is not affiliated with or endorsed by PeopleCert.

Claude Code (Anthropic), Codex (OpenAI), Linear and T3 Code are named only to say what Yggdrasil works with. Yggdrasil is not affiliated with or endorsed by Anthropic, OpenAI, Linear or the makers of T3 Code.

## Safety

Yggdrasil's checks are **accident guards**. They catch an agent that gets confused, misreads its role or takes a shortcut. They do **not** contain a hostile agent, or an agent that follows instructions planted in a web page, file or message it reads. Such an agent can get around them, because agents run as your own OS user. The ways around are listed under [Known gaps](docs/security.md#known-gaps). Containment, with a separate broker user and a sandbox per agent, is planned for a later release.

What the guards do today:

- Every Claude Code agent session gets a guard hook that refuses shell commands which push to anything other than `ygg/...` branches, force-push, delete, rewrite history, skip the push hook, write git refs directly or send data out by a known pattern.
- Codex builders have no guard hook. Each runs in Codex's sandbox, with no network and write access only to its own box or scratch folder and its relay folder; its `ygg` calls run through a relay with its own identity, and its commits reach the project only through a receive step that runs the lane checks again in the home. What the sandbox cannot close is listed under [Known gaps](docs/security.md#known-gaps).
- A pre-push check lets an agent push only its own project branch, only to the remote you had when you approved the plan, and refuses any push that touches CI workflows unless the approved plan allows workflow changes.
- Approving plans and phases, deciding questions, granting overnight envelopes, stopping and resuming are refused from every session the conductor started. Your main session is an AI session that holds your authority and runs them for you. Its instructions tell it to act only on your answers; nothing checks that (see [Known gaps](docs/security.md#known-gaps)).
- You can pause or stop everything at any time. A Claude Code session halts at its next tool call. A paused Codex builder's `ygg` calls are refused, but the builder itself keeps working in its box until it ends, so use `ygg stop` to end one now: stop ends a Codex builder's process the same way as a Claude Code agent's.

Until containment ships, run Yggdrasil on work that is backed up and not confidential, in an OS account or machine that holds no credentials you don't want agents to reach, and watch what it does. Nothing limits how much model usage the agents spend. The details are in [Security](docs/security.md).

## Status

| Part | Status |
|---|---|
| The core: the conductor, approvals, the files tracker, starting agents headless with Claude Code or Codex | In v1.1.1 |
| Projects on a folder of documents (`vault-docs`) | Preview in v1.1.1: no automatic per-piece review |
| PMs, team leads, workers and senior users on Codex, each builder in its own private clone | In v1.1.1 (macOS) |
| Keeping records in Linear | In v1.1.1. v1.0.0 was proven on it end to end; v1.1.0 added a regression test against a test server. See [Trackers](docs/trackers.md#linear-tracker) |
| Starting agents in T3 Code | Experimental in v1.1.1, for Claude Code sessions; see [Launchers](docs/launchers.md#t3) and its known gaps in [Security](docs/security.md#known-gaps) |
| Building a North Star (a short statement of what a project is for) by conversation | Not in this build |
| Containment of agents (separate broker user, sandbox per agent) | Planned for a later release |
| A limit on the number of agents running at once | In v1.1.1: `max_concurrent_sessions` |
| A live demo with no AI account (`ygg demo`) | Since v1.1.0 |
| Limits on spending | Not in this build |

## Documentation

- [Getting started](docs/getting-started.md): requirements, setup, the terminal route and the background service
- [Concepts](docs/concepts.md): how the team is organised and how sessions pick up work
- [Configuration](docs/configuration.md): settings and presets
- [Trackers](docs/trackers.md): the files and Linear trackers
- [Launchers](docs/launchers.md): how agents are started
- [Autonomy](docs/autonomy.md): approvals, overnight work and the morning summary
- [PRINCE2](docs/prince2.md): how the flow maps to the method, and the two sets of words
- [Security](docs/security.md): what the guards do, and the known gaps
- [CLI reference](docs/cli-reference.md): every `ygg` command
- [Contributing](docs/contributing.md): tests, the leak scan and plugins
- [Release notes for v1.1.1](docs/releases/v1.1.1.md): what is new and the known limits; [v1.1.0](docs/releases/v1.1.0.md) lists what moved to later releases
- [Release notes for v1.0.0](docs/releases/v1.0.0.md): what was proven, what changed, and the known issues
- [Reporting a vulnerability](SECURITY.md)

## Quickstart

Optional: a command-line demo for developers, using local files and no account. It sets up records only. Don't start a conductor against this state folder unless you want it to launch a real project manager session on your AI login.

Run it in a fresh, empty folder: it creates its tracker and state folders under the current folder. `YGG_CONFIG=none` makes `ygg` ignore any config file, so a `yggdrasil.yaml` or `~/.config/yggdrasil/config.yaml` you already have is not picked up, and `unset YGG_PROFILE` drops a profile you may have set. These `export` lines change your current shell, so run the demo in a new terminal or close it afterwards.

```bash
export YGG_CONFIG=none
unset YGG_PROFILE
export YGG_TRACKER=files
export YGG_FILES_ROOT="$PWD/ygg-tracker"
export YGG_STATE_ROOT="$PWD/ygg-state"
mkdir -p demo-home
ygg programme create demo --domain demo --home "$PWD/demo-home" --kind vault-docs
ygg project create demo-p1 --programme demo --pm pm1
ygg node add pm1tl1 --role tl --parent pm1
ygg node add pm1su1 --role su --parent pm1 --pairs-with pm1tl1
ygg status
```

## Licence

MIT (see LICENSE).
