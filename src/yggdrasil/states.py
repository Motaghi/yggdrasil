"""Work-package and project states (A11 amendments 21 and 22). Data only: the lanes own the transitions.

Tracker standard version 2 (`data/tracker-standard.yaml`) adds `in-review`, `stopped` and `cancelled` to the
eight version-1 states, which keep today's meaning. Version-1 records keep reading as version 1; new
work-package records are written as version 2.

- `in-review`: from `authorised` or `rejected` on `ygg wp ready`, or on `ygg wp complete` when the project has
  a reviewer node; back to `authorised` on a revise, a stale review, a dropped review or `one more round`; on
  to `ready-for-test` on an approve or `accept as is` (lane 2).
- `stopped`: from a held cap decision answered `stop`; to `cancelled` on `ygg wp cancel`, or to `authorised` on
  `ygg wp reopen` (lane 2).
- `cancelled`: final; counts as closed for stage readiness and the merge-check trigger; not on the dashboard.

WIP rule (ruling 134): `in-review` counts against the WIP limit (unfinished work); `stopped` and `cancelled`
don't (no one owes work on them).

Project state `ready-to-merge` (D4, amendment 22): a git-repo or shared-tree project (one repo) enters it only
when the last stage is approved, every package is merged and the merge check for the current (main SHA,
integration SHA) pair passed; it leaves to `closed` when main contains the integration head after the main
session's `ygg merge main`, or to `executing` when a package is reopened (lanes 2 and 4).
"""

from __future__ import annotations

PACKAGE_STATES_V1 = ("planned", "authorised", "ready-for-test", "accepted", "rejected", "review-exhausted",
                     "suspended", "merged")
PACKAGE_STATES_V2 = PACKAGE_STATES_V1 + ("in-review", "stopped", "cancelled")
PACKAGE_STATES = PACKAGE_STATES_V2
WIP_STATES = ("authorised", "ready-for-test", "rejected", "in-review")
# Closed for stage readiness and the merge-check trigger (AC-14, AC-27).
CLOSED_PACKAGE_STATES = ("merged", "cancelled")
# Not shown on the dashboard (AC-08).
HIDDEN_PACKAGE_STATES = ("cancelled",)

PROJECT_STATES = ("initiating", "pid-ready", "pid-rejected", "executing", "awaiting-approval", "exception",
                  "ready-to-merge", "closed")
