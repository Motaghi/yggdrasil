# A11 acceptance suite

This suite covers the v1.0 feature set, in five files: configuration keys and release switches (`test_lane0_seams.py`), lane hooks and commit guards (`test_lane1_hooks.py`), the conductor's handling of work packages, reviews, start confirmation and phase ends (`test_lane2_conductor.py`), launches, agent permissions, T3 and the main-session skill (`test_lane3_launcher.py`), and alerts, the hourly summary, the dashboard, `ygg doctor` and the merge check (`test_lane4_modules.py`).

The acceptance suites are frozen acceptance tests for the signed criteria. Each test carries an `ac` marker naming its criterion and a `release` marker: `v1.0-blocker`, `v1.0-should` or `fallback-v1.1`. `ac-table.md` lists the criteria and their tests. Don't edit a test to make a change pass; raise an issue instead (see [Contributing](../../docs/contributing.md)). The rows marked `fallback-v1.1` that fail against v1.0.0, and why, are listed in [Contributing](../../docs/contributing.md#rows-that-fail-by-design-in-v100).

The tests drive `ygg` only through its command line, HTTP requests, files, external commands and the recording fakes of the earlier suites, and import no application code. `driver_a11.py` extends the drivers of the core, A9 and A10 suites, and `stub_a11.py` returns empty results for the stub run. The AC-25 tests read `skills/yggdrasil-l1/SKILL.md` from the repository through `YGG_CORE_ROOT`.

Run it from the top folder of the repository:

```bash
ACCEPTANCE_TARGET=real YGG_BIN="$(command -v ygg)" YGG_CORE_ROOT="$PWD" pytest tests/acceptance-a11 -q
```

`ACCEPTANCE_TARGET=stub` runs the tests against empty results instead, to show that they don't pass on nothing; `stub-passes.md` lists the tests that pass there because their expected result is itself empty. Release switches are read from the package's `release.yaml`; in test mode, `YGG_RELEASE_FILE` names another file and `YGG_TEST_RELEASE_OFF` turns named switches off. `check_criterion_tags.py` checks the tests' markers against the written specification, which is not published in this repository. `spec-gaps.md` lists criteria the suite could not test as written; it is empty.
