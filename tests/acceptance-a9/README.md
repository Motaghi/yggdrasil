# A9 acceptance suite

This suite covers the guards: the guard hook's refusal of ways to skip the push check, the push check (approved remote, own branch per role, history sent), the branch protection check at plan approval, judgements recorded by their own author, commands reserved for the user, reads scoped to the session's programme, the agent environment allowlist, the guard hook's absolute `ygg` path and its refusals on bad input, and the emergency stop.

The acceptance suites are frozen acceptance tests for the signed criteria. Each test carries an `ac` marker naming the criterion it covers. Don't edit a test to make a change pass; raise an issue instead (see [Contributing](../../docs/contributing.md)).

The tests drive `ygg` only through its command line, files, processes and the recording fakes of the core suite, and import no application code. `driver_a9.py` extends the core driver, and `stub_a9.py` returns empty results for the stub run.

Run it from the top folder of the repository:

```bash
ACCEPTANCE_TARGET=real YGG_BIN="$(command -v ygg)" pytest tests/acceptance-a9 -q
```

`ACCEPTANCE_TARGET=stub` runs the tests against empty results instead, to show that they don't pass on nothing. `check_criterion_tags.py` checks the tests' `ac` markers against the written specification, which is not published in this repository. `spec-gaps.md` lists criteria the suite could not test as written; it is empty.
