"""`practical_delta_guard` — cardinal #10 *magnitude layer* (blocks_ship).

Cardinal rule #10 (statistical claims must match the design) is
enforced by two layers in v0.1:

1. The **primary endpoint** — `failure_improvement_guard` checks the
   rate-based question ("did at least N% of the failure cohort
   improve?"). This is the load-bearing endpoint that drives the
   verdict per #10's "verdicts derive from predeclared cohort-level
   endpoints" doctrine. The symmetric `baseline_regression_guard`
   covers non-regression on the baseline cohort.

2. The **magnitude layer** (this guard) — a supplementary defense
   that blocks Ship even if the primary endpoint passes, when the
   observed median delta is inside the practical-delta noise floor
   (`<= epsilon`). "Statistically observable but practically negligible"
   is the cardinal #10 framing; the magnitude check refuses to ship a
   noise-floor win regardless of how the primary endpoint scored.

Both layers are needed: rate-only could ship a 51% improvement of
+0.001 (technically met, practically meaningless); magnitude-only could
ship a +0.5 delta that only 5% of the cohort experienced (huge effect,
narrow base).

This guard reads the failure cohort only — that's where we evaluate
"the change rescued failures by enough to ship". The baseline
non-regression guard (separate, future PR) handles the symmetric check
on the baseline cohort.

Precondition: a `failure` cohort exists with a non-None `median_delta`.
When either is missing this guard emits no finding; the floor or
another guard catches the missing-cohort case. The floor's
`required_cohort_present` rule is the structural check; this guard
operates above the floor on quality.

"""

from __future__ import annotations

from collections.abc import Sequence

from whatifd.decision.finding_codes import make_decision_finding
from whatifd.serialization.decimal import FieldLabel, parse_decimal_string
from whatifd.types.cohort import CohortResult
from whatifd.types.finding import DecisionFinding
from whatifd.types.policy import DecisionPolicy


def practical_delta_guard(
    cohort_results: Sequence[CohortResult],
    policy: DecisionPolicy,
) -> list[DecisionFinding]:
    """Emit `practical_delta_below_threshold` when the failure cohort's
    median delta is at or below `policy.practical_delta_epsilon`.

    Equality counts as below-threshold: the epsilon represents "the
    smallest meaningful effect"; a delta exactly at that level isn't
    above noise.
    """
    failure = next((c for c in cohort_results if c.name == "failure"), None)
    if failure is None or failure.median_delta is None:
        return []

    median_delta_str = failure.median_delta
    median_delta_float = parse_decimal_string(
        median_delta_str,
        field=FieldLabel(f"CohortResult.median_delta (cohort={failure.name!r})"),
    )

    threshold = policy.practical_delta_epsilon
    if median_delta_float > threshold:
        return []

    return [
        make_decision_finding(
            "practical_delta_below_threshold",
            message=(
                f"failure cohort median delta {median_delta_str} "
                f"<= practical-delta threshold {threshold:.3f}"
            ),
            details={
                "median_delta": median_delta_str,
                "threshold": format(threshold, ".3f"),
            },
        )
    ]
