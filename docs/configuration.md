# Configuration

Yggdrasil works after one `ygg init` with no other setup. Its behaviour can still be changed through configuration, without editing the code.

## Layers

Settings come from layers. A higher layer replaces the value of any key it sets. Lists are replaced, not merged.

| Layer, lowest first | Where it lives | Shown by `config explain` as |
|---|---|---|
| Core defaults | `data/yggdrasil.yaml` inside the package | `core` |
| Preset | the preset named by the `preset` key | `preset:<name>` |
| Your config | the file `ygg init` wrote, found through `YGG_CONFIG`, else `./yggdrasil.yaml` in the current folder, else `~/.config/yggdrasil/config.yaml` | `config:<path>` |
| Profile | the YAML file named by `YGG_PROFILE` | `profile:<path>` |
| Programme | the programme's tracker record, key `config` | `programme:<name>` |
| Project plan | top-level keys of the project plan's frontmatter | `pid:<project>` |
| Work item | the work item's `config` field | `package:<project>/<id>` |
| Environment | `YGG_TRACKER`, `YGG_STATE_ROOT`, `YGG_FILES_ROOT` and `YGG_PLUGIN_PATH`, which beat every file for their key | `env:<NAME>` |

Your agreement to the agents' permissions is not a configuration layer. `ygg agents` records it in `_ygg/agents-agreement.json` in the files tracker folder, per programme (see [Security](security.md#what-agents-may-do-without-asking)).

