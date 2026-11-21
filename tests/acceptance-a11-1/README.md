# A11.1 acceptance suite

This suite covers the follow-up to A11: history dropped from a lane, cancelling work packages (its guards, and finishing an interrupted cancel), package states and retries, the main-session skill's instructions, alerts, and merging into main. Every test runs twice, with the phase-end feature (`stage_close`) on and off.

The acceptance suites are frozen acceptance tests for the signed criteria. Each test carries an `ac` marker naming its criterion and a `release` marker. `ac-table.md` lists the criteria and their tests. Don't edit a test to make a change pass; raise an issue instead (see [Contributing](../../docs/contributing.md)).

The tests drive `ygg` only through its command line, files, processes and the recording fakes of the earlier suites, and import no application code. `driver_a11_1.py` extends the A11 driver, and `stub_a11_1.py` returns empty results for the stub run.

Run it from the top folder of the repository:

```bash
ACCEPTANCE_TARGET=real YGG_BIN="$(command -v ygg)" YGG_CORE_ROOT="$PWD" pytest tests/acceptance-a11-1 -q
```

`ACCEPTANCE_TARGET=stub` runs the tests against empty results instead, to show that they don't pass on nothing. `check_criterion_tags.py` checks the tests' markers against the written specification, which is not published in this repository. `spec-gaps.md` lists criteria the suite could not test as written.
