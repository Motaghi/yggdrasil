# Trackers

The tracker is where Yggdrasil writes everything that matters: plans, decisions, reports, approvals and each session's notes to itself. Because sessions are rebuilt from it, the tracker is the source of truth. `ygg rehydrate` rebuilds the conductor's state from the tracker, plus the stop and pause markers in the local safety folder (see [Security](security.md#stopping-things)).

This build has two trackers, `files` and `linear`. You pick the tracker with the `tracker` setting or `YGG_TRACKER`.

| Tracker | Status |
|---|---|
| `files` (default) | In this build |
| `linear` | In this build. Version 1.0.0 was proven on it, end to end, with a git repository. See [Linear tracker](#linear-tracker). |

The tracker is written before local state, so a failed tracker write leaves local state as it was, and the command exits with code 8. Exit 8 does not mean the whole command changed nothing: a command that writes several records may have written some of them before the one that failed. Before you repeat a command that changes state (an approval, a decision, a write), check the error and the current record with `ygg status` or in the tracker, and repeat it only if the change is missing. Stop and pause are the exception: they record their marker locally first (and stop signals its sessions), and write the tracker last. If that write fails they exit 8 but still hold, and the next conductor tick records them once the tracker can be written (see [Security](security.md#stopping-things)). Workers never write to the tracker; they report to their team lead, who does. Strings that start like known tokens are replaced with `[REDACTED]` in messages and most records (see [Security](security.md#secrets)).

## Files tracker

The files tracker is a folder of markdown and JSON files under `YGG_FILES_ROOT` (or `files_root` in your config; `ygg init` writes `./ygg-tracker` next to the config file). It needs no account, works offline and is friendly to git, so you can keep it in a repo and see every change as a diff. Each file is locked while it is written and replaced in one step, so a crash never leaves half a file.

The layout:

```text
initiatives/<programme>/initiative.json      the programme
initiatives/<programme>/documents/           North Star, journal, autonomy.yaml
projects/<project>/project.json              the project
projects/<project>/milestones.json           phases
projects/<project>/issues/<id>.md            work items, decisions, concerns, problems, risks
projects/<project>/updates/<time>.md         progress reports
projects/<project>/documents/<slug>.md       project plan, phase plans, route map, reports
_ygg/counters.json                           the last number given out per record type
```

Every record carries the tracker standard version it was written under and an `actor` field (`user` for your approvals, `ygg-app` for writes on behalf of a node). Agents run as your OS user and can edit these files directly; the `actor` field is not a signature. See [Security](security.md#known-gaps).

## Linear tracker

The Linear tracker keeps the records in one Linear team. Yggdrasil still works from a local copy of them, the cache, in `linear-cache` in the state folder, laid out like the files tracker. Every write goes to Linear first and to the cache second. If the Linear call fails, the cache is not changed and the command exits with code 8. That does not always mean Linear is unchanged: when a call fails in a way that leaves its outcome unknown, Linear may still have applied it, and the next conductor pass or `ygg rehydrate` brings the cache up to what Linear holds. After a work item is written, its new records are also posted as Linear comments, and that step can fail after the record is already saved in Linear and in the cache; the comments are posted with the item's next write. So check the error and the current record before you repeat a command that changes state. Reads come from the cache. On each conductor pass, one request checks whether the journals or the id counters changed in Linear, and only what changed is read again. `ygg rehydrate` rebuilds the cache from Linear alone.

### Setting it up

1. **A Linear app.** Yggdrasil signs in to Linear as an OAuth app, with the client credentials grant: it needs the app's client ID and client secret, and asks for the scopes `read`, `write`, `initiative:read` and `initiative:write`. As a workspace admin, create the app in Linear under **Settings → API → OAuth applications** (or at `https://linear.app/settings/api/applications/new`), turn on client credentials tokens for it, and copy its client ID and client secret. Without that switch Linear refuses the token request with "Client does not support the client_credentials grant type". The app's token sees the workspace's public teams; `ygg init` suggests a private team, so give the app access to that team on the app's details page. Rotating the app's secret in Linear ends its current token at once, and Yggdrasil then needs the new secret. See Linear's [OAuth 2.0 authentication](https://linear.app/developers/oauth-2-0-authentication) page.
2. **Store the client ID and secret.** They never go in the config or in an environment variable, and there is no `ygg` command for this step: you add them yourself, from your own terminal, in one of two places.
   - **The macOS Keychain** (the default). Two generic password items under the service `yggdrasil-linear`, one with the account `client_id` and one with the account `client_secret`:

     ```bash
     security add-generic-password -s yggdrasil-linear -a client_id -w
     security add-generic-password -s yggdrasil-linear -a client_secret -w
     ```

     With `-w` last, `security` asks for the value, so it stays out of your shell history. `linear_keychain_service` changes the service name.
   - **A file.** Set `linear_token_store: file` and `linear_token_file` to its absolute path (or one that starts with `~`). Unlike other paths in the config, it is not resolved against the config file's folder, so a relative path would depend on the folder each command runs in. The file holds JSON, `{"client_id": "...", "client_secret": "..."}`. It must be a regular file owned by you with mode 0600, in a folder that you own and that only you can write to. Use this where there is no `security` command, such as on Linux. Because it holds the app's client secret and, once used, a live access token, keep it out of cloud-synced folders (iCloud Drive, Dropbox, OneDrive and the like), out of folders that are backed up to another machine or service, and out of every git repository, including your Yggdrasil config folder if you keep that in git. If a copy does end up in one of those places, rotate the app's secret in Linear and store the new one.
3. **Choose the team.** Set `linear_team_key` to the team's key, the short prefix of its issue ids. The default is `YGG`.
4. **Select the tracker.** Set `tracker: linear` in your config, answer `linear` to the records question in `ygg init`, or use the `team` preset.

On first use Yggdrasil requests an access token with the client ID and secret, and saves it in the same store before using it (the Keychain account `access_token`, or the `access_token` and `expires_at` fields of the file). It requests a new one 3 days before the token expires, or when Linear refuses it. There is no refresh token. If it can't get a token, commands exit with code 8. One `ygg-tracker-auth` alert is raised for each run of failures, not for every command. The token is never put in an environment variable, so agent sessions don't see it. Agents can still read the Keychain item or the file as your OS user; see [Security](security.md#known-gaps).

Other Linear settings: `linear_authority_actor_ids` (see below), `linear_deadline_seconds` (default 30: how long one Linear request may take, and the longest rate-limit wait Yggdrasil accepts before a call fails) and `linear_api_url` (default `https://api.linear.app`; it must be an `https` address on a `linear.app` host).

Under Linear the conductor runs a pass every 30 seconds unless you set `tick_seconds`, and `settle_delay_seconds` must be more than zero.

### One Linear team per installation

Use one Linear team for each Yggdrasil installation. An installation here is one state folder (`state_root`). Its safety folder, which holds the stop and pause markers, sits next to it as `<state folder>.safety`. The first time an installation uses the team, the team's journal records that safety folder. A second installation on the same team has a different safety folder, so it refuses every launch with `safety folder ... missing or unreadable`.

### What Yggdrasil creates in Linear

- An initiative for each programme, holding its journal, North Star and autonomy documents.
- A Linear project for each project, in your team and linked to its initiative, holding the project plan, phase plans and reports as documents.
- An issue for each work item, decision, concern, problem and risk. The issue's description holds the record. Each new record or comment on it is also posted as a Linear comment. Worker tasks are sub-issues of their work item. Phases are project milestones named `Stage <n>`.
- A project update, with its health, for each progress report.
- A control project, `_ygg-control`, holding the id counters, the root journal and a registry of everything the app created.

Each record is stored inside a code block, under a note that says to edit it through `ygg`, not by hand.

### Edits made in Linear

Yggdrasil reads back only what its own app wrote. A record created or last changed in Linear by someone else is not adopted: it raises a `ygg-linear-foreign` alert, and writes to it are held. A journal or counter that changed in Linear in any way other than growing (an entry removed or rewritten, a counter going down) raises a `ygg-tracker-rewrite` alert, and writes to it are held too. `linear_authority_actor_ids` lists Linear user ids (not names) whose changes to Yggdrasil's documents count as the app's own; it does not cover issues. The journals and counters are checked on every pass. A person's edit to any other issue or document is noticed only at the next `ygg rehydrate` or the next write to it.

## The tracker standard

How the project items map onto the tracker is written down in one versioned file, packaged with Yggdrasil (`src/yggdrasil/data/tracker-standard.yaml` in the repository). It lists the states of work items and phases, the labels for decisions, and the kinds of reports and documents. `ygg standard check` validates the packaged copy; `ygg standard check --file <path>` validates another file. The copy in `standards/` is a reference copy of the same file and is not read at run time.

The current version is 2. It adds the work item states `in-review`, `stopped` and `cancelled` to the eight states of version 1, which keep their meaning; the other entries are the same in both versions.

Every record carries the version it was written under, and readers use the matching entry, so a project with records from several versions still loads correctly.

| Item | Files tracker | Linear tracker |
|---|---|---|
| Programme | `initiatives/<programme>/initiative.json` | initiative |
| Project | `projects/<project>/project.json` | project in your team, linked to the initiative |
| Phase | entry in `milestones.json` | project milestone `Stage <n>` |
| Work item | `issues/<id>.md` | issue titled `<id> <title>` |
| Worker task | issue file with `parent: <id>` | sub-issue |
| Criteria, review and acceptance records | entries under `## Records` | in the issue description, and posted as a comment |
| Progress report | `updates/<time>.md` | project update |
| Check-in | entry under `## Comments` | in the issue description, and posted as a comment |
| Decisions, concerns, problems, risks, rework | issue files with those labels | issues with those labels |
| Project plan, phase plans, reports, lessons | `documents/<slug>.md` | project documents |
| North Star, journal | `initiatives/<programme>/documents/` | initiative documents |

## Other trackers

Third-party trackers can be added as plugins. In this build a tracker plugin must keep its records in the files-tracker format and provide a `root()` method that returns their folder. See [Contributing](contributing.md).
