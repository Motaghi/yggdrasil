None

No test in this suite passes against the empty stub. Run against the stub, the whole suite collects 160 cases (parameter variants included) and every one fails on an assertion, with no collection errors, fixture errors or skips.

The suite covers every in-suite criterion (see ac-table.md). AC-16, AC-25 and AC-28 run outside the suite on the real Codex CLI, so they have no tests or tags here.

To check this yourself, from the repository root:

```sh
ACCEPTANCE_TARGET=stub python -m pytest tests/acceptance-v1-1-codex -q -p no:cacheprovider
```

The five upgrade tests in test_ac57_upgrade.py also need `YGG_V100_BIN`, the path to an installed v1.0.0 `ygg`; without it they skip against a real build, and they fail against the stub.
