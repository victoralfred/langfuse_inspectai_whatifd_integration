"""`FloorFailure` and `CohortResult` — per-cohort artifacts.

Cardinal rule #2 doctrine: the trust floor is about evidence existence,
not evidence quality. Below the floor, no verdict can be rendered (the
run is `Inconclusive`); above the floor, evidence exists but its quality
is a policy concern.

`CohortResult` is the per-cohort artifact carrying both the raw counts
(selected/replayed/scored) and the floor evaluation outcome. One of these
is produced per required cohort during `evaluate_floor()` (Phase 2).

`FloorFailure` is the structured record of a single floor-rule violation.
Replaces an earlier prose-only "list of failed rules" with a typed shape
so renderers and downstream consumers don't have to re-parse strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from whatifd.exceptions import InvariantViolationError
from whatifd.types.primitives import DecimalString


@dataclass(frozen=True, slots=True)
class FloorFailure:
    """One trust-floor rule that did not pass for one cohort.

    `severity` is constrained to the two values that make sense for floor
    rules: `blocks_ship` for quality-floor failures, `blocks_all` for
    evidence-existence failures (e.g., zero scored traces). Floor failures
    never produce `info` or `degrades_trust` severity — the floor exists
    precisely to refuse rendering verdicts when these conditions hit.

    `observed` accepts `float | int | str` to handle:
    - integer counts (selected, replayed, scored)
    - decimal-formatted ratios (replay validity ratio as DecimalString)
    - string descriptors when the failure mode isn't numeric
    """

    rule: str
    observed: float | int | str
    threshold: float | int
    severity: Literal["blocks_ship", "blocks_all"]


CIUnavailableReason = Literal[
    "sample_too_small",
    "zero_variance",
    "computation_failed",
]


@dataclass(frozen=True, slots=True)
class CohortResult:
    """Per-cohort stats + floor evaluation outcome.

    The unit of statistical inference per cardinal rule #10 — verdicts
    derive from per-cohort primary endpoints, not per-trace observations.

    Per V0_1_DECISION_RECORD §2 the CI status is split into two fields:

    - `ci_computable` (structural): whether bootstrap CI was computed
      successfully. False when the bootstrap couldn't run (sample too
      small, zero variance, computation failed). When False,
      `ci_unavailable_reason` carries the structured reason. When
      True, `ci_unavailable_reason` is None. `ci_availability_guard`
      reads this and emits `blocks_all` for required cohorts.
    - `ci_meaningful` (policy quality): whether the computed CI is
      narrow enough to be actionable (width below
      `policy.max_ci_width`). Defaults to True for v0.1; the
      width-vs-threshold check is wired in a later phase (cache
      subsystem + stats layer). False is only valid when
      `ci_computable=True`. See cascade-catalog
      "ci_meaningful policy-guard wiring".

    The split keeps cardinal #2 honest: structural failures
    (`ci_computable=False`) and policy-quality concerns
    (`ci_meaningful=False`) live at different layers with different
    severities. Conflating them into a single `ci_available` was the
    v0.1 doctrine drift the skill-alignment pass corrects.

    Numeric fields in the determinism budget (`median_delta`, `ci_lower`,
    `ci_upper`) are typed `DecimalString` per cardinal rule #4. Float
    arithmetic happens internally; emission via `format(value, '.3f')`
    in `whatifd/serialization/decimal.py` (Phase 5) is platform-stable.

    `floor_passed` is True iff `floor_failures` is empty.

    `improved_count`, `unchanged_count`, `regressed_count` carry the
    per-cohort outcome partition over scored traces (per cardinal #10
    paired-delta unit of analysis): a trace is `improved` when its
    paired delta exceeds `policy.practical_delta_epsilon`, `regressed`
    when it falls below `-epsilon`, and `unchanged` otherwise. The
    rate-based guards (`baseline_regression_guard`,
    `failure_improvement_guard`) read these counts to compute
    regression/improvement rates against the policy thresholds. The
    counts default to 0 so pre-Phase-2.5b construction sites
    (test fixtures, the floor evaluator) continue to work without
    rate data; guards check `total > 0` before computing rates.
    """

    name: str  # "failure", "baseline", or future
    selected: int
    replayed: int
    scored: int

    ci_computable: bool
    ci_unavailable_reason: CIUnavailableReason | None

    median_delta: DecimalString | None
    ci_lower: DecimalString | None
    ci_upper: DecimalString | None

    floor_passed: bool
    floor_failures: list[FloorFailure] = field(default_factory=list)

    # Rate-count partition over scored traces (Phase 2.5b).
    improved_count: int = 0
    unchanged_count: int = 0
    regressed_count: int = 0

    # Policy-quality CI assessment (per V0_1_DECISION_RECORD §2 split).
    # Only meaningful when ci_computable=True; the deferred policy guard
    # populates False when CI width exceeds policy.max_ci_width. Defaults
    # True so v0.1 construction sites that don't yet wire the check don't
    # spuriously fail policy.
    ci_meaningful: bool = True

    def __post_init__(self) -> None:
        # Per cardinal #1, structural integrity violations propagate as
        # typed errors. The rate-count partition can't exceed the
        # number of scored traces — if a projection-layer bug populates
        # the wrong totals, the rate-based guards would silently emit
        # incorrect findings. `<=` (not `==`) is intentional: callers
        # may legitimately leave counts at 0 (the Phase 2.5b default for
        # backward compat with construction sites that pre-date the
        # rate-count fields). Phase 2.6+ projection should populate
        # exhaustively; this check catches the over-population bug.
        count_sum = self.improved_count + self.unchanged_count + self.regressed_count
        if count_sum > self.scored:
            raise InvariantViolationError(
                f"CohortResult({self.name!r}) rate-count partition exceeds scored: "
                f"improved={self.improved_count} + unchanged={self.unchanged_count} + "
                f"regressed={self.regressed_count} = {count_sum}, but scored={self.scored}. "
                "The rate partition is over scored traces; sum cannot exceed total. "
                "Likely a projection-layer bug."
            )
        if self.improved_count < 0 or self.unchanged_count < 0 or self.regressed_count < 0:
            raise InvariantViolationError(
                f"CohortResult({self.name!r}) rate counts must be non-negative: "
                f"improved={self.improved_count}, unchanged={self.unchanged_count}, "
                f"regressed={self.regressed_count}."
            )
        # ci_meaningful is a quality assessment of a CI that exists.
        # `ci_computable=False, ci_meaningful=False` is incoherent — there
        # is nothing to assess for meaningfulness. Default `ci_meaningful=True`
        # combined with `ci_computable=False` is a benign no-op (the guard
        # never reads ci_meaningful for non-computable cohorts), but
        # explicitly False against non-computable is a projection-layer bug.
        if not self.ci_computable and not self.ci_meaningful:
            raise InvariantViolationError(
                f"CohortResult({self.name!r}) ci_meaningful=False requires "
                "ci_computable=True. ci_meaningful is the quality assessment "
                "of a computed CI; if no CI exists, meaningfulness is "
                "undefined."
            )
