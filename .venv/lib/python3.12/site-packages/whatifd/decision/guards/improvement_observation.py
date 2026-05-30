"""`improvement_observation_guard` — info-severity observation.

Emits the `improvement_observed` info finding when the failure cohort's
median delta is meaningfully positive (above the practical-delta
epsilon). The finding is observational — it does NOT drive the verdict
on its own; the verdict layer reads it for the report's narrative
section ("the change improved the failure cohort").

The symmetric "regression observed on baseline" finding has a different
shape (severity blocks_ship, not info) and lands with the
baseline-regression guard in a future PR.

This guard pairs with `practical_delta_guard`: at most one of them
emits per run. When the failure cohort's median delta is above epsilon,
this guard fires; when at-or-below, the practical_delta guard fires.
That mutual exclusion isn't enforced structurally (both are pure
functions; verdict layer reads severities), but tests pin the behavior.

Note on the redundant `parse_decimal_string` call: when this guard and
`practical_delta_guard` both run on the same cohort, both call
`parse_decimal_string` independently on `median_delta`. The redundancy
is intentional — each guard is self-contained for testability and
reasoning. Caching parsed floats across guards (via Phase 2.6's
verdict computation passing pre-parsed values) is a future
optimization; today's overhead is negligible.

TODO(phase-2.6): replace this redundant parse when verdict computation
threads pre-parsed `median_delta_float` into the guard signature. See
cascade-catalog "Guard pre-parse caching — Phase 2.6 verdict
computation".
"""

from __future__ import annotations

from collections.abc import Sequence

from whatifd.decision.finding_codes import make_decision_finding
from whatifd.serialization.decimal import FieldLabel, parse_decimal_string
from whatifd.types.cohort import CohortResult
from whatifd.types.finding import DecisionFinding
from whatifd.types.policy import DecisionPolicy


def improvement_observation_guard(
    cohort_results: Sequence[CohortResult],
    policy: DecisionPolicy,
) -> list[DecisionFinding]:
    """Emit `improvement_observed` when the failure cohort's median
    delta is strictly above `policy.practical_delta_epsilon`.

    Strict `>` is intentional: the practical-delta epsilon is the "no
    meaningful effect" threshold; a delta exactly at it is noise, not
    improvement. The matching guard (`practical_delta_guard`) uses
    `<=` so the two are mutually exclusive.
    """
    failure = next((c for c in cohort_results if c.name == "failure"), None)
    if failure is None or failure.median_delta is None:
        return []

    median_delta_float = parse_decimal_string(
        failure.median_delta,
        field=FieldLabel(f"CohortResult.median_delta (cohort={failure.name!r})"),
    )

    threshold = policy.practical_delta_epsilon
    if median_delta_float <= threshold:
        return []

    threshold_str = format(threshold, ".3f")
    return [
        make_decision_finding(
            "improvement_observed",
            message=(
                f"failure cohort median delta {failure.median_delta} "
                f"above practical-delta threshold {threshold_str} "
                "(improvement observed)"
            ),
            details={
                "median_delta": failure.median_delta,
                "threshold": threshold_str,
            },
        )
    ]
