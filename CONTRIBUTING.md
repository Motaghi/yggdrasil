# Contributing

The full guide is in [docs/contributing.md](docs/contributing.md). Two things to do first in a fresh clone:

- **Turn on the leak scan.** Git doesn't install hooks from a clone. Run `git config core.hooksPath scripts/hooks` so `scripts/hooks/pre-commit` checks every commit you make for secret-shaped strings and, if you set `YGG_LEAK_DENYLIST` or `git config ygg.leakDenylist <file>`, for terms from your own denylist.
- **Report security problems privately**, as described in [SECURITY.md](SECURITY.md), not in a public issue.
