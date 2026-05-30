"""Shared internal constants for the `whatifd.render` subpackage.

Promotes cross-module-shared values out of the individual format
modules so both `ci_status.py` and `summary.py` (and Phase 7.1's
`markdown.py`) import from a single source of truth — neither
renderer depends on the other's internals.

The constants here:

- `COHORT_FAILURE` / `COHORT_BASELINE` — canonical cohort names
  for the v0.1 failure-rescue shape. Mirror
  `DecisionPolicy.required_cohorts` default `("failure",
  "baseline")`. v0.2 regression-check shapes will introduce
  additional names; the renderers' generic per-cohort fallback
  paths handle unknown names without referencing these constants.

- `SEVERITY_RANK` — total ordering on `Severity` for "highest-
  severity finding" selection in compact formats. Higher rank =
  more load-bearing for the rendered reason. The strict-subscript
  discipline (NOT `.get(..., 0)`) is enforced at the call sites:
  a `Severity` value outside the closed Literal arriving here is
  schema drift and must surface as `KeyError`, not silently demote
  to below `info`.

These names are PUBLIC within `whatifd.render` (no leading
underscore) but the module itself is `_constants` (leading
underscore) — sibling renderers import freely; consumers outside
the subpackage should not.
"""

from __future__ import annotations

from whatifd.types.finding import Severity

COHORT_FAILURE = "failure"
COHORT_BASELINE = "baseline"

SEVERITY_RANK: dict[Severity, int] = {
    "blocks_all": 4,
    "blocks_ship": 3,
    "degrades_trust": 2,
    "info": 1,
}

# Human-readable verdict labels keyed by the wire `verdict_state`
# Literal. Single source of truth across the three renderers
# (ci_status, summary, markdown) so a future label change happens
# in one place.
VERDICT_LABEL: dict[str, str] = {
    "ship": "Ship",
    "dont_ship": "Don't Ship",
    "inconclusive": "Inconclusive",
}


__all__ = [
    "COHORT_BASELINE",
    "COHORT_FAILURE",
    "SEVERITY_RANK",
    "VERDICT_LABEL",
]
