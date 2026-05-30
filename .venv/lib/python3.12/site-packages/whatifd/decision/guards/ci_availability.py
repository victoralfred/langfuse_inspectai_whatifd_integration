"""`ci_availability_guard` — required-cohort CI availability check.

Per cardinal rule #10's predeclared-cohort-endpoint doctrine, a verdict
that depends on cohort-level uncertainty cannot be defensibly rendered
when CI is unavailable on a required cohort. This guard reads
`CohortResult.ci_computable` for each cohort named in
`policy.required_cohorts` and emits
`ci_unavailable_for_required_cohort` (blocks_all) when CI is missing
on any of them.

Pairs with `FAILURE_CODE_REGISTRY['ci_uncomputable_for_required_cohort']`:
the failure record is the operational fact (bootstrap returned
None / sample too small / zero variance); this finding is the policy
conclusion (verdict cannot ship without it).

The blocks_all severity forces Inconclusive. Per V0_1_DECISION_RECORD §6,
v0.1 has no `--accept-no-ci` escape hatch: CI unavailability is treated
as a policy concern severe enough (blocks_all) to force Inconclusive.
The policy lever for accepting wider CIs is `policy.max_ci_width` (read
by the deferred `ci_meaningful` policy-quality check, not this guard;
see cascade-catalog "ci_meaningful policy-guard wiring").

Precondition: a cohort named in `policy.required_cohorts` exists in
`cohort_results`. Missing cohorts are the floor's
`required_cohort_present` rule, not this guard's concern.

The `derived_from_failures` field on the emitted finding is left
empty here. Phase 2.6 / projection layer threads the matching failure
record IDs in once the failure-record collection is plumbed end-to-end;
the cascade catalog tracks that wiring.
"""

from __future__ import annotations

from collections.abc import Sequence

from whatifd.decision.finding_codes import make_decision_finding
from whatifd.types.cohort import CohortResult
from whatifd.types.finding import DecisionFinding
from whatifd.types.policy import DecisionPolicy

# Placeholder for `derived_from_failures` until Phase 2.6 plumbs real
# failure-record IDs through the verdict pipeline. Exported (single
# underscore) so tests can lock it; once Phase 2.6 lands, the test
# that asserts this constant flips and the constant is removed.
_PHASE_2_6_PLACEHOLDER = "pending_phase_2_6_plumbing"


def ci_availability_guard(
    cohort_results: Sequence[CohortResult],
    policy: DecisionPolicy,
) -> list[DecisionFinding]:
    """Emit `ci_unavailable_for_required_cohort` for every required
    cohort whose `ci_computable` is False.

    One finding per affected cohort. Order matches the order of
    `policy.required_cohorts`; cohorts not present in `cohort_results`
    are skipped (the floor's `required_cohort_present` rule catches
    them structurally).
    """
    by_name = {c.name: c for c in cohort_results}
    findings: list[DecisionFinding] = []
    for required_name in policy.required_cohorts:
        cohort = by_name.get(required_name)
        if cohort is None:
            # Floor's required_cohort_present rule catches missing
            # cohorts. This guard is above the floor; missing cohort
            # is structural, not a CI-availability concern.
            continue
        if cohort.ci_computable:
            continue
        # CI is unavailable. The reason field on CohortResult is
        # CIUnavailableReason | None — when ci_computable is False the
        # reason should be populated, but we guard against None anyway
        # to keep the guard pure (no upstream-bug hiding; if reason is
        # None when ci_computable is False, that's a projection-layer bug
        # that surfaces as a "reason: unspecified" finding the renderer
        # makes visible).
        reason = cohort.ci_unavailable_reason or "unspecified"
        findings.append(
            make_decision_finding(
                "ci_unavailable_for_required_cohort",
                message=(f"CI unavailable for required cohort {cohort.name!r}: {reason}"),
                details={"cohort": cohort.name, "reason": reason},
                # TODO(phase-2.6c): replace this placeholder with the
                # real failure-record IDs once Phase 2.6 plumbs failure
                # records end-to-end through the verdict pipeline. See
                # cascade-catalog "Phase 2.5 deferred guards" → bullet 4.
                # The constant string makes this grep-discoverable.
                derived_from_failures=[_PHASE_2_6_PLACEHOLDER],
            )
        )
    return findings
