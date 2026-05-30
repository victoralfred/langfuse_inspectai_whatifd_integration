"""`primary_endpoint_guard` — cardinal #10 configurable primary endpoints.

Phase 2.6b consolidation. Replaces the hardcoded
`failure_improvement_guard` and `baseline_regression_guard` with a
single configurable guard that reads `policy.primary_endpoints` and
dispatches by direction:

- `improvement_above_threshold` (default for `failure` cohort) — emits
  `failure_improvement_below_threshold` when the cohort's improvement
  rate is strictly below `policy.min_failure_improvement_ratio`.
- `non_regression_below_threshold` (default for `baseline` cohort) —
  emits `baseline_regression_above_threshold` when the cohort's
  regression rate strictly exceeds `policy.max_baseline_regression_ratio`.

Per cardinal rule #10's predeclared-cohort-endpoint doctrine, verdicts
derive from cohort-level endpoints declared in advance via
`DecisionPolicy.primary_endpoints`. The default endpoints
(`failure: improvement_above_threshold`, `baseline: non_regression_below_threshold`)
match v0.1's failure-rescue scope; v0.2 may add more directions (e.g.,
latency-reduction) by extending the `EndpointDirection` Literal and
adding a dispatch branch here.

Multi-metric support (one primary metric per cohort today; v0.2 adds
Holm correction for multi-metric) lives in
`MethodologyDisclosure.multiplicity` per cardinal #10. v0.1 has only
one metric per cohort; the `metric` field on `PrimaryEndpoint` is
recorded in the methodology block but doesn't affect this guard's
dispatch.

The guard reuses the existing finding codes from Phase 2.3 — no new
code or fix-suggestion needed. Each emitted finding has the same
shape that the per-cohort guards used in Phase 2.5b. The renderer
sees the same surface.

**v0.1 limitation (cascade-tracked):** the finding codes
`failure_improvement_below_threshold` and
`baseline_regression_above_threshold` encode the v0.1 default cohort
identities into the code namespace. A v0.2 custom policy declaring
`PrimaryEndpoint(cohort="warmup", direction="improvement_above_threshold")`
would emit `failure_improvement_below_threshold` even though the
cohort is `"warmup"`. This is acceptable for v0.1 (failure-rescue
scope only; default cohorts are `failure` + `baseline`), but v0.2
needs direction-keyed codes (e.g. `primary_improvement_below_threshold`)
plus a `cohort` detail field. See cascade-catalog
"Direction-keyed finding codes for v0.2 multi-cohort
primary_endpoint_guard".

Boundary semantics (preserved from the hardcoded guards):
- Improvement: strict `<` so equality at the threshold meets the
  policy's "at least N%" promise.
- Regression: strict `>` so equality at the threshold meets the
  policy's "at most N%" promise.

Precondition for each endpoint: the named cohort exists with non-zero
scored traces. Missing cohorts (the floor's `required_cohort_present`
rule) and zero-scored cohorts (`min_scored_per_required_cohort`) are
the floor's structural concerns; this guard is policy-level and
abstains silently in those cases.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import assert_never

from whatifd.decision.finding_codes import make_decision_finding
from whatifd.types.cohort import CohortResult
from whatifd.types.finding import DecisionFinding
from whatifd.types.policy import DecisionPolicy, PrimaryEndpoint
from whatifd.types.primitives import DecimalString


def primary_endpoint_guard(
    cohort_results: Sequence[CohortResult],
    policy: DecisionPolicy,
) -> list[DecisionFinding]:
    """For each endpoint in `policy.primary_endpoints`, evaluate against
    the matching cohort and emit a `blocks_ship` finding when the
    endpoint fails.

    Findings emit in `policy.primary_endpoints` order — the renderer
    sees them in the order the policy declared them, not in cohort
    discovery order.
    """
    by_name = {c.name: c for c in cohort_results}
    findings: list[DecisionFinding] = []
    for endpoint in policy.primary_endpoints:
        cohort = by_name.get(endpoint.cohort)
        if cohort is None:
            continue  # floor's required_cohort_present catches missing cohorts
        finding = _evaluate_endpoint(endpoint, cohort, policy)
        if finding is not None:
            findings.append(finding)
    return findings


def _evaluate_endpoint(
    endpoint: PrimaryEndpoint,
    cohort: CohortResult,
    policy: DecisionPolicy,
) -> DecisionFinding | None:
    """Dispatch on `endpoint.direction`.

    Returns a finding when the endpoint fails; None when it passes
    (or the precondition isn't met — zero scored traces).

    Uses a `match` statement with an explicit case per Literal value
    so mypy strict catches missing dispatch branches when v0.2+ adds
    new directions to `EndpointDirection`. The Literal exhaustiveness
    pattern is the project standard (see `whatifd/types/verdict.py`'s
    `Verdict` sealed union).
    """
    total_scored = cohort.improved_count + cohort.unchanged_count + cohort.regressed_count
    if total_scored == 0:
        # Floor's min_scored_per_required_cohort rule catches this case
        # structurally. Phase 2.6c partition tightening will make this
        # branch unreachable for production cohorts (cascade-tracked).
        return None

    match endpoint.direction:
        case "improvement_above_threshold":
            return _evaluate_improvement(cohort, total_scored, policy)
        case "non_regression_below_threshold":
            return _evaluate_non_regression(cohort, total_scored, policy)
        case _ as never:
            # Self-documenting exhaustiveness: `assert_never` is mypy's
            # contract that this branch is unreachable. v0.2 adding a new
            # `EndpointDirection` literal without a `case` here is a
            # compile-time error, not a silently-dropped finding.
            assert_never(never)


def _evaluate_improvement(
    cohort: CohortResult,
    total_scored: int,
    policy: DecisionPolicy,
) -> DecisionFinding | None:
    """`improvement_above_threshold`: emit when improvement rate is
    strictly below `policy.min_failure_improvement_ratio`."""
    improvement_rate = cohort.improved_count / total_scored
    # TODO(v0.2): per-endpoint thresholds. The threshold is currently
    # policy-level, not endpoint-level — any cohort using
    # `improvement_above_threshold` reads the same value regardless of
    # cohort name. v0.2 threads thresholds through `PrimaryEndpoint`.
    # See cascade-catalog "Direction-keyed finding codes for v0.2
    # multi-cohort primary_endpoint_guard".
    threshold = policy.min_failure_improvement_ratio

    if improvement_rate >= threshold:
        return None

    observed_str = DecimalString(format(improvement_rate, ".3f"))
    threshold_str = DecimalString(format(threshold, ".3f"))
    return make_decision_finding(
        "failure_improvement_below_threshold",
        message=(
            f"{cohort.name} cohort improvement rate {observed_str} below "
            f"threshold {threshold_str} "
            f"({cohort.improved_count}/{total_scored} traces improved)"
        ),
        details={"observed": observed_str, "threshold": threshold_str},
    )


def _evaluate_non_regression(
    cohort: CohortResult,
    total_scored: int,
    policy: DecisionPolicy,
) -> DecisionFinding | None:
    """`non_regression_below_threshold`: emit when regression rate
    strictly exceeds `policy.max_baseline_regression_ratio`."""
    regression_rate = cohort.regressed_count / total_scored
    # TODO(v0.2): per-endpoint thresholds. Same v0.1 limitation as
    # `_evaluate_improvement` — threshold is policy-level, not
    # endpoint-level. See cascade-catalog "Direction-keyed finding
    # codes for v0.2 multi-cohort primary_endpoint_guard".
    threshold = policy.max_baseline_regression_ratio

    if regression_rate <= threshold:
        return None

    observed_str = DecimalString(format(regression_rate, ".3f"))
    threshold_str = DecimalString(format(threshold, ".3f"))
    return make_decision_finding(
        "baseline_regression_above_threshold",
        message=(
            f"{cohort.name} cohort regression rate {observed_str} exceeds "
            f"threshold {threshold_str} "
            f"({cohort.regressed_count}/{total_scored} traces regressed)"
        ),
        details={"observed": observed_str, "threshold": threshold_str},
    )
