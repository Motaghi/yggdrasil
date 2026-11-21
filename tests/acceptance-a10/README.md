# A10 acceptance suite

This suite covers the public-repository hardening: plugins that may load only from trusted config, the leak scan on staged files, commit messages and history, redaction of token-shaped strings in records, alerts and messages, private file modes for state and logs, refusal of corrupt node and project records, Yggdrasil's own git calls running no repository hooks, filters or merge drivers, and private delivery of each agent's prompt and brief.

The acceptance suites are frozen acceptance tests for the signed criteria. Each test carries an `ac` marker naming the criterion it covers. Don't edit a test to make a change pass; raise an issue instead (see [Contributing](../../docs/contributing.md)).

The tests drive `ygg` only through its command line, files, processes and the recording fakes of the core suite, and import no application code. `driver_a10.py` extends the A9 driver, and `stub_a10.py` returns empty results for the stub run.

Run it from the top folder of the repository:

```bash
ACCEPTANCE_TARGET=real YGG_BIN="$(command -v ygg)" YGG_CORE_ROOT="$PWD" pytest tests/acceptance-a10 -q
```

`YGG_CORE_ROOT` points at the repository checkout, which the leak-scan checks and the demo tracker check read. `ACCEPTANCE_TARGET=stub` runs the tests against empty results instead, to show that they don't pass on nothing. `check_criterion_tags.py` checks the tests' `ac` markers against the written specification, which is not published in this repository. `spec-gaps.md` lists criteria the suite could not test as written; it is empty.
