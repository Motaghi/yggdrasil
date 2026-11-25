# Getting started

This guide takes you from nothing to a first project. The normal way to use Yggdrasil is to talk to your **main session** (the docs call it L1): one AI session in Claude Code or Codex that sets things up and runs the `ygg` commands for you. Every step can also be done by hand; see [If you prefer the terminal](#if-you-prefer-the-terminal).

Read [Security](security.md) first. The guards in this version catch accidents; they don't contain an agent that follows planted instructions.

## What you need

- **macOS or Linux.** Yggdrasil uses POSIX file locks, `ps` and the load average; Windows is not supported.
- **Python 3.12 or later**, and `uv` or `pipx` to install the `ygg` command.
- **Claude Code and Codex, both installed and logged in.** In v1.1 every session runs on Claude Code except the project reviewer, which checks each work package on a GPT-family model through Codex, so the review comes from another model family. Without Codex the project reviewer runs on the Claude model instead. A Codex-only setup is not supported in v1.1: project managers, team leads, workers and senior users on Codex are refused ("Codex builds are not in this release").
- **Your agreement to the agents' permissions.** Yggdrasil starts Claude Code as `claude -p --settings <file> --model <model> --effort <effort>`, with the prompt on standard input, and passes no permission flags. Agents run with Yggdrasil's own Claude Code settings folder, so what they may do without asking comes from the rules you agree to once with `ygg agents allow-standard --yes` or `ygg agents allow-minimal`. Until you agree, launches are refused. See [Security](security.md#what-agents-may-do-without-asking).
- **A Claude Code sign-in the agents can use.** Headless Claude Code agents don't run with your own Claude Code folder (`~/.claude`). By default Yggdrasil points them at its own folder, `~/.config/yggdrasil/agent-claude` (the `agent_claude_config_dir` setting), through `CLAUDE_CONFIG_DIR`. That keeps your own instructions, hooks, skills and settings away from agents, but it also means the sign-in kept in your own Claude Code folder may not reach them. Give the conductor's environment a credential Claude Code reads from the environment, such as `CLAUDE_CODE_OAUTH_TOKEN` (made with `claude setup-token`) or `ANTHROPIC_API_KEY`; those pass to agents (see [Agent environment](configuration.md#agent-environment)). Or sign in once inside Yggdrasil's folder with `CLAUDE_CONFIG_DIR=~/.config/yggdrasil/agent-claude claude`. Codex sessions use your own Codex login. T3 sessions are different: they run in the T3 server's environment, so their sign-in and settings are whatever that server uses.
- **git**, for projects that work on code or other files kept in a git repo.
- **For each git repository the agents work on:** a GitHub remote, a default branch named `main` with branch protection on GitHub, and the `gh` command installed and logged in with a GitHub account that can read that protection. `gh` is needed for one thing: when you run `ygg pid approve`, Yggdrasil calls `gh api` to read the branch protection and rulesets on `main`, and by default it refuses to approve a plan without safe protection (see [Security](security.md#main-is-protected-by-the-server)). That call runs in your own shell, as your OS account, with your `gh` login. Agents are not meant to use `gh`. Their permission rules allow `ygg`, a few git commands and the test and build commands named in the plan you approved, so a Claude Code agent can't run `gh` unless that plan names it, and `GH_TOKEN` is not passed to them. An agent that reaches `gh` some other way, through a script for example, finds your `gh` login, because it runs as your OS user too (see [Known gaps](security.md#known-gaps)). Branch protection on private repositories may need a paid GitHub plan.
- **A computer that stays on** while work runs.

By default, records are kept in local files and agents are started headless. To keep the records in Linear instead, you also need a Linear app and a team for it; see [Trackers](trackers.md#linear-tracker). Starting Claude Code sessions in T3 Code is optional; see [Launchers](launchers.md#t3).

## The three steps

You do all three in a conversation with your AI tool.

### 1. Ask it to install Yggdrasil

Say:

> Install Yggdrasil with `uv tool install git+https://github.com/Motaghi/yggdrasil@v1.1.0`, copy the `skills/yggdrasil-l1` folder from that repository into my skills folder, and set up `ygg conductor run` as a background service with `YGG_CONFIG` pointing at my config file.

This does three things:

- installs the `ygg` command (with uv, into `~/.local/bin`), pinned to release v1.1.0. Leave off `@v1.1.0` to install the newest code on the main branch instead;
- adds the `yggdrasil-l1` **skill**, a small instruction pack that teaches your AI tool how to be your main session (see [Installing the main-session skill](#installing-the-main-session-skill));
- sets up the **conductor**, the small program that starts the agents when there is work for them. It uses no AI itself.

The conductor is required: while it isn't running, no agent starts. It must also outlive the chat that started it, so it runs as a background service (see [Keeping the conductor running](#keeping-the-conductor-running)). The service needs the config file from step 2, so the session sets it up after `ygg init`.

### 2. Answer about five setup questions

The session runs `ygg init` in the folder that will hold your Yggdrasil config and records (for example `~/yggdrasil`) and asks you:

1. **What is the work mostly?** Building software, writing and research, business tasks, or a mix.
2. **Which AI tool do you have?** Claude Code, Codex, both, or T3 Code. Answer Claude Code or both: Codex alone gives a Codex-only setup, which v1.1 doesn't support.
3. **Where should the records be kept?** In files on this computer, or in Linear.
4. **How much may the agents do without asking?** Ask me before each phase, or continue overnight within limits I set.
5. **Which words should Yggdrasil use?** Plain words (recommended), or PRINCE2® terms.

`files` needs no account. If you answer `linear` for question 3, set up the Linear app, its credentials and the team key before the first project, as described in [Trackers](trackers.md#linear-tracker); until then, every command that needs Linear stops with exit code 8. If you answer T3 Code for question 2, the config records `launcher: t3`; see [Launchers](launchers.md#t3) for what else T3 needs.

From your answers Yggdrasil picks a preset, a ready-made set of defaults, either `starter` or `solo-builder`. `team` is used only when you ask for it with `ygg init --preset team`; answering `linear` keeps the picked preset and sets the tracker to Linear.

- `starter`: the default for most people. Records in local files, your own AI tool, your approval at every phase.
- `solo-builder`: suggested if you build software and allow overnight work. Like starter, but at most three pieces of work in progress at once and two review rounds instead of five.
- `team` (only with `--preset team`): keeps the records in Linear, for several people sharing one tracker. It needs the Linear setup described in [Trackers](trackers.md#linear-tracker).

It saves your choices in `yggdrasil.yaml`, with a plain comment above every line, and keeps the records in `ygg-tracker` and `ygg-state` next to it. [Configuration](configuration.md) explains every setting.

### 3. Say "Start a Yggdrasil project"

Here is what happens next.

1. **A few short questions.** The session asks what you want to achieve, why, what done looks like and what the work must not do. Your answers become the project brief, which is kept word for word and passed to every reviewer. If the result must match another tool's output exactly, say in the brief which machine or locale counts: the same tool can answer differently on another system (macOS and GNU `wc`, for example, treat a no-break space differently).
2. **The setup.** The session creates the programme (the area of work, with its home folder) and the project, and registers the project manager, the first team lead and senior user, and one programme reviewer for the morning summary (`ygg node add rv1 --role reviewer --parent <programme>`; only you, through the main session, may add a reviewer).
3. **The project plan.** The conductor starts the project manager, which writes the project plan: scope, phases, what each result must be, how quality is checked, limits on time, scope, quality and risk, and which repos the work may touch. Any questions come back to you through the main session.
4. **Your approval.** The session shows you the plan. You approve it, or send it back with a note. Until you approve, only planning and criteria writing happen.
5. **The work.** Team leads and workers build, reviewers test, and the project manager reports. At the end of each phase you approve the next one, unless you have granted an envelope.

To see where things stand, ask the main session for a status.

## Installing the main-session skill

The repository ships the main-session skill as `skills/yggdrasil-l1/SKILL.md`; the installed package does not include it, so you copy it from a clone of the repository. It teaches your AI tool to run the project interview, report status, ask you about open decisions and call `ygg` for you. Copy the `yggdrasil-l1` folder into your tool's skills folder, then start a new session:

- Claude Code: `~/.claude/skills/` (all your projects) or `.claude/skills/` inside one project.
- Codex: your Codex skills folder, usually `~/.codex/skills/`.

For example: `git clone https://github.com/Motaghi/yggdrasil`, then from the top folder of the clone `mkdir -p ~/.claude/skills && cp -R skills/yggdrasil-l1 ~/.claude/skills/`. Set `YGG_CONFIG` (and `YGG_PROFILE`, `YGG_STATE_ROOT` and `YGG_FILES_ROOT` if you use them) in the session's environment, or start the session in the folder that holds `yggdrasil.yaml`. A skill in `~/.claude/skills/` is loaded in every Claude Code session you open, so copy it again when you update Yggdrasil.

## If you prefer the terminal

Every step above can be done by hand.

**Install** with uv or pipx:

```bash
uv tool install git+https://github.com/Motaghi/yggdrasil@v1.1.0
pipx install git+https://github.com/Motaghi/yggdrasil@v1.1.0
```

These install release v1.1.0. To install the newest code on the main branch instead, leave off `@v1.1.0`, for example `uv tool install git+https://github.com/Motaghi/yggdrasil`.

Or install from a clone, which also gives you the skill and the examples:

```bash
git clone https://github.com/Motaghi/yggdrasil
cd yggdrasil
uv tool install .
```

Check it worked with `ygg --help`.

**Run the setup** in the folder where you want Yggdrasil to keep its config and records:

```bash
mkdir -p ~/yggdrasil
cd ~/yggdrasil
ygg init
```

It asks the five questions from step 2 (press Enter on any of them for the default) and prints the next commands to run. Useful options:

- `ygg init --preset starter` fixes the preset.
- `ygg init --non-interactive` takes every answer from the preset.
- `ygg init --answers answers.yaml` reads the answers from a file.
- `ygg init --out path/to/config.yaml` writes the config somewhere else.
- `ygg init --force` overwrites an existing config, which is otherwise never replaced.

Run `ygg config validate` afterwards to check the result.

**Add the skill** as described in [Installing the main-session skill](#installing-the-main-session-skill).

**Start the conductor** in the same folder, to try it out:

```bash
ygg conductor run
```

**Check status** at any time with `ygg status`.

## Keeping the conductor running

`ygg conductor run` runs the conductor in the foreground, so it stops when you close that terminal. That is fine for trying things out. For real use, run it as a background service that starts with your computer. The core package doesn't install one for you.

A background service starts in a different folder from yours, so it can't find `yggdrasil.yaml` on its own. It needs three things:

- `YGG_CONFIG` set to the absolute path of your `yggdrasil.yaml`. The config's relative `ygg-tracker` and `ygg-state` folders then resolve next to it.
- A `PATH` that contains `ygg`, `claude`, `codex`, `gh` and `git`. Every launch refuses to start when `ygg` is not on the conductor's `PATH`, because the guard hooks call it by its absolute path.
- The command `ygg conductor run`.

On Linux, a systemd user service for a config in `~/yggdrasil` and `ygg` installed with uv looks like this, saved as `~/.config/systemd/user/yggdrasil-conductor.service`:

```ini
[Unit]
Description=Yggdrasil conductor

[Service]
Environment=YGG_CONFIG=%h/yggdrasil/yggdrasil.yaml
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/ygg conductor run
Restart=on-failure

[Install]
WantedBy=default.target
```

Add the folders where `claude`, `codex` and `gh` live to `PATH` if they are elsewhere, then run `systemctl --user daemon-reload` and `systemctl --user enable --now yggdrasil-conductor`. On macOS, a LaunchAgent needs the same three things, with absolute paths, because launchd doesn't expand `~`.

Only one conductor runs at a time; a second one exits straight away with exit code 5. The conductor writes a heartbeat file (`conductor.heartbeat`) in the state folder on every pass, so you can check that it is alive.

## Advanced

**The main session's lock.** The main session can run on any host that can call `ygg`. It attaches with `ygg attach --session <id>`, which holds a lock so only one main session acts at a time. The lock is cooperative: approvals and decisions don't check it, so the skill renews it with `ygg attach` before each reserved command. It has a time limit (30 minutes by default, `l1_lock_ttl_minutes`), and you can take it over from a new session with `ygg attach --session <id> --takeover`.

**Trying it without an AI tool.** The README's Quickstart sets up a demo programme with local files and no account. `examples/demo-programme/` in the repository holds a ready-made tracker tree. Both demos create their tracker and state folders under the current folder, so run them in a fresh, empty folder. `YGG_CONFIG=none` makes `ygg` ignore every config file, so a `yggdrasil.yaml` or `~/.config/yggdrasil/config.yaml` you already have is not picked up, and `unset YGG_PROFILE` drops a profile you may have set. Replace `<clone>` with the path of your clone of the repository:

```bash
mkdir ygg-demo && cd ygg-demo
cp -R <clone>/examples/demo-programme/tracker ./ygg-tracker
export YGG_CONFIG=none YGG_TRACKER=files YGG_FILES_ROOT="$PWD/ygg-tracker" YGG_STATE_ROOT="$PWD/ygg-state"
unset YGG_PROFILE
ygg rehydrate
ygg status
```

Both demos only write records. A conductor started against the same state folder launches a real project manager session on your AI login.

## Next

- [Concepts](concepts.md) explains how the team is organised and how sessions pick up work.
- [Autonomy](autonomy.md) explains overnight work and the morning summary.
- [Security](security.md) explains what the checks do and don't cover.