The current-folder lookup applies only to your own shell. Agent sessions never read `./yggdrasil.yaml`; the conductor passes them its own config path. A background service runs in another folder, so it needs `YGG_CONFIG` (see [Getting started](getting-started.md#keeping-the-conductor-running)).

Relative paths in your config resolve against the folder that holds the config file.

Each key has a scope. **Global** keys (`tracker`, `launcher`, `launcher_cli`, `vocabulary`, `alert_forwarder`, `state_root`, `files_root`, `plugin_paths`, `agent_env_passthrough`, the Linear keys, the profile keys and the timing keys below) may be set only in the core, preset, your config, a profile or the environment. **Project** keys (`models`, `review_rounds`, `delivery_approach`, `agile_cadence`, `cadence`, `wip_limit`, `timebox_hours`, `merge_mode`, `branch_protection`) may also be set for a programme, a project plan or a single work item.

Security-relevant keys are honoured only from layers you control. `authority`, `state_root`, `files_root`, `plugin_paths`, `merge_mode` and `branch_protection` are ignored when set in a programme record or a work item, and a project plan's value counts only as you approved it. `ygg config explain` lists ignored values under `ignored`, and `ygg merge main --print` names the same `merge_mode` value that `config explain` shows.

The model map has its own resolution: a programme record's `models` field, then the approved project plan's `models`, then a work item's `models`, each replacing the keys it sets. `ygg resolve-model <node>` shows the result for a node.

A profile is an optional extra layer for a whole setup. Without one, Yggdrasil runs on the core defaults. `ygg profile show` prints the loaded profile, with secret-looking values hidden, or `none`.

## Presets

| Preset | For | What it sets |
|---|---|---|
| `starter` | Most people. Used when no preset is named. | The same as the core defaults: files tracker, headless launcher with Claude Code, agile delivery in a continuous flow, branch protection required, plain words, 5 review rounds, alerts to a local file and delivered to you (`auto`). No standing permissions and no overnight work until you grant it. |
| `solo-builder` | One person building software. | Like `starter`, plus at most 3 unfinished work items at a time (`wip_limit: 3`) and 2 review rounds. |
| `team` | Several people sharing one tracker. | Records in Linear (see [Trackers](trackers.md#linear-tracker)), headless launcher with Claude Code, agile delivery in a continuous flow, branch protection required, plain words, 5 review rounds. Set `linear_team_key` and store the Linear app's credentials before first use. |

## Checking your configuration

`ygg config explain` shows the value of every key and the layer it came from. Add a key name to see one, `--project` or `--package` to see the value for a project or work item, and `--json` for machine-readable output:

```bash
ygg config explain
ygg config explain review_rounds --project website-redesign-p1
```

`ygg config validate` loads every layer and refuses:

- unknown keys, suggesting the closest known key (`unknown key 'reveiw_rounds'; did you mean 'review_rounds'?`);
- keys set at a layer their scope forbids (`review_rounds` is fine in a project plan, `tracker` is not);
- unknown plugin names, listing the ones available;
- plugins with an unsupported `api_version`.

Every command except `init`, `config`, `plugins`, `profile`, `hook` and `_supervise` runs the same validation first and stops with the same message if something is wrong.

Selecting a plugin, or adding `plugin_paths`, makes `ygg` import Python code. Use only plugins and config files you trust.

## Key settings

Defaults are those in the core config.

| Key | Default | What it does |
|---|---|---|
| `tracker` | `files` | Where records go: `files` or `linear`. See [Trackers](trackers.md). |
| `files_root` | `./ygg-tracker` in the current folder; `ygg init` writes `./ygg-tracker` next to the config | Folder for tracker files. |
| `state_root` | `~/.local/state/yggdrasil`; `ygg init` writes `./ygg-state` next to the config | Folder for local working state. |
| `launcher` | `headless` | How sessions start: `headless` or `t3`. See [Launchers](launchers.md). |
| `launcher_cli` | `claude-code` | Keep `claude-code` in v1.0. `codex` makes `codex_models` the default model map, which puts every role on a GPT-family model, and v1.0 refuses Codex for project managers, team leads, workers and senior users ("Codex builds come in v1.1"). `ygg init` writes `codex` if you answer Codex alone, or if Codex is the only tool it finds. |
| `vocabulary` | `plain` | `plain` or `prince2` labels in the `ygg status` text and the main-session skill. JSON output, record kinds and command names never change. |
| `alert_forwarder` | `auto` | Every alert is appended to `alerts.jsonl` in the state folder. With `auto`, the conductor also delivers each alert meant for you: to the main session's T3 thread when `alert_t3_thread` is set, else (or if that send fails) as a macOS notification unless `alert_macos` is `off`, and to `alert_command` when you set one, with the alert as JSON on its standard input. Delivery is retried, with longer gaps, up to `alert_max_attempts` times. `jsonl` keeps alerts in the file only. |
| `models` | `claude-opus-5-5`, effort `high`, paired with `gpt-6.1-sol`, effort `xhigh` | The model map. GPT-family models run on Codex. In v1.0 only the project reviewer may run on Codex: it uses the pair model when Codex is installed, else the main model, and `models.review_model` names another. Every other role must use a Claude model. The programme reviewer always runs on the core Claude model. |
| `codex_models` | `gpt-6.1-sol`, efforts `high` and `xhigh` | The model map used when `launcher_cli` is `codex`. Not usable in v1.0; see `launcher_cli`. |
| `review_rounds` | `5` | How many write-and-review rounds a work item gets before it goes to the project manager as a concern. |
| `delivery_approach_default` | `agile` | The delivery approach for a project plan that doesn't name one. |
| `agile_cadence_default` | `flow` | `flow` or `timebox` for agile projects. |
| `merge_mode` | `prepare` | `prepare` or `local`. In this release it changes only the wording of `ygg merge main --print`. `ygg merge main` is yours alone and merges into your local main in either mode once the merge check has passed; `--print` leaves main unchanged. Pushing main is a hard stop. |
| `branch_protection` | `refuse` | What `ygg pid approve` does when a repository's main branch lacks a safe GitHub protection policy: `refuse`, `warn` or `off`. |
| `authority.actors` | `[user]` | Whose approvals count. Setting the list replaces the default. |
| `load_threshold` | `0.85` | Host load per CPU above which new sessions wait. |
| `agent_permissions` | not a setting | Your agreement to the agent permission rules is not configuration. It is recorded only by `ygg agents allow-standard --yes` or `ygg agents allow-minimal` (withdrawn by `ygg agents revoke`) in `_ygg/agents-agreement.json` in the files tracker folder, per programme. `agent_permissions` or `agent_permissions_version` in a config file or profile is an error. See [Security](security.md#what-agents-may-do-without-asking). |
| `agent_commands` | unset (none) | Not a config or profile key. It is a list in the project plan's header, `agent_commands: [npm test, npm run build]`: the test and build commands the project's team leads, workers and senior users may run without asking, under the standard rules. Each is one plain command; its arguments are allowed. Only the header you approved with `ygg pid approve` counts. See [Security](security.md#what-agents-may-do-without-asking). |
| `agent_project_settings` | `exclude` | `include` lets Claude Code agents load their project's `.claude/settings.json` and `settings.local.json` (hooks and permission rules); by default they read only the project's `CLAUDE.md`. Only your own config or profile counts. |
| `max_concurrent_sessions` | unset (no limit) | Most agent sessions running at once; the conductor holds further launches until one ends. |
| `stall_threshold_minutes` | `20` | How long a dead session may stay silent before it is restarted. |
| `max_silence_minutes` | `60` | How long a live session may stay silent before it is flagged. |
| `retry_limit` | `2` | Restarts before a session is marked stalled. |
| `settle_delay_seconds` | `30` | Pause between session starts before load is measured again. |
| `stop_grace_seconds` | `10` | How long an emergency stop waits before killing. |
| `l1_lock_ttl_minutes` | `30` | Time limit on the main session's lock. |
| `checkpoint_batch_hours` | `4` | Longest a project manager waits to process check-ins in one batch. |
| `plugin_paths` | none | Extra folders searched for plugins. `YGG_PLUGIN_PATH` does the same. |
| `agent_env_passthrough` | `[]` | Extra environment variables passed from the conductor's environment to agent sessions, by name. See [Agent environment](#agent-environment). Only your own config or profile counts. |
| `linear_team_key` | `YGG` | With `tracker: linear`, the key of the Linear team that holds the records. Use one team per installation. See [Trackers](trackers.md#linear-tracker). |
| `linear_token_store` | `keychain` | Where the Linear app's client ID, secret and access token are kept: `keychain` (macOS) or `file`. |
| `linear_keychain_service` | `yggdrasil-linear` | The Keychain service name of those items. |
| `linear_token_file` | unset | With `linear_token_store: file`, the path of the 0600 JSON file that holds them. |
| `linear_authority_actor_ids` | unset (none) | Linear user ids whose changes to Yggdrasil's documents in Linear count as the app's own. |

## The per-tick launch cap

One conductor pass ("tick") limits how many sessions it starts. This paces starts; it is not a cap on how many sessions run or what they cost.

| Key | Default | Meaning |
|---|---|---|
| `tick_cap` | `both` | `count`, `time`, or `both` (whichever limit comes first). |
| `max_launches_per_tick` | `5` | Most sessions one tick starts. Used by `count` and `both`. |
| `tick_budget_seconds` | `240` | Seconds one tick may spend starting sessions. Used by `time` and `both`. |

Only sessions that actually started count. The first start of a tick is never put off. Anything the tick didn't reach stays queued for the next tick. No preset changes these keys.

## Agent environment

Headless agent sessions don't get the conductor's whole environment. They get an allowlist: basic variables such as `PATH`, `HOME`, `LANG`, the `LC_` variables, the proxy and certificate variables, `SSH_AUTH_SOCK`, plus the credentials of the tool they run (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CONFIG_DIR` for Claude Code; `OPENAI_API_KEY`, `OPENAI_BASE_URL` and `CODEX_HOME` for Codex). `YGG_` variables pass too, except the test controls (`YGG_TEST_MODE`, `YGG_NOW`), `YGG_SUPERVISOR_FOR` and `YGG_LINEAR_TOKEN`, and the identity variables (`YGG_NODE` and the others) are always set by Yggdrasil itself. `TZ` is dropped and set from the profile's `timezone` instead. Every other variable in the conductor's environment is dropped. A profile's `hub_base_url` and `launch_env` then add their values.

`SSH_AUTH_SOCK` points at your running SSH agent. Passing it lets agents use git over SSH, and it also lets them use every key loaded in that SSH agent, for any SSH connection, without seeing the keys. If the SSH agent the conductor sees holds keys you don't want agents to use, unset `SSH_AUTH_SOCK` before you start the conductor (that stops passing your SSH agent to them; it doesn't stop an agent reading SSH key files or a separately configured agent socket that your OS user can read), or run it from an OS account whose SSH keys and SSH agent hold only what the projects need. See [Security](security.md#known-gaps).

`agent_env_passthrough` adds variables to that list by name. A launch is refused if it names `PATH` or a `GIT_` or `YGG_` variable, and `LINEAR_` variables are never passed. Naming `TZ` passes the conductor's `TZ` when the profile sets no `timezone`. It counts only from the config `YGG_CONFIG` names, the home config or a profile; a config found in the current folder may not set it, and no environment variable sets it.

**T3 sessions** don't use this allowlist. A T3 session runs in the environment of the T3 server that starts it. Yggdrasil adds only its identity and folder variables (`YGG_NODE`, `YGG_ACTIVATION`, `YGG_PROGRAMME`, `YGG_PROJECT`, the state, tracker, config and profile paths, and `YGG_TRACKER` when it is set), through the session's settings file. `agent_env_passthrough` has no effect there.

**Codex through a proxy or another provider.** If your Codex config names a provider that reads its API key from an environment variable (the provider's `env_key`), and that variable is not one of the Codex names above, list it under `agent_env_passthrough`. Otherwise it is dropped, and Codex sessions, such as the reviewers, fail at start:

```yaml
agent_env_passthrough: [MY_PROVIDER_API_KEY]
```

## Profile keys

A profile may set any global key. These profile keys are read in this build:

| Key | What it does |
|---|---|
| `hub_base_url` | Exported to headless agent sessions as `ANTHROPIC_BASE_URL`, for a proxy in front of Claude. Not applied to T3 sessions. |
| `launch_env` | Extra environment variables for headless agent sessions. It may not set any `YGG_` variable. Not applied to T3 sessions. |
| `agent_claude_config_dir` | A Claude Code settings folder for headless agent sessions, passed to them as `CLAUDE_CONFIG_DIR` (T3 sessions use the T3 server's environment instead), so they run without your own instructions, hooks and skills. The core default is `~/.config/yggdrasil/agent-claude`, which Yggdrasil creates with mode 0700. Agents then may not see the sign-in kept in your own Claude Code folder, so give them one through the environment (see [Getting started](getting-started.md#what-you-need)). |
| `digest_dir`, `digest_filename` | Where `ygg digest` writes (default `digests/<date>.md` in the state folder). `{date}` in the file name is replaced. |
| `platform_pause_dir` | A folder whose `pause` file stops all launches. |
| `pools` | Names shown in `ygg resolve-model` output. They do not change how sessions start. |

These profile keys are used too:

- `timezone`: an IANA zone name such as `Europe/Copenhagen`. Headless agent sessions get it as `TZ` (an unknown zone refuses the launch, and `launch_env` may not set `TZ`); T3 sessions keep the T3 server's own. The merge check and the dashboard use it as well.
- `commit_identity`: `name` and `email` become the git `user.name` and `user.email` of each agent worktree. `trailers: none` makes the lane commit check refuse a `Co-Authored-By` trailer in an agent's commit; the default, `allow`, lets it through.
- `leak_denylist`: the path of a file of terms, one per line (`#` lines are comments, case is ignored). The lane commit check refuses an agent's commit whose staged content, file names or message holds one of them. It reads only this key, never `YGG_LEAK_DENYLIST` or git config, which belong to the repository's own leak scan (see [Contributing](contributing.md)).

`name` and `hub_state_file` are accepted but not used by Yggdrasil itself in this build.

## Environment variables

- `YGG_CONFIG`: your config file. `none` means no config file. Without it, `ygg` reads `./yggdrasil.yaml` in the current folder, else `~/.config/yggdrasil/config.yaml`, so a command run from another folder doesn't see your model map or other settings. `ygg config explain <key>` shows which file a value came from.
- `YGG_PROFILE`: a profile.
- `YGG_TRACKER`, `YGG_FILES_ROOT`, `YGG_STATE_ROOT`, `YGG_PLUGIN_PATH`: override the matching keys.
- `YGG_CLAUDE_BIN`, `YGG_CODEX_BIN`, `YGG_GH_BIN`: paths of the Claude Code, Codex and GitHub CLIs, if they are not `claude`, `codex` and `gh` on `PATH`.
- `CLAUDE_PLATFORM_ENABLED=0`, or a `pause` file in the folder named by `YGG_PLATFORM_STATE`: stops all launches.
- `YGG_TEST_MODE` and `YGG_NOW` are for the test suite. They are never passed to agent sessions, and `YGG_PLATFORM_STATE` is ignored inside agent sessions.
