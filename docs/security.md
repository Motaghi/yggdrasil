# Security

Yggdrasil lets agents commit, push branches, write to the tracker and raise alerts on their own. This page says what the guards do, how, and where they stop.

## Threat model

The guards in this version are **accident guards**. They are built for an agent that misreads its role, drifts off course or takes a shortcut, and they make the common wrong moves fail loudly.

They are **not containment**. They do not hold back a deliberately hostile agent, and they do not hold back an agent that follows instructions planted in text it reads: a web page, a file in the repo, a tracker record or a message. Prompt injection is outside what this version defends against. A planted instruction can use any of the bypasses under [Known gaps](#known-gaps).

The reason is architectural. Agents run as your own OS user, and the checks are mostly text matching on shell commands. Containment needs a broker running as a separate OS user, which owns credentials and approvals, plus an OS sandbox around every agent. That is planned for v1.1.

Until then:

- run Yggdrasil on work that is backed up and not confidential;
- run it in an OS account or machine that holds no credentials you don't want agents to use (cloud keys, SSH keys to other systems, password managers, signed-in browsers);
- give agents only repositories that GitHub branch protection backs up (see [Main is protected by the server](#main-is-protected-by-the-server));
- watch what it does, and keep the stop command at hand.

## What the code checks

These checks run inside `ygg` itself, for every command.

- **Identity.** Every session the conductor starts has `YGG_NODE` and `YGG_ACTIVATION` set. With `YGG_NODE` set, its `--from` and `--as` must be itself or one of its registered children, or the command is refused.
- **Commands reserved for you.** These are refused (exit 3) in an agent session: when `YGG_NODE` or `YGG_ACTIVATION` is set, or the command runs inside an agent's worktree (a folder under a `.ygg-managed` marker). Two use a narrower test: `node add --role su` and `backlog order --theme` check only `YGG_NODE`. `node release` and `node reset` also refuse when `YGG_NODE` is `l1`, the main session's own name. The commands are:
  - approvals and decisions: `decide`, `pid approve`, `pid reject`, `pid work-ahead`, `stage approve`, `stage reject`, `stage end` in your form (without `--within-tolerance` and `--report`; a project manager's form with both is allowed), `exception decide`, `morning-go decide` and `ruling record`;
  - standing permissions: `envelope grant`, `envelope revoke`, `relay grant`, `relay revoke`, and every `agents` command (`allow-standard`, `allow-minimal`, `revoke`, `revoke-minimal`);
  - setting things up: `init`, `programme create`, `project create`, `project shape`, `project close`, `node add --role reviewer`, `node add --role su` (agents may add other nodes only beneath themselves), and `backlog order --theme`;
  - repairs and settling: `node release`, `node reset`, `review retry`, `review drop`, `wp cancel --finish` and `t3 settle`;
  - main and safety: `merge main`, `stop`, `resume` and `attach --takeover`;
  - other: `dashboard serve`, and `send` without `--from` (your short form for messaging a node).
- **Commands that start or drive sessions.** `launch`, `_supervise`, `conductor`, `heimdall`, `rehydrate`, `event`, `attach` and `detach` are refused in the same way, so an agent can't start other agents, run the conductor or feed it events.
- **Judgements are recorded by their author.** Criteria, review, acceptance and rejection records are refused when a session writes them in another node's name, including its own child's. Each records the caller that actually ran the command.
- **Reads are scoped.** `ygg status` and `ygg pending`, run from an agent session, show only that session's programme.
- **Message rules.** Who may message whom, and the ban on messages across domains, are checked on every `ygg send`.
- **Whose approvals count.** When state is rebuilt from the tracker, an approval counts only if its `actor` is in `authority.actors` (by default `user`). Approval-shaped records from other actors are ignored and listed under `ignored_authority` in `ygg status --json`. The `actor` field is a plain value in a file, not a signature: see [Known gaps](#known-gaps).
- **Agent environment.** A headless agent's environment is built from an allowlist: system basics, locale, proxy and certificate settings, `SSH_AUTH_SOCK`, Yggdrasil's own variables except its test controls, and the credentials of the AI tool being started. Other credentials in your environment (for example `GH_TOKEN` or cloud keys) are not passed on. `SSH_AUTH_SOCK` is passed so git over SSH works, and it is the address of your running SSH agent: an agent can use every key loaded in it, to push or pull over SSH or to log in to any host those keys open, without seeing the keys themselves. If your SSH agent holds keys you don't want agents to use, run the conductor without `SSH_AUTH_SOCK` set (that stops passing your SSH agent; SSH key files and any separately configured agent socket your OS user can read stay reachable), or from an OS account whose SSH keys and SSH agent hold only what the projects need. See [Known gaps](#known-gaps). A profile's `launch_env` may not set any `YGG_` variable. A T3 session is different: it runs in the environment of the T3 server that starts it, and Yggdrasil only adds its identity and folder variables, so any credential in the T3 server's environment reaches the session unfiltered. Either way this filters the environment only; files and keychain items your OS user can read stay readable.
- **Test overrides.** The test-suite controls `YGG_TEST_MODE` and `YGG_NOW` are never passed to agents.

## The guard hook

Every Claude Code session the conductor starts gets a settings file that registers Yggdrasil's guard hook for every tool call. The hook command is the absolute path of the `ygg` that was on the launching `PATH`, so a session can't swap in another `ygg` later, and the settings set `disableAllHooks` to `false`. If no `ygg` is on the launching `PATH`, or the hook can't be written, the launch is refused (exit code 3) and nothing starts.

Before each tool call the hook:

- denies T3's messaging tools and the `t3` command;
- denies shell commands that would push to anything other than `ygg/...` branches, force-push, delete branches or recursively delete files, rewrite history (`filter-branch`, `filter-repo`, `reset --hard`), or send data out (`curl` and similar with an upload or request flag, `scp`, `rsync`, `nc`, `sendmail` and similar);
- denies ways to skip the push check: `git push --no-verify`, overrides of `core.hooksPath`, `core.fsmonitor`, git aliases, include files or the worktree-config extension (through `-c`, `--config-env`, `GIT_CONFIG_*` variables or `git config` writes and section edits);
- denies direct ref writes: `git update-ref`, `git symbolic-ref` writes and `git replace`;
- treats as unresolved any git subcommand that isn't a git built-in, so aliases and `git-<name>` programs are refused whatever they would run, along with `--exec-path` and changes to `PATH` or `GIT_EXEC_PATH` in a command that runs git (`git filter-branch` and `git filter-repo` are refused as history rewrites);
- denies setting or unsetting `YGG_` variables, and shell constructs it can't resolve (`eval`, `source`, `xargs`, shells fed from stdin or a file, and similar), including text it won't try to read the way a shell would: carriage returns, line continuations, unusual whitespace, a `#` inside a word, and a comment in a command that also holds quotes or backslashes;
- lets senior users run only read-only git commands and no file edits;
- refuses every tool call while the session's programme, or everything, is paused or stopped, so running work halts at its next tool call;
- refuses every tool call when the session's node is unknown to the tracker, the tracker can't be read, or the hook's input is malformed;
- records a heartbeat, so the conductor can tell a working session from a dead one.

The hook inspects only `Bash` commands and T3 tools, by pattern. It does not inspect file edits, web fetches or other MCP tools, and it can't see what an interpreter or script does. Codex sessions have no hook at all. See [Known gaps](#known-gaps).

## What agents may do without asking

A Claude Code agent runs non-interactively, so a tool call that needs an approval is simply denied: nobody is there to give it. With its own settings folder (`agent_claude_config_dir`), an agent has nothing else that allows it anything, so it couldn't even read its tracker or run `ygg`. Yggdrasil therefore writes permission rules into each launch's settings file, but only after you agree, once, to one of two rule sets:

- **Standard** (the one most people want): run `ygg agents allow-standard`. It prints the exact rules for each role and records nothing. If you agree, run `ygg agents allow-standard --yes`.
- **Strict minimum:** run `ygg agents allow-minimal`. It records your agreement straight away.

Until you agree, nothing is granted, and a launch whose agent has its own settings folder is refused (exit 3) with both commands named. Owed work for such an agent shows in `ygg status` as `blocked`, with `agent-permissions` in `blocked_by` and the reason (and the command to run) in `held_reason`; it clears by itself once the agreement is valid. A launch that uses your own Claude Code setup is unchanged.

The commands are yours alone: they are refused (exit 3, `user-only`) in any agent session, that is with `YGG_NODE` or `YGG_ACTIVATION` set, and inside an agent's worktree, staging folder or scratch folder. Your agreement is recorded in `_ygg/agents-agreement.json` in the files tracker folder (`files_root`), whichever tracker you use, with mode 0600. It holds the rule set you agreed to, its version and the programmes it covers. Agreement is per programme: agreeing covers every programme that exists at that moment. A launch in a programme created later is refused until you extend the agreement with `ygg agents allow-standard --yes --programme <name>` (or `ygg agents allow-minimal --programme <name>`); the refusal names that command. An agreement file that is a symbolic link, or whose real location is inside an agent's worktree, staging folder or the scratch root, never counts, and the commands never write through a link. The agreement is read afresh at every launch, so an agreement or a revocation reaches a conductor that is already running at its next launch. No other place counts: `agent_permissions` in a config file or profile is an error, and in a programme, project plan or work item it is ignored. `ygg agents revoke` withdraws the agreement for every programme; sessions already running keep the settings they started with.

**Versions.** Each rule set has a version, raised whenever its rules allow more. When a newer Yggdrasil's rules are wider than the version you agreed to, launches are refused (exit 3) until you run the command again, see the new list and agree.

The minimal rules, for each launch:

- `defaultMode: dontAsk`: anything no rule allows is denied straight away.
- `Bash(ygg *)`: the agent's own `ygg` commands. `ygg` refuses the commands reserved for you. A rule never matches a compound command such as `ygg inbox && other`.
- `Read` of its project's folder in the tracker (its rehydration document, the brief and the records). A programme reviewer gets the folders of its programme's projects. Reads inside the agent's own working folder need no rule.
- For project managers, team leads and workers: `Edit` (which covers writing new files) inside its own worktree. For team leads and workers in a Git worktree, also `git add` and `git commit`; project managers get no git write rule.
- For reviewers: `Edit` of their own scratch folder only, where they write their verdict, and `git diff` and `git show` with arguments. Senior users edit nothing.

The standard rules add:

- For every role in a Git worktree, and for reviewers anywhere, read-only git, in a few exact shapes only: `git status [--short|--porcelain]`, `git diff [--cached|--stat|--name-only] [<rev>]`, `git log [--oneline] [-n N] [<rev>]`, `git show [--stat] <rev>`, `git rev-parse <rev>`, `git rev-parse --abbrev-ref HEAD` or `git rev-parse --show-toplevel` and `git branch --list [pattern]`. The rules allow `git status`, `git diff`, `log`, `show`, `rev-parse` and `branch --list`, with or without arguments; the guard hook then holds every git command that reads to those shapes. Each must be one simple command run from inside the agent's worktree, where git selects the lane's own repository as the launch recorded it: the same git directory and common git directory, neither inside the lane itself (a nested or rewritten `.git`, or redirected lane metadata such as `commondir` or `HEAD`, is refused). Editing git's metadata with Claude Code's file tools is refused too: any `.git` component in any case, and any path that reaches (relative to the agent's folder, through symbolic links or a differently cased alias, compared by file identity) a `.git` folder or the lane's recorded git directories, with no path, no other option, no `cd`, `pushd`, `env`, `export` or variable assignment, and no pipe, redirection, substitution or second command; a revision is one plain word that names no file. Anything else that reads with git (`git grep`, `blame`, `ls-files`, `cat-file`, `log -p`, `show <rev>:<path>` and so on) is refused with the list of shapes. `--output` and `--no-index` are denied in any position and abbreviation, and setting `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` or another variable that points git elsewhere is denied in any command.
- For team leads, workers and senior users, the project's named test and build commands, with any arguments. The project manager lists them in the project plan's header, `agent_commands: [npm test, npm run build]`, and they count only as you approved that header with `ygg pid approve`: never from a config file, profile, programme record, a later edit of the plan or a work item. Each must be one plain command, with no shell operators, quotes, `$`, backticks, `*` or parentheses. A test command runs the project's code, so it can do whatever that code does.
- For team leads and workers in a Git worktree, `git mv`, `git rm` and `git restore`.
- For team leads in a Git worktree, exactly `git push origin ygg/<project>/lane/<team lead>`: its own lane branch, nothing else, and no other form of the command (not even with `-u`). The push check below still applies.

Paths are absolute and resolved through symlinks. A path that would read as a pattern (it holds `*`, `?`, `[`, `]`, `\`, `!`, parentheses or a line break) refuses the launch. The rules were checked against Claude Code 2.1.322 with a clean settings folder and an untrusted repository. With the minimal rules, the agent ran `ygg`, read its tracker and wrote in its worktree, and was denied writes, shell commands and reads elsewhere. With the standard rules, it ran read-only git, a named command and its own lane push, and was denied `git diff --output`, another lane's push and `git checkout`.

**Your project's own Claude Code settings.** By default an agent reads its project's instructions (`CLAUDE.md`) but not the project's settings (`.claude/settings.json` and `.claude/settings.local.json`, with their hooks and permission rules). Yggdrasil supplies the guard hook, the agreed rules and its own lane commit check instead. It starts Claude Code with `--setting-sources user`, adds the working folder back as a `CLAUDE.md` folder (`--add-dir` with `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`), and keeps its own `--settings`. If you want agents to load the project's settings too, set `agent_project_settings: include` in your own config or profile; no other layer counts.

**Lane commits.** Every agent commit in a lane worktree passes Yggdrasil's own lane check (`ygg lane-check`, run by the lane hooks; see [Launchers](launchers.md#headless)):
- the staged content, the staged file names and the commit message must not match a term in the profile's `leak_denylist` or a common secret shape. The lane check reads only the profile's list, never `YGG_LEAK_DENYLIST` or git config;
- no CI workflow may be touched (that is a hard stop) unless the project's approved plan allows workflow changes, no git hook file unless it allows hook changes, no file of an already accepted work package, and no credential file: `.env`, keys (`*.pem`, `*.key`, `*.p12`, `*.jks`, `id_rsa` and the like), the standard stores (`.aws/credentials`, `.git-credentials`, `.netrc`, `.pypirc`, `.kube/config`, the gcloud, gh and Terraform credential files), and `.npmrc`, `.yarnrc` or `.docker/config.json` when they carry authentication (npm and yarn keys in any quoting, with `=`, `:` or a space, registry-scoped or not; a Docker config is decoded with every key seen, duplicates included, keys compared with Unicode case folding as Docker does and any non-ASCII key counted, and is checked as raw text too; one that doesn't decode is refused). A path shaped like a secret is refused too, deleted or not, and is never repeated in the message;
- when `commit_identity` says `trailers: none`, the message may hold no `Co-Authored-By` trailer;
- no commit at all while a work package of the lane is being checked, and the lane branch may not move to a commit that drops its newest accepted commit (an amend, reset or rebase).

The home repository's own git hooks do not run on lane commits. Its pre-commit runs once, in the merge check before the merge into main (see [CLI reference](cli-reference.md#git)). The guard hook refuses `git commit --no-verify`, `-n` and any short-flag cluster holding `n`, and the push check runs the same lane check on every pushed commit, so a commit made with its hooks skipped (by a script, say) still can't reach the remote.

**T3 sessions.** The agreement is checked the same way before a launch, whichever route it takes, and a T3 session gets the same agreed rules in the `.claude/settings.json` T3 loads from its worktree, beside the guard hook, plus one rule to read its own private prompt file (`state/launch/<activation>/prompt.md`), which T3 points it at. Without a valid agreement a T3 launch is refused, and its owed work shows the permissions hold in `ygg status`.

**Codex.** The equivalent for Codex is its sandbox, not a rule list: `codex exec` never asks for approval, and Yggdrasil already chooses the sandbox. In v1.0 only project reviewers run on Codex. A Codex reviewer gets `workspace-write` rooted in an empty scratch folder, where it writes its verdict, and no write access to the state or tracker folders, so its checkout stays read-only. Yggdrasil sets no network option: whether the sandbox reaches the network, and any extra writable folders, follow your own Codex configuration (`sandbox_workspace_write` in its config file), so check those settings. Reads and commands are not narrowed: a Codex agent may read any file and run any command its OS user may, inside that sandbox. No agreement is asked for Codex sessions in this build, because neither rule set changes what they may do, and Codex has no project-settings switch here.

## Hard stops

These actions are meant to wait for you, inside or outside an envelope: pushing anywhere outside the project's `ygg/...` branches, force-pushing, deleting branches or data, rewriting history, sending anything outside, changing CI workflows, and spending money. The guard hook and the push check refuse the shell forms listed above. A decision whose `--action` names a hard stop can never become provisional. Spending can never be granted, even as a standing permission. Nothing detects spending by itself.

## The push check

Every agent worktree has git's hooks folder pointed at Yggdrasil's pre-push script, which runs `ygg push-guard` with git's remote name and URL. For each pushed ref it:

- allows only fast-forward pushes of `ygg/<project>/lane/<node>` or `ygg/<project>/_integration` branches, for a project whose approved plan names that repository;
- requires the destination URL to be one the repository had when you approved the plan (git's own URL rewrites applied, local paths compared as absolute paths). A check without a URL when a remote was recorded, a relative URL, or any URL when the repository had no remote at approval, is refused;
- binds the branch to the caller: a team lead may push only its own lane, a worker its team lead's lane, a project manager only the integration branch; senior users and reviewers may not push;
- refuses deletions, tags, non-fast-forward pushes and branches of other projects;
- refuses the push when any commit being sent touches `.github/workflows/`, even if a later commit undoes it, unless the plan allows workflow changes;
- ignores `GIT_DIR`-style variables and git replace refs, so it reads the real repository and commits.

The pre-push script runs only if git runs hooks. `--no-verify` and hooks-path overrides are refused by the guard hook, but an agent that reaches git another way (an interpreter, or a Codex session) can skip it.

## Git settings in your repositories

Git config can name programs for git to run: merge drivers, filters, diff tools, signing programs, editors, pagers, SSH commands, credential helpers and transports. Yggdrasil's own git calls (creating worktrees, merging lanes, reading history) never run any of them.

- Every git call Yggdrasil makes itself forces the program settings off (hooks, fsmonitor, signing, editors, pager, SSH command, askpass, credential helper) and allows no network transport. It ignores your system git config and your own global config, apart from your name and email, and drops `GIT_DIR`-style, `GIT_CONFIG*`, `GIT_EXEC_PATH` and program variables from its environment.
- Before it creates, reuses or checks out a worktree, or merges in a repository, Yggdrasil reads every config value its own git would load there, in the repository and worktree scopes, with includes followed. Its own git calls never load your global or system git config: they skip the system file and replace your global file with a minimal one of Yggdrasil's, so helpers configured there never run in them. An agent's own git does use your real global and system config, and this check does not read them; see [Known gaps](#known-gaps). If any key is not on the list below, the launch or merge is refused (exit code 3) and the message names the repository and the key. Includes are refused too. A config git can't read counts as refused, and git older than 2.26 is refused because it can't report scopes.
- New worktrees are created without a checkout, checked against the list, then filled in. A worktree whose creation was interrupted is filled the same way, and refused rather than overwriting a file someone put there.

The keys a repository may set:

| Area | Keys |
|---|---|
| Identity | `user.name`, `user.email` |
| Repository format | `core.bare`, `core.repositoryformatversion`, `core.filemode`, `core.ignorecase`, `core.precomposeunicode`, `core.logallrefupdates`, `extensions.worktreeconfig`, `lfs.repositoryformatversion` |
| Line endings and whitespace | `core.autocrlf`, `core.eol`, `core.safecrlf`, `core.whitespace` |
| Remotes and branches | `remote.<name>.url`, `remote.<name>.pushurl`, `remote.<name>.fetch`, `branch.<name>.remote`, `branch.<name>.merge`, `fetch.prune`, `push.default` |
| Pull and merge behaviour | `pull.rebase`, `pull.ff`, `merge.ff`, `init.defaultbranch` |
| Display | `color.*` |
| Submodules | `submodule.<name>.url`, `submodule.<name>.path`, `submodule.<name>.branch` (never `submodule.<name>.update`) |
| Aliases | `alias.*`, unless the value starts with `!` (a shell command) |
| Git LFS | `filter.lfs.clean`, `filter.lfs.smudge`, `filter.lfs.process` and `filter.lfs.required`, with the standard Git LFS values only |
| Overridden | `core.hookspath` and `core.fsmonitor` (Yggdrasil's calls force both off) |
| Yggdrasil | `ygg.*` |

Anything else, such as a merge driver, a filter, `diff.external`, `core.sshCommand`, `gpg.program` or `include.path`, refuses the operation. Remove the setting or move the repository's work elsewhere. This covers Yggdrasil's own git calls only. An agent running git in its worktree uses git as you have it configured, except that the guard hook refuses aliases (see [The guard hook](#the-guard-hook)).

## Main is protected by the server

Yggdrasil keeps agents off your main branch by convention, and GitHub enforces it:

- Only you merge into main. `ygg merge main` is reserved for you, so no agent session can run it, and it merges into your local main only after the merge check passed for the current main and integration heads. With `--print` it changes nothing and gives you the git command or the pull request to make. `merge_mode` doesn't change this in this release. Pushing main is a hard stop.
- `ygg pid approve` checks branch protection on `main` for every repository the plan names, using `gh` with your login. By default (`branch_protection: refuse`) it refuses approval unless the policy binds administrators, disables force pushes and deletions, and requires at least one approving review. Either kind of GitHub protection counts: classic branch protection, or active repository rulesets that nobody may bypass and that together block force pushes and deletion and require an approving review. `warn` approves and raises an alert; `off` still asks GitHub but ignores a missing or unsafe policy. The check runs once, at approval.
- Local commits and merges an agent makes in your home checkout with plain git are not blocked by the guard hook. Server-side protection on GitHub is what keeps them out of your remote main.

CI on `ygg/*` branches should run with read-only permissions and no secrets, with any job that needs secrets behind a GitHub environment that requires a reviewer.

## Secrets

- Strings that start like known tokens are replaced with `[REDACTED]` in messages and in most tracker records Yggdrasil writes. The prefixes are: Linear `lin_api_` and `lin_oauth_`; Anthropic `sk-ant-`; OpenAI `sk-proj-`, `sk-svcacct-` and `sk-admin-`; GitHub `ghp_`, `gho_`, `ghs_`, `ghu_`, `ghr_` and `github_pat_`; AWS access key ids (`AKIA` or `ASIA` followed by 16 capital letters or digits); and Slack `xoxa-`, `xoxb-`, `xoxp-`, `xoxo-`, `xoxs-` and `xoxr-`. Other token formats, and secrets with no recognisable prefix, are not caught. Treat this as a safety net, not a guarantee.
- `ygg profile show` hides values under keys that look secret (secret, token, password, credential, API key).
- Agents can read any file your OS user can read, including credential files and keychain items your user can open without a prompt.

## Stopping things

Stops and pauses are recorded on your machine before anything else, in Yggdrasil's safety folder. By default that folder sits next to the state folder (`~/.local/state/yggdrasil.safety`). It is kept apart from the state folder on purpose: wiping or losing the state folder doesn't lift a stop or a pause.

- `ygg pause` pauses everything and `ygg pause <programme>` pauses one programme. Pause writes a marker in the safety folder, then records the pause in the tracker. If the tracker can't be written, pause exits 8, but the pause already holds. While paused, nothing launches, and Claude Code sessions halt at their next tool call. Any session may pause; only you may resume.
- `ygg stop [<programme>|<node>]` is the emergency stop. It works in this order:
  1. It writes a stop marker in the safety folder, so nothing new starts in the scope.
  2. It finds the running headless agents in scope from the local launch records and a check of your processes. It doesn't need the tracker for this.
  3. It signals each headless agent's process group and kills it after a short grace period (`stop_grace_seconds`, default 10). T3 sessions are not signalled: the stop marker makes the guard hook deny a stopped T3 session's next tool call, and nothing more is sent to its thread. A reply the session is already writing can finish.
  4. It records the stop in the tracker, waiting at most 5 seconds for it. If the write fails it exits 8 with `stopped <n> session(s); tracker write failed`; if the tracker stays busy it exits 8 with `... tracker write pending`.

  Stop only signals a process it can identify as that agent (its recorded start time, or the agent's own environment). A live process it can't identify is listed in the message and left alone, and stop exits 7. A process an agent detached from its own process group can survive. Nothing in the scope starts again until you resume.
- A session stopped by `ygg stop` gives back the work it was given, so after `ygg resume` that work is offered again. A session that exited normally once a stop had begun signalling it (Claude Code exits cleanly on the signal) may have finished or been cut short, so it is held until you settle it. Only a signal the system could not send at all (the process was already gone) leaves a normal exit counted as done. A held session raises one `ygg-stop-held` alert, shows its node as blocked by `uncertain-activation` with the activation in `uncertain_keys`, and keeps the node from launching. `ygg launch` is refused too (exit 3) while the held session was given any work, because the agent would see that work again; it goes ahead only when the held session was given nothing. Settle it with `ygg node release <node> --as aborted` to offer its work again, or `--as failed` to treat it as done.
- `ygg resume [<programme>]` lifts exactly the stops and pauses that exist when it runs: it records their ids in the tracker first, then removes their markers. A stop or pause made after that, even one made while the tracker was down, stays in force.
- If something was interrupted, the next `ygg conductor tick` or `ygg rehydrate` finishes it: it signals any agent a stop didn't reach, records stops and pauses that couldn't be written to the tracker when they were made, and removes the markers of a resume that was recorded but didn't finish. A stop or pause recorded in the tracker but missing its marker still holds until you resume.
- `CLAUDE_PLATFORM_ENABLED=0`, a `pause` file in the folder named by `YGG_PLATFORM_STATE`, or a `pause` file in the folder named by the profile key `platform_pause_dir` stops all launches.

## Known gaps

These are accepted for this version and stated plainly. Each one can be used by a confused agent by accident, or by any agent that follows planted instructions.

- **Same OS user.** Agents run as you. They can read your files, credentials and keychain items your user can open, and edit Yggdrasil's own state and tracker folders directly. Editing those files can forge approvals, identities or node registrations, because nothing signs them.
- **Your global and system git config reach agents' own git.** The helper check reads only the repository and worktree scopes. An agent's git loads your `~/.gitconfig` and the system config, so a credential helper, filter or alias defined there runs when the agent's git uses it. Keep those files free of helpers you don't want agents to run, or run the conductor from an OS account whose git config holds only what the projects need.
- **Your SSH agent reaches headless agents.** `SSH_AUTH_SOCK` is on the agent environment's allowlist, so a headless agent can ask your running SSH agent to sign with any key loaded in it: to push or fetch over SSH, or to log in to another host. The agreed permission rules keep a Claude Code agent from running `ssh` itself, and the push check covers `git push` through git's hooks, but the project's test and build commands, and any script they run, can use the keys freely, as can a T3 session whose server has `SSH_AUTH_SOCK` set. The guard hook doesn't refuse `ssh`; it refuses `scp`, `sftp` and `rsync` as outbound transfers. Keep keys you don't want agents to use out of the SSH agent the conductor sees, or unset `SSH_AUTH_SOCK` before you start the conductor.
- **Dropping the markers.** A session that unsets both `YGG_NODE` and `YGG_ACTIVATION` is treated as you. The guard hook refuses `unset` and `env -u` of `YGG_` variables, but an interpreter or a script can start a process without them.
- **Interpreters and scripts.** The guard hook reads shell command lines. `python -c`, `node -e`, a script file or any other program can push, delete, send data or call `ygg` without matching a pattern.
- **Direct file writes.** The guard hook doesn't check file edits, except that senior users may not edit. With agent permissions agreed, Claude Code itself denies Edit and Write outside the agent's worktree; without them, an agent using your own Claude Code setup can edit any file that setup allows. A script or interpreter can write any file its OS user can write, including git configuration and Yggdrasil's state.
- **Your repository's Claude Code settings can reach agents.** An agent's worktree holds your repository's `.claude/settings.json` and `CLAUDE.md`. Every agent reads `CLAUDE.md`. A headless agent loads the settings file, and so runs its hooks, only when you set `agent_project_settings: include`; Claude Code then ignores that file's allow rules in a workspace you haven't trusted, but still runs its hooks. A T3 launch is refused when the repository tracks its own `.claude/settings.json`, and T3 sessions have their own settings gap (below).
- **Tools other than Bash.** Web fetches and MCP servers configured in your own AI tool (mail, chat, GitHub and others) are not inspected. Outbound checks cover selected shell command patterns only; for example `gh api` writes and `gh pr create` are not in the list.
- **No spend cap.** Nothing limits model spending or tokens. The number of sessions running at once is unlimited unless you set `max_concurrent_sessions`. The other controls are host load, the per-tick launch pacing and the per-node retry limit. Every wake starts a paid session on your login or key.
- **Your main session holds your authority.** The main session is an AI session that runs the reserved commands for you. It acts on your answers by its instructions, not by a check.
- **Approval records are unsigned.** `actor: user` is a field any writer of a tracker file can set. The allowlist only constrains records written through `ygg`.
- **Provisional decisions rest on the agent's word.** Inside an envelope, whether a decision is reversible and fits the North Star are flags the agent sets when it raises the decision, and there is no North Star to check against in this build. A decision about pushing, deleting or spending that leaves out `--action` is not recognised as a hard stop.
- **Tracker credentials are readable by any of your processes.** The Linear token file and the macOS Keychain item can be read by anything running as your OS user, an agent included. The guard hook refuses the obvious paths (the token file, its folder, the `security` command, the keychain service's name), but a script can still reach them. A credential broker is planned for v1.1.
- **The managed-worktree marker depends on the working folder.** A command counts as a managed session inside a folder holding `.ygg-managed`, so a session that drops its identity variables there is refused user-only verbs and its tool calls are denied. A process started in another folder is not covered.
- **`.claude` and `.ygg-managed` are agent-writable.** The guard hook refuses edits to them through file tools and plain shell words, but a script can still change them. Tampering with a T3 worktree's settings is detected before the next message, not prevented.
- **T3 sessions can edit their own guard settings.** T3 takes the guard hook from the worktree's `.claude/settings.json`, a file the session itself can write; T3 has no settings path outside the worktree. Yggdrasil writes the file with `disableAllHooks: false` and, before every message it sends to a T3 thread, checks that the file is unchanged and that the worktree's `.claude` folder holds nothing else (in any letter case), so no `settings.local.json` or other override can apply. A repository that ships its own `.claude` files cannot run T3 sessions. A changed worktree marks the node stopped and raises `ygg-t3-settings-tampered`. Between two checks, a session that rewrites the file can run tools without the hook.
- **Codex sessions have no guard hook.** In v1.0 these are project reviewers only. They run in the Codex sandbox (`workspace-write`), rooted in a scratch folder. Pause applies to Codex at launch only.
- **Workers started through Claude Code's Agent tool share their team lead's session.** Their identity rests on the prompt.
- **A hook that doesn't run doesn't block.** If Claude Code can't start the hook process or the hook times out, Claude Code may run the tool anyway. A hook that runs and fails internally denies the call.
- **Your repository's own git hooks don't run in agent worktrees.** Yggdrasil points each agent worktree's hooks folder at its own lane hooks, so your pre-commit checks don't run on agent commits. They run once, in the merge check, before the merge into main.
- **Relays across domains are detected by reference only.** A message that passes `--relay-of` is checked. A message that quotes another domain's text without it is not detected, and the main session restating material from one domain to another is held back by its instructions only.
- **Configuration can run code.** Selecting a tracker, launcher or alert-forwarder plugin, and `plugin_paths`, make `ygg` import Python code. In your own shell, `ygg` reads `./yggdrasil.yaml` from the folder you run it in, so run it only in folders you trust, or set `YGG_CONFIG`.
- **The main session's lock is cooperative.** Only `ygg attach` and `ygg detach` read it; the reserved commands don't, so two sessions running as you could both act. The skill renews the lock before each reserved command.
- **Agents can't run these git extensions.** `git submodule`, `git lfs` and `git mergetool` are not git built-ins, so the guard hook refuses them along with aliases and other `git-<name>` programs. Run them yourself where a repository needs them.
- **Branch protection is checked once.** It is read at plan approval through `gh`; a later change on GitHub is not noticed.
