"""Verdict computation — Phase 2.6a.

`compute_verdict` is the single entry point that turns
`(cohort_results, floor, policy)` into a `Verdict`. It composes the
existing decision pipeline:

1. **Floor evaluation** (`evaluate_floor`) — Cardinal #2 structural
   gate. If the floor returns a `FloorFailureSet`, the verdict is
   `Inconclusive` regardless of any guard findings. The floor
   precedence is absolute.

2. **Guard chain** (`run_guards`) — Each guard in the configured
   sequence emits 0+ `DecisionFinding`s. Order matches registration;
   `run_guards` is the chain composer.

3. **Severity sorting** — Findings are partitioned by severity:
   - `blocks_all` → forces `Inconclusive` (operational catastrophes
     like cache corruption that break the run regardless of policy)
   - `blocks_ship` → produces `DontShip` when no `blocks_all` is
     present; the policy says "no Ship" but evidence existed
   - `degrades_trust` → accumulates against future quality thresholds
     (v1.0+); v0.1 collects but does not blocking-act on these
   - `info` → observational; no verdict impact, included in `findings`
     for the renderer

4. **Verdict construction**:
   - Any `blocks_all` → `Inconclusive(blocking_findings=blocks_all_findings)`
   - Else any `blocks_ship` → `DontShip(blocking_findings=blocks_ship_findings)`
   - Else → `Ship(proof=floor_passed_proof)`

Per cardinal #2, the `Ship` branch is the ONLY branch that consumes
the `FloorPassedProof`. The witness token is structurally required —
`compute_verdict` cannot construct `Ship` without one.

## v0.1 guard registration

Phase 2.6b uses four landed guards (cardinal #10's three-layer
structure plus the operational CI-availability check):
- `primary_endpoint_guard` (rate-based, configurable via
  `policy.primary_endpoints`, blocks_ship) — replaces the Phase 2.5b
  hardcoded pair (`failure_improvement_guard` + `baseline_regression_guard`)
  with a dispatcher that reads each declared `PrimaryEndpoint` and
  emits the matching finding code based on direction.
- `practical_delta_guard` (magnitude floor, blocks_ship)
- `improvement_observation_guard` (observational info)
- `ci_availability_guard` (cohort-level CI availability, blocks_all)

`cache_staleness_guard` is deferred to Phase 3 (cache subsystem).

## CI unavailability handling

Per V0_1_DECISION_RECORD §6, v0.1 has no `--accept-no-ci` escape hatch.
`ci_availability_guard` emits `ci_unavailable_for_required_cohort` at
`blocks_all` severity unconditionally; the verdict is `Inconclusive`.
The policy lever for accepting wider (but computable) CIs is
`policy.max_ci_width`, read by the deferred `ci_meaningful` policy
check (see cascade-catalog "ci_meaningful policy-guard wiring").
"""

from __future__ import annotations

from collections.abc import Sequence

from whatifd.decision.floor import FloorFailureSet, FloorPassedProof, evaluate_floor
from whatifd.decision.guards import (
    Guard,
    ci_availability_guard,
    improvement_observation_guard,
    practical_delta_guard,
    primary_endpoint_guard,
    run_guards,
)
from whatifd.types.cohort import CohortResult
from whatifd.types.manifest import ExperimentShape
from whatifd.types.policy import DecisionPolicy, TrustFloor
from whatifd.types.verdict import DontShip, Inconclusive, Ship, Verdict

# v0.1 default guard chain, in registration order. Order matches the
# cardinal #10 layer structure: rate-based primary endpoints first
# (load-bearing, configurable via `policy.primary_endpoints`),
# magnitude layer, observational layer, then operational guards
# (CI availability). Test_layer_composition pins the no-mutation
# contract; the per-guard tests pin the boundary semantics.
#
# Phase 2.6b consolidation: `primary_endpoint_guard` replaces the
# Phase 2.5b `failure_improvement_guard` and `baseline_regression_guard`
# pair. The configurable guard reads `policy.primary_endpoints` and
# dispatches by direction; for the default policy it emits the same
# findings the hardcoded pair did. Cascade-catalog "Phase 2.5 deferred
# guards" entry's bullet 4 partial resolution.
_DEFAULT_GUARDS: tuple[Guard, ...] = (
    primary_endpoint_guard,
    practical_delta_guard,
    improvement_observation_guard,
    ci_availability_guard,
)

