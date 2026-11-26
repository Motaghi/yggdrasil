# Contributing

Thank you for helping. This page covers setting up a development copy, running the tests, the leak scan, and writing plugins. To report a security problem, follow [SECURITY.md](../SECURITY.md) instead of opening an issue.

## Licence

Yggdrasil is released under the MIT licence (see `LICENSE`). Contributions are accepted under the same licence.

## Development setup

You need macOS or Linux, Python 3.12 or later, `uv` and git. From a clone of the repo:

```bash
uv venv
source .venv/bin/activate
uv pip install -e '.[test]'
git config core.hooksPath scripts/hooks
```

This installs the `ygg` command from your working copy, plus `pytest` and `jsonschema` for the tests, and turns on the leak-scan hook (see [Keeping secrets and personal data out](#keeping-secrets-and-personal-data-out)). Keep the virtual environment active: launches refuse to start when `ygg` is not on `PATH`, and some unit tests launch agents.

## Running the tests

There are two suites.

**Unit tests** live in `tests/unit/` and import the package directly:

```bash
pytest tests/unit
```

**The acceptance suites** live in `tests/acceptance/`, `tests/acceptance-a*/`, `tests/acceptance-v1-1/` and `tests/acceptance-v1-1-codex/` (the Codex builder suite). They were written independently from the signed specification and are frozen: the tests drive `ygg` only through its command line, public tracker files and recording fakes for `t3`, Claude Code, Codex and `gh`, and they import no application code. Every test runs in its own temporary folders with a clean environment, no inherited credentials and no profile.

```bash
ACCEPTANCE_TARGET=real YGG_BIN="$(command -v ygg)" YGG_CORE_ROOT="$PWD" \
  pytest tests/acceptance/core -q
```

Run the other suites the same way, one folder at a time, for example `pytest tests/acceptance-a11 -q` or `pytest tests/acceptance-v1-1-codex -q`.

- `YGG_BIN` names the `ygg` executable under test.
- `YGG_CORE_ROOT` points at your checkout; the public-repo checks (the README Quickstart, the demo programme, the leak scan) read it.
- `YGG_V100_BIN` names an installed v1.0.0 `ygg`, for the five upgrade rows of `tests/acceptance-v1-1-codex` (they install state with v1.0.0 and continue with the build under test). Install v1.0.0 into a tool folder of its own, so it doesn't replace your development `ygg`, for example `UV_TOOL_DIR="$HOME/ygg-v100/tools" UV_TOOL_BIN_DIR="$HOME/ygg-v100/bin" uv tool install git+https://github.com/Motaghi/yggdrasil@v1.0.0`, then set `YGG_V100_BIN="$HOME/ygg-v100/bin/ygg"`. Without it those rows are skipped.
- `YGG_LEAK_DENYLIST` names a denylist file for the leak-scan checks. Two checks need it: `test_public_checkout_and_diffs_have_zero_leak_matches[tree]` and `[history]` (AC-36, in `tests/acceptance/core`). Without it, both fail with "YGG_LEAK_DENYLIST must name the leak-scan fixture". The third row of that test, `[secret-history]`, needs no denylist.

Each test carries an `ac` marker naming the acceptance criteria it covers. A skipped row has not passed: count only rows pytest reports as passed, and set up what a skipped row needs before you rely on a run. The suites are the bar for a change: if a change needs a frozen test to change, raise it as an issue rather than editing the test.

Frozen does not mean a row can never change. When the behaviour a row checks is itself changed on purpose, the row is changed in a separate step, written by an independent test author and checked against an empty stub, never in the change that makes it pass. In v1.1.1 that happened to the Codex builder rows of `tests/acceptance-a11` and the rows it shares with `tests/acceptance-a10` (they now test the private-clone design), to the crash-recovery rows of `tests/acceptance-a7` (which now let a short-lived stand-in agent exit before they look a second time), and to the version rows of `tests/acceptance-v1-1` (which now expect 1.1.1).

<a id="rows-that-fail-by-design-in-v110"></a>

### Rows that fail by design in v1.1.1

In the published v1.1.1 tree, 2 rows of `tests/acceptance-a11` fail, and are expected to. Each carries the pytest marker `release('fallback-v1.1')`. The suite is frozen, so the marker stays, but the work those rows test comes later: sending urgent messages straight into a running T3 thread, planned for v1.2. The 8 rows that test Codex builders pass in v1.1.1, in the form changed for this release (see above). A red row in `tests/acceptance-a11` that is not in the table below, and not behind a switch that shipped off (see the next paragraph), is a real failure.

The published tree, main and the tag alike, carries the shipped copy of `src/yggdrasil/data/release.yaml`, so that file shows which switches shipped off. It is written at release time by the build-only `scripts/release-switches` from the acceptance results: a switch ships on only when every criterion it carries is green. A development tree may keep every switch on. The rows of a switch that ships off also fail by design, and the [release notes](releases/v1.1.1.md) list them. The second table names the rows of the two optional switches, `confirm_window` and `dashboard_serve`, which can ship off.

| Rows | Criterion | Why it fails |
|---|---|---|
| `test_lane3_launcher.py::test_ac36_t3_live_thread[1]`, `[2]` | AC-36 | The urgent T3 live send moved to v1.2; the test sees no message sent to the thread. |

If a switch ships off, these rows fail too:

| Switch | Rows | Criterion |
|---|---|---|
| `confirm_window` | `test_lane2_conductor.py::test_ac22_confirm_windows[1]`, `[2]`, `[3]`; `test_ac77_window_bounds[2]` | AC-22, AC-77 |
| `dashboard_serve` | `test_lane4_modules.py::test_ac09_row1_serves_file`, `test_ac09_row2_read_only[DELETE]`, `[POST]`, `[PUT]`, `test_ac09_row3_paths_refused` (4 rows: `[/x]`, `[/%2e%2e/state]`, `[/../alerts.jsonl]`, `[/dashboard/beta.html]`); `test_ac49_allowed_host[1]`, `[2]`; `test_ac50_loopback_user_only[1]`, `[2]` | AC-09, AC-49, AC-50 |

`test_ac50_loopback_user_only[1]` needs a machine with a non-loopback IPv4 address: its host name must resolve to that address, and a connection to that address on a closed port must be refused, not dropped. On a machine without such an address it fails with "AC-50 requires a host with a non-loopback IPv4 address". Run the check on such a machine before you rely on the row.

One optional row of the v1.1 suite (`tests/acceptance-v1-1`) is red because its item was cut from v1.1:

| Rows | Criterion | Why it fails |
|---|---|---|
| `test_resolved_criteria.py::test_ac20_changed_testing_details_send_one_urgent_notice_with_no_change_control` | AC-20 | The optional testing-race notice was cut from v1.1 and moves to v1.2. |

One more row can fail under heavy machine load: `test_lane2_conductor.py::test_ac40_row1_starting` (AC-40) checks a start within a short window. Run it alone to check it; it passes on an idle machine.

A few commands exist only for tests (`ygg test set-next`, `ygg test activation`). They are refused unless `YGG_TEST_MODE=1` is set and `YGG_NODE` is not.

The README's `## Quickstart` section is run by the suite as one bash block. Keep that heading followed by exactly one fenced `bash` block that works in an empty folder.

## Keeping secrets and personal data out

The repo is public. `scripts/leak-scan` is the leak scan, run by two hooks: `pre-commit` checks every staged file in full, the staged file names and the author and committer identity, and `commit-msg` checks the commit message. It blocks a common secret shape (Anthropic, OpenAI, GitHub, Linear, AWS and Slack token prefixes, private key headers, JSON Web Tokens, legacy `sk-` keys and JSON `refresh_token` values) or, if you configure one, any line of a private denylist (`YGG_LEAK_DENYLIST`, or `git config ygg.leakDenylist <file>`; blank lines and `#` lines are ignored). A configured denylist that can't be read blocks the commit. An identity you publish under on purpose is allowed with `git config ygg.publicIdentity "Name <email>"`. Git doesn't install hooks from a clone, so the hooks run only after `git config core.hooksPath scripts/hooks`. Before publishing, `scripts/leak-scan --history main` checks every commit's message, identities and content. It is a safety net, not a guarantee.

Please also:

- use only neutral names in tests and examples (programmes `alpha` and `beta`, nodes `pm1`, `pm1tl1`, `pm1su1`, paths under a temporary folder);
- never commit a real token; when a test needs a secret-shaped value, build it from parts at run time (for example `"sk-" + "ant-" + "x" * 24`) so no matching literal ever appears in a file or diff;
- keep anything specific to your own setup (paths, hostnames, proxy URLs, pool names) in a profile file outside the repo.

## Plugins

Trackers, launchers and alert forwarders are plugins, found through Python entry points. The built-in ones register the same way.

| Entry point group | Built-in names | Selected by |
|---|---|---|
| `yggdrasil.trackers` | `files`, `linear` | `tracker` |
| `yggdrasil.launchers` | `headless`, `t3` | `launcher` |
| `yggdrasil.alert_forwarders` | `auto` (the default), `jsonl` | `alert_forwarder` |

A plugin is a Python object with an `api_version` attribute. This build supports `api_version = 1`. Register it in your package's `pyproject.toml`:

```toml
[project.entry-points."yggdrasil.trackers"]
my-tracker = "my_package.tracker:Tracker"
```

Install the package next to Yggdrasil, or add its folder to `plugin_paths` (or `YGG_PLUGIN_PATH`); a folder with the module and its `<dist>-<version>.dist-info` (holding `METADATA` and `entry_points.txt`) is enough. Then check it is found:

```bash
ygg plugins list
ygg config validate
```

Validation refuses an unknown plugin name (listing the available ones) and an unsupported `api_version`. A plugin is code that runs with your user's rights whenever `ygg` loads it, so install only plugins you trust.

What this build does with plugins is limited:

- **Trackers.** A tracker plugin must keep its records in the files-tracker format and provide a `root()` method that returns their folder.
- **Launchers and alert forwarders.** Only `api_version` is checked. Sessions are always started by the built-in launchers. Alerts always go to `alerts.jsonl`, and they are delivered to you only with the built-in `auto` forwarder (see `alert_forwarder` in [Configuration](configuration.md#key-settings)); a plugin forwarder is never called.

## Where to start

The docs in `docs/` and the acceptance suites describe the intended behaviour. Before a larger change, open an issue describing it, so it can be checked against the threat model in [Security](security.md).
