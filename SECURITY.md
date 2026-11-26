# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue or pull request.

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. Include what you found, the commit or release you tested, the steps to reproduce it, and what an attacker could do with it. You will get an answer in that private advisory, and we will agree with you on when the fix and the details are published.

## What counts

This version's guards are accident guards, not containment. Its threat model and the known ways around the guards are written down in [docs/security.md](docs/security.md#known-gaps). A report that one of those listed gaps can be used is welcome but is not a new vulnerability. Reports we especially want:

- a guard or check behaving differently from what [docs/security.md](docs/security.md) says;
- a way for an agent session to run a command reserved for the user through `ygg` itself while its session markers are still set;
- a push the push check should refuse that it allows;
- a Codex builder writing outside its box, changing another lane or the home's git folder, or running a `ygg` command its relay does not allow;
- a secret reaching a tracker record, message or log that the docs say is redacted;
- anything in this repository that should not be public.

## Supported versions

The latest release and the latest commit on `main` are supported while Yggdrasil is in its early versions.