# Phase C (v0.2): regression_check experiment shape has no `failure`
# cohort — only baseline-vs-baseline-with-change. The failure-cohort
# guards (practical_delta, improvement_observation) read the failure
# cohort directly and would emit spurious findings. primary_endpoint
# is configurable via policy.primary_endpoints and naturally handles
# the regression-check policy when the policy declares only the
# baseline non-regression endpoint.
_REGRESSION_CHECK_GUARDS: tuple[Guard, ...] = (
    primary_endpoint_guard,
    ci_availability_guard,
)


def _guards_for_shape(shape: ExperimentShape) -> tuple[Guard, ...]:
    """Map experiment_shape → default guard sequence."""
    if shape == "regression_check":
        return _REGRESSION_CHECK_GUARDS
    # failure_rescue (the v0.1 default)
    return _DEFAULT_GUARDS


def _required_cohorts_for_shape(shape: ExperimentShape, policy: DecisionPolicy) -> tuple[str, ...]:
    """Derive the floor's required-cohorts list from the experiment shape.

    Failure-rescue requires both `failure` and `baseline` cohorts (the
    v0.1 default). Regression-check requires only `baseline`. The
    `policy.required_cohorts` field is left as the v0.1 default;
    shape-derived overrides take precedence so a user who hand-set a
    policy doesn't need to also remember to flip required_cohorts.
    """
    if shape == "regression_check":
        return ("baseline",)
    return policy.required_cohorts


def compute_verdict(
    cohort_results: Sequence[CohortResult],
    floor: TrustFloor,
    policy: DecisionPolicy,
    *,
    guards: Sequence[Guard] | None = None,
    experiment_shape: ExperimentShape = "failure_rescue",
) -> Verdict:
    """Compute the verdict for a run.

    Cardinal #2: floor failures produce `Inconclusive` regardless of
    guard findings. The floor evaluation is the structural gate.

    `guards` defaults to the v0.1 `_DEFAULT_GUARDS` tuple. Tests pass
    a custom sequence to exercise specific scenarios in isolation.

    Returns one of `Ship`, `DontShip`, `Inconclusive`. The `Ship`
    branch consumes the `FloorPassedProof` from `evaluate_floor`;
    structurally cannot construct `Ship` without that token.

    `floor` is typed `TrustFloor` directly — mypy strict enforces the
    contract at call sites. No runtime isinstance check; per the
    project's enforcement-strength hierarchy, type-level prevention
    is stronger than runtime defense-in-depth.
    """
    resolved_guards = guards if guards is not None else _guards_for_shape(experiment_shape)

    floor_outcome = evaluate_floor(
        cohort_results,
        floor,
        _required_cohorts_for_shape(experiment_shape, policy),
    )

    # Run guards regardless of floor outcome so observational findings
    # (improvement_observed, etc.) appear in the report even when the
    # floor is the structural reason for Inconclusive.
    findings = run_guards(resolved_guards, cohort_results, policy)

    # Pre-compute severity-partitioned views once; both Inconclusive
    # branches and the DontShip branch read from these.
    inconclusive_blocking = [f for f in findings if f.severity in ("blocks_ship", "blocks_all")]
    blocks_all = [f for f in findings if f.severity == "blocks_all"]
    blocks_ship = [f for f in findings if f.severity == "blocks_ship"]

    cohort_results_list = list(cohort_results)

    # Cardinal #2: floor failures → Inconclusive, regardless of guards.
    if isinstance(floor_outcome, FloorFailureSet):
        return Inconclusive(
            cohort_results=cohort_results_list,
            findings=findings,
            blocking_findings=inconclusive_blocking,
            floor_failures=list(floor_outcome.failures),
        )

    # Floor passed; floor_outcome is a FloorPassedProof.
    assert isinstance(floor_outcome, FloorPassedProof)  # narrows for mypy

    if blocks_all:
        # Operational catastrophe at policy level (e.g., cache lock
        # unavailable). Floor passed but evidence is unrenderable.
        return Inconclusive(
            cohort_results=cohort_results_list,
            findings=findings,
            blocking_findings=inconclusive_blocking,
            floor_failures=[],
        )

    if blocks_ship:
        return DontShip(
            cohort_results=cohort_results_list,
            findings=findings,
            blocking_findings=blocks_ship,
        )

    return Ship(
        proof=floor_outcome,
        cohort_results=cohort_results_list,
        findings=findings,
    )
