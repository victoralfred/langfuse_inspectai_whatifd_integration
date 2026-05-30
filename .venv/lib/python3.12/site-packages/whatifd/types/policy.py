"""`TrustFloor`, `DecisionPolicy`, `PrimaryEndpoint` — Phase 1.5 policy types.

The trust floor is structural; the decision policy layers on top.

`TrustFloor` is about evidence existence — below the floor, no verdict
can be rendered. The floor is versioned (sticky in manifest); v0.1
ships floor v1. Defaults are provisional and marked for revision after
the first 10 production runs (per V0_1_DECISION_RECORD § 4).

`DecisionPolicy` is about evidence quality — configurable thresholds
that gate Ship/Don't Ship decisions ABOVE the floor. Cardinal rule #2
doctrine: policy can be stricter than floor, never weaker. Stricter is
enforced by the guard chain at evaluation time (Phase 2.6); weaker is
prevented because the floor is structural and the policy never gets
asked about below-floor cases.

`PrimaryEndpoint` is the cardinal-#10 commitment: verdicts derive from
predeclared cohort-level endpoints. v0.1 ships two endpoints: failure-
cohort improvement and baseline-cohort non-regression. Multiple primary
metrics with Holm correction is deferred to v0.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

EndpointDirection = Literal[
    "improvement_above_threshold",
    "non_regression_below_threshold",
]

ScorerCacheMode = Literal["auto", "on", "off", "read_only", "refresh"]
ScorerCacheStorageProfile = Literal["normalized_result_only", "full_judge_io"]


@dataclass(frozen=True, slots=True)
class PrimaryEndpoint:
    """A predeclared cohort-level endpoint that drives the verdict.

    Per cardinal rule #10: "verdicts derive from predeclared cohort-level
    endpoints; per-trace evidence is descriptive, not inferential."

    `cohort` — which cohort the endpoint is evaluated against ("failure",
    "baseline", or future expansions).
    `direction` — improvement or non-regression. Other directions
    (e.g., latency-reduction) are deferred to v0.2.
    `metric` — the scorer metric (e.g., "faithfulness"). v0.1 supports
    one primary metric per cohort; multiple metrics is v0.2 (Holm
    correction).

    The numeric threshold for each direction lives on `DecisionPolicy`:
    `min_failure_improvement_ratio` for "improvement_above_threshold";
    `max_baseline_regression_ratio` for "non_regression_below_threshold".
    Endpoint metadata + policy threshold = full endpoint definition.
    """

    cohort: str
    direction: EndpointDirection
    metric: str = "faithfulness"


# Default v0.1 primary endpoints. Tuple is immutable, so safe as a
# dataclass default. Per statistical-defaults.md, these are the
# minimum endpoints that produce a credible Ship verdict.
_DEFAULT_PRIMARY_ENDPOINTS: tuple[PrimaryEndpoint, ...] = (
    PrimaryEndpoint(
        cohort="failure",
        direction="improvement_above_threshold",
        metric="faithfulness",
    ),
    PrimaryEndpoint(
        cohort="baseline",
        direction="non_regression_below_threshold",
        metric="faithfulness",
    ),
)


@dataclass(frozen=True, slots=True)
class TrustFloor:
    """Structural minimum below which no verdict can be rendered.

    Cardinal rule #2: floor cannot be bypassed by configuration. The
    `evaluate_floor()` function in `whatifd/decision/floor.py` checks
    these rules per-cohort; failing any one produces an `Inconclusive`
    verdict regardless of policy state.

    Default values are provisional. The 0.50 replay-validity ratio is
    marked for revision after the first 10 production runs (per
    V0_1_DECISION_RECORD § 4).

    `version` is sticky in the manifest — existing runs use the floor
    version they were built against. v0.2 may bump to floor v2; v0.1
    runs continue to validate against v1.
    """

    version: str = "v1"
    source: str = "whatifd-0.1.0"
    min_selected_per_required_cohort: int = 5
    min_replayed_per_required_cohort: int = 5
    min_scored_per_required_cohort: int = 5
    min_replay_validity_ratio_per_required_cohort: float = 0.50

    @classmethod
    def rule_names(cls) -> tuple[str, ...]:
        """Canonical ordering of floor rule names.

        Per the "Floor table rendering — passing rules need to be
        enumerable" cascade entry. The renderer iterates this list to
        produce the per-cohort floor evaluation table (scenario 4 in
        docs/walkthroughs/), checking each rule against the cohort's
        `floor_failures` list to determine pass/fail.

        Returning a tuple (not a list) signals immutability: the
        canonical order is fixed; reordering rules is a schema change
        per the public-schema versioning rules.
        """
        return (
            "min_selected_per_required_cohort",
            "min_replayed_per_required_cohort",
            "min_scored_per_required_cohort",
            "min_replay_validity_ratio_per_required_cohort",
        )


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    """User-configurable policy that layers on top of the floor.

    `require_baseline` defaults to True (baseline cohort required for
    Ship verdict). Per V0_1_DECISION_RECORD addendum 2026-05-05, v0.1
    ships failure-rescue scope with `cohort: str` flexibility for v0.2
    `regression_check` expansion.

    `primary_endpoints` per cardinal rule #10 — predeclared, drive the
    verdict. Defaults to v0.1's two endpoints; users may add more in
    v0.2 with Holm correction.

    Cache policy fields are configurable but bounded — `scorer_cache_mode`
    is sealed to a literal set; `storage_profile` controls what's
    persisted (cardinal #5 redaction defaults to `normalized_result_only`).

    Per V0_1_DECISION_RECORD §6, v0.1 has NO `--accept-no-ci` escape
    hatch: CI unavailability forces Inconclusive (blocks_all severity).
    The policy lever for accepting wider CIs is `max_ci_width` (raise
    or set None to disable). Persistent acceptance mechanisms are
    deferred to v1.0 as a coherent unit.
    """

    # Cohort requirements
    require_baseline: bool = True
    required_cohorts: tuple[str, ...] = ("failure", "baseline")

    # Primary endpoints per cardinal #10
    primary_endpoints: tuple[PrimaryEndpoint, ...] = field(
        default_factory=lambda: _DEFAULT_PRIMARY_ENDPOINTS
    )

    # Quality thresholds (above-floor concerns)
    max_baseline_regression_ratio: float = 0.10
    min_failure_improvement_ratio: float = 0.50
    max_ci_width: float | None = None  # None disables the check

    # Practical-delta threshold (cardinal #10; statistical-defaults)
    practical_delta_epsilon: float = 0.05

    # Cache policy
    scorer_cache_mode: ScorerCacheMode = "auto"
    scorer_cache_warn_after_days: int = 30
    scorer_cache_block_after_days: int = 90
    scorer_cache_storage_profile: ScorerCacheStorageProfile = "normalized_result_only"
