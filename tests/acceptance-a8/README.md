# A8 acceptance suite

This suite covers crash safety around launches and stops: process identity and deadlines, the safety interlocks (stops and pauses made offline, and a full wipe of the state folder), supervisor crashes and write faults, launcher races, and a jump of the clock.

The acceptance suites are frozen acceptance tests for the signed criteria. Each test carries an `ac` marker naming the criterion it covers, and `ac-table.md` lists the criteria and their tests. Don't edit a test to make a change pass; raise an issue instead (see [Contributing](../../docs/contributing.md)).

The tests drive `ygg` only through its command line, files and processes, and import no application code. They reuse the drivers of the core suite (`tests/acceptance`) and the A7 suite (`tests/acceptance-a7`). New calls live in `driver_a8.py`, and `stub_a8.py` returns empty results for the stub run.

Run it from the top folder of the repository:

```bash
ACCEPTANCE_TARGET=real YGG_BIN="$(command -v ygg)" pytest tests/acceptance-a8 -q
```

`ACCEPTANCE_TARGET=stub` runs the tests against empty results instead, to show that they don't pass on nothing. `checks_a8.py` holds helper checks the tests share. `spec-gaps.md` lists criteria the suite could not test as written; it is empty.
