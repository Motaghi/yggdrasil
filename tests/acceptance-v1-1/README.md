# Yggdrasil v1.1 acceptance suite

This suite checks the v1.1 acceptance criteria AC-01 to AC-30 (AC-25 was withdrawn). It drives the `ygg` command and
its package only through `driver.py`, so it does not read the source tree. Each test carries an `ac` marker naming the
criterion it covers, and `ac-table.md` lists the collected tests by criterion.

## Running it

```sh
python -m pytest tests/acceptance-v1-1 -q -p no:cacheprovider
```

By default the suite runs against a real `ygg`: set `YGG_BIN` to the installed `ygg` command. AC-14 and AC-29 install
a built wheel into a temporary tool folder with `uv`, so pass the wheel with `--acceptance-wheel <path>`. AC-29 is
skipped on a non-Linux host, or when `bwrap` or `strace` is missing; a skip is not a pass of that criterion. A Linux
host with both tools but no usable network namespace fails the test.

`ACCEPTANCE_TARGET=stub` runs the suite against `stub.py`, which returns only empty results. It checks that the
tests are collected and tagged, and it proves nothing about the product.

## How the tests are built

- Tests call application boundaries only through `driver.py`. The only import into a target subprocess is the
  process start-identity helper, which the process-identity checks need.
- Pytest's temporary folders are kept inside the checkout.
- The expected values come from the acceptance examples under each criterion's tag. A parametrised row is one
  example. AC-09, AC-20 and AC-21 each combine two of their examples into one test with a positive control, and
  AC-23's empty-config row expects only exit 0, so it also passes against the stub.
- Optional criteria (AC-15, AC-19, AC-20) are marked `v1.1-optional`; the rest are `v1.1-mandatory`.
