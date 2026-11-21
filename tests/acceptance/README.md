# Core acceptance suite

This is the core acceptance suite for Yggdrasil. Its tests live in `core/` and cover the base behaviour: launching sessions and working in git, the session flow, state and decisions, the tracker and reporting, delivery approaches, `ygg init` and the presets, work-ahead, the Linear tracker, T3 and headless launches, the North Star and the dashboard page, events and envelopes, the guard hook and the clock, and messaging. It also checks the public repository itself: the README Quickstart, the demo programme and the leak scan.

The acceptance suites are frozen acceptance tests for the signed criteria. Each test carries an `ac` marker naming the criterion it covers, and `ac-table.md` lists the criteria and their tests. Don't edit a test to make a change pass; raise an issue instead (see [Contributing](../../docs/contributing.md)).

## How the tests work

The tests drive `ygg` only through its command line, the tracker files and recording fakes, and import no application code. `driver.py` owns every subprocess and file access. The fakes in `fakes/` stand in for Claude Code, Codex, T3, `gh` and the Linear API and record how they were called. Every test runs in its own temporary folders, with a clean environment built by the driver, no inherited credentials and no profile. `schemas/` holds the JSON schemas that `ygg status --json` and `ygg pending --json` output is checked against.

The other acceptance suites (`tests/acceptance-a*`) build on this suite's driver and fakes.

## Running it

Install the test extras first (see [Contributing](../../docs/contributing.md#development-setup)). From the top folder of the repository:

```bash
ACCEPTANCE_TARGET=real YGG_BIN="$(command -v ygg)" YGG_CORE_ROOT="$PWD" \
  YGG_LEAK_DENYLIST=/path/to/denylist pytest tests/acceptance/core -q
```

- `ACCEPTANCE_TARGET` is `real` (the default) or `stub`. `stub` runs the tests against `stub.py`, which returns only empty results, to show that the tests don't pass on nothing: almost every test fails there.
- `YGG_BIN` names the `ygg` executable under test (default `ygg` on `PATH`).
- `YGG_CORE_ROOT` points at the repository checkout, for the checks on the public repository.
- `YGG_LEAK_DENYLIST` names a denylist file for two of the leak-scan checks. Without it, those two fail with a message saying so.

`spec-gaps.md` lists criteria the suite could not test as written; for this suite it says `none`.
