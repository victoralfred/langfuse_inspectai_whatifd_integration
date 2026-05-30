"""Fix suggestion registry — Phase 2.4, cardinal rule #8.

Cardinal rule #8: Inconclusive must be actionable. Every blocking
finding code (`blocks_ship` or `blocks_all`) in `FINDING_CODE_REGISTRY`
MUST have a corresponding entry in `FIX_SUGGESTION_REGISTRY` so the
renderer can produce concrete next steps the user can take.

Coverage is enforced by two test invariants:
- Positive: every blocking finding code is a key in this registry.
- Negative: no `info`-severity finding code is a key (info codes
  describe observations; they need no fix).

The negative invariant follows PR #17's review feedback: the absence of
an info code in this registry is not just convention — it's structural.
A test that asserts "info codes never appear here" catches drift if a
future contributor mistakenly adds one.

Adding new fix suggestions: when a new blocking code lands in
`FINDING_CODE_REGISTRY`, the coverage test fails until a matching entry
appears here. The test is the gate; the registry is the surface.

Phase 7 (render) reads `FixSuggestion.steps` to produce the "Suggested
next steps" section seen in the walkthroughs (e.g., scenario 5 cache-
lock-unavailable). v0.1 keeps `steps` as Markdown-formatted strings; a
structured `FixStep` type with explicit command/title/body fields is a
deferred refinement.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class FixSuggestion:
    """One row in `FIX_SUGGESTION_REGISTRY`.

    `finding_code` is the canonical key — duplicates the dict key for
    structural redundancy (a misnamed key would be caught by the
    `finding_code == key` test invariant).

    `summary` is a one-line headline used by the renderer for the section
    title (e.g., "Could not acquire scorer cache lock"). Caller-facing,
    keep it imperative and concrete.

    `steps` are Markdown-formatted strings, ordered. Phase 7 renders them
    as a numbered list. Inline `code` fences and `whatifd <subcommand>`
    references are encouraged; the renderer treats them as Markdown.

    `description` is internal-only.
    """

    finding_code: str
    summary: str
    steps: tuple[str, ...]
    description: str


_REGISTRY_BUILDER: dict[str, FixSuggestion] = {
    # ----- blocks_ship (DontShip) ----------------------------------------
    "baseline_regression_above_threshold": FixSuggestion(
        finding_code="baseline_regression_above_threshold",
        summary="Baseline cohort regressed beyond the policy threshold.",
        steps=(
            "Identify which baseline traces regressed and look for a common pattern (prompt change, "
            "tool behavior change, model output drift).",
            "Run `whatifd diff <previous-report.json> <this-report.json>` to compare against a known-good run.",
            "If the failure cohort improvement is large enough to justify the baseline regression, consider "
            "adjusting `DecisionPolicy.max_baseline_regression_ratio` — but only with a documented rationale.",
            "Otherwise, do not ship. Iterate on the proposed change to reduce baseline impact.",
        ),
        description=(
            "Aggregate baseline regression — no underlying operational failure. Iteration target."
        ),
    ),
    "failure_improvement_below_threshold": FixSuggestion(
        finding_code="failure_improvement_below_threshold",
        summary="Failure cohort did not improve enough to justify shipping.",
        steps=(
            "Examine the failure-cohort traces individually. Are the failures the proposed change targets "
            "actually represented in the cohort?",
            "Check the cohort selection: a poorly-targeted failure cohort dilutes the apparent improvement.",
            "If the change is correct but partial, document the partial-rescue intent and either lower "
            "`DecisionPolicy.min_failure_improvement_ratio` (with rationale) or iterate.",
        ),
        description="Insufficient rescue rate. Either selection or change scope is the issue.",
    ),
    "practical_delta_below_threshold": FixSuggestion(
        finding_code="practical_delta_below_threshold",
        summary="Observed effect is within the practical-delta noise floor.",
        steps=(
            "The change shows a statistically observable but practically negligible improvement. "
            "Cardinal #10: small wins inside the noise floor are not shippable.",
            "Iterate on the change to produce a larger effect, OR",
            "Lower `DecisionPolicy.practical_delta_epsilon` ONLY with a calibration set documenting why "
            "the smaller threshold is meaningful for this judge/metric pair.",
        ),
        description=(
            "Effect smaller than the practical-delta epsilon. Cardinal #10 enforcement point."
        ),
    ),
    # ----- blocks_all (Inconclusive) -------------------------------------
    "cache_corruption_detected": FixSuggestion(
        finding_code="cache_corruption_detected",
        summary="Scorer cache contains entries that fail checksum validation.",
        steps=(
            "Run `whatifd cache verify` to enumerate corrupt entries.",
            "Run `whatifd cache rebuild --force` to rebuild from scratch (slower but safe).",
            "Investigate the root cause — concurrent writes from multiple whatifd processes against the "
            "same cache, partial writes from a killed process, or storage-layer corruption — before "
            "shipping a verdict from this cache again.",
        ),
        description=(
            "Cache integrity broken. Verdict cannot be trusted until cache is rebuilt and root cause "
            "addressed."
        ),
    ),
    "cache_lock_unavailable": FixSuggestion(
        finding_code="cache_lock_unavailable",
        summary="Could not acquire the scorer cache lock.",
        steps=(
            "If you know the previous run is no longer running: `whatifd cache rebuild --force` rebuilds "
            "from scratch (slower next run, but safe).",
            "If you want to clear just the lock without rebuilding: `whatifd cache unlock`. Use only if "
            "you are certain no other whatifd process is using this cache.",
            "If you suspect file corruption: `whatifd cache verify` reports any entries with checksum "
            "mismatches and optionally repairs.",
        ),
        description=(
            "Lock contention or orphaned lock. The CLI subcommands let the user pick the recovery "
            "appropriate to their situation."
        ),
    ),
    "ci_unavailable_for_required_cohort": FixSuggestion(
        finding_code="ci_unavailable_for_required_cohort",
        summary="Confidence interval could not be computed for a required cohort.",
        steps=(
            "Check the linked failure record (see `derived_from_failures` in the finding) for the "
            "specific reason: `sample_too_small` means fewer scored traces than the bootstrap requires; "
            "`zero_variance` means every paired delta was identical; `computation_failed` means the "
            "bootstrap raised an unexpected error.",
            "If the reason is sample size: increase the cohort selection limit in `whatifd.config.yaml` "
            "so more traces are evaluated, or relax the `selection.<cohort>.filter` if it's too narrow.",
            "Per V0_1_DECISION_RECORD §6 there is no escape-hatch flag for accepting verdict-without-CI "
            "in v0.1 — the run will produce Inconclusive. Persistent acceptance mechanisms are deferred "
            "to v1.0 as a coherent unit. To accept WIDER (but computable) CIs, raise `policy.max_ci_width` "
            "or set it to None — that lever operates on the policy side via the deferred `ci_meaningful` "
            "guard (cascade-tracked) and does not bypass structural CI-uncomputable cases.",
            "If the reason is `zero_variance` or `computation_failed`: investigate the scored traces "
            "directly. Identical deltas on every trace suggest a scoring bug; computation failures point "
            "at the bootstrap implementation.",
        ),
        description=(
            "CI unavailable on a required cohort. Cardinal #10 stance: verdicts that depend on cohort-"
            "level uncertainty cannot ship without it. Per V0_1_DECISION_RECORD §6 there is no v0.1 "
            "escape hatch — `policy.max_ci_width` is the lever for accepting wider but computable CIs."
        ),
    ),
    "cohort_systemic_failure": FixSuggestion(
        finding_code="cohort_systemic_failure",
        summary="A single failure mode dominates the cohort — the run is not measuring the change.",
        steps=(
            "Inspect the linked failures (see `derived_from_failures` in the finding) to identify the "
            "common code and shared `details`.",
            "Address the underlying issue — common causes: tool-cache misses (tracer not capturing "
            "tool outputs), schema mismatches (trace schema drift), runner timeouts (timeout too low or "
            "trace genuinely needs more time).",
            "Either fix the root cause and re-record traces, OR expand cohort selection so the systemic "
            "failures are a smaller fraction.",
            "Rerun whatifd. Cardinal #2: the floor is about evidence existence, not evidence quality. "
            "A cohort dominated by one failure mode does not provide the evidence needed for a verdict.",
        ),
        description=(
            "Phase 2.7 aggregation emit. The fix is operational (fix the failure mode) before "
            "policy (rerun whatifd)."
        ),
    ),
}

FIX_SUGGESTION_REGISTRY: Mapping[str, FixSuggestion] = MappingProxyType(_REGISTRY_BUILDER)
"""Frozen registry. Coverage is gated by `tests/unit/whatifd/decision/
test_fix_suggestions.py::TestCrossRegistryCoverage` — adding a new
blocking finding code without a matching entry here fails CI."""
