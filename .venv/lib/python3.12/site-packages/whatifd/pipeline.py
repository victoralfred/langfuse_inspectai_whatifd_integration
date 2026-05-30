"""Programmatic integration entry point — Phase 9A.1.

`run_pipeline(trace_source, *, delta_fn, floor, policy, runtime,
methodology, cache_summary) -> ReportV01` stitches the adapter →
cohort aggregation → verdict → projection path end-to-end.

## Phase 9A.1 scope and shortcuts

This sub-phase establishes the architectural plumbing. Two
deliberate shortcuts that follow-up sub-phases close:

- **Per-trace deltas come from a caller-supplied `delta_fn`**, not
  from running a real `Scorer` over (original, replayed) pairs.
  The stub's faithfulness is fixture-driven and the integration
  test controls the delta function. Phase 9A.2+ wires real paired
  scoring through the stub's `Scorer` once a Runner is available
  in scope.
- **CI bounds use empirical 5th/95th percentiles of the deltas**,
  not stratified bootstrap. Adequate for cardinal-#2 floor-passing
  Ship/Don't-Ship verdicts in tests; Phase 9A.3 (determinism) +
  the broader stats layer replace this with proper bootstrap.

Both shortcuts are documented at the call sites; the function
signature is the stable contract.

## What this function does NOT do

- Read the cache. The cache subsystem is exercised by
  `whatifd.cache` unit tests; integration tests pass a pre-built
  `CacheSummary`.
- Build the manifest. Manifest construction (timestamps, env
  fingerprint, sensitive-unwrap drain) is the CLI's responsibility;
  integration tests pass a pre-built `RunManifest`.
- Run a real two-affirmation. Tests construct fixtures directly;
  the CLI surface enforces cardinal #7.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from whatifd.adapters.protocols import RawTrace, TraceSource
from whatifd.cache.summary import CacheSummary
from whatifd.decision.failure_codes import make_failure_record
from whatifd.decision.floor import compute_cohort_floor_failures
from whatifd.decision.verdict import compute_verdict
from whatifd.replay.closure_errors import _ReplayStageError, _ScorerStructuralError
from whatifd.report.models_v01 import ReportV01
from whatifd.report.projection import project_to_report_v01
from whatifd.statistical import (
    BOOTSTRAP_CI_LEVEL,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    paired_percentile_bootstrap,
    to_decimal_string,
)
from whatifd.types.cohort import CIUnavailableReason, CohortResult
from whatifd.types.failure import FailureRecord
from whatifd.types.manifest import RunManifest
from whatifd.types.policy import DecisionPolicy, TrustFloor
from whatifd.types.primitives import DecimalString
from whatifd.types.statistical import MethodologyDisclosure

# Phase E.2 statistical-layer constants (BOOTSTRAP_SEED,
# BOOTSTRAP_RESAMPLES, BOOTSTRAP_CI_LEVEL) live in
# `whatifd.statistical` and are imported above. The canonical
# source of truth is that module — both `whatifd.pipeline` and
# `whatifd.cli` import directly from it. There are no aliases or
# re-exports here.


@dataclass(frozen=True, slots=True)
class _CohortBuckets:
    """Per-cohort accumulator. Internal."""

    name: str
    selected: int
    deltas: tuple[float, ...]


# Phase 9A.1 emits `scorer_unavailable` when `delta_fn` raises:
# delta_fn IS the scoring step in the 9A.1 shortcut (real paired
# scoring through the stub's Scorer is Phase 9A.2+ work that needs
# a Runner in scope), so the registered `score`-stage code with
# transient-error semantics is the right fit. Phase 9A.4 routes
# this through `make_failure_record` so the registry is the single
# source of truth — no more `delta_fn_raised` literal that bypassed
# the registry validation.
_PIPELINE_SCORER_FAILURE_CODE = "scorer_unavailable"


def run_pipeline(
    trace_source: TraceSource,
    *,
    delta_fn: Callable[[RawTrace], float],
    floor: TrustFloor,
    policy: DecisionPolicy,
    runtime: RunManifest,
    methodology: MethodologyDisclosure,
    cache_summary: CacheSummary,
) -> ReportV01:
    """Drive the pipeline end-to-end and return a `ReportV01`.

    The entry point is adapter-agnostic: any concrete `TraceSource`
    that satisfies the Phase 4A.1 protocol works. v0.1 callers pass
    the synthetic stub from `whatifd.adapters.stub`; Phase 9B will
    pass real Langfuse / Inspect AI adapters through the same
    function.
    """
    raw_traces = list(trace_source.iter_traces())
    buckets, delta_failures = _bucket_by_cohort(raw_traces, delta_fn=delta_fn)
    cohort_results = tuple(
        _cohort_result_from_bucket(b, policy=policy, floor=floor) for b in buckets
    )
    # Phase C: experiment_shape comes from the manifest (Phase A added
    # it to RunManifest). compute_verdict branches its guard sequence
    # and required-cohorts list on the shape — failure_rescue uses the
    # full v0.1 guard chain; regression_check skips the failure-cohort
    # guards and requires only the baseline cohort at the floor.
    verdict = compute_verdict(
        cohort_results,
        floor,
        policy,
        experiment_shape=runtime.experiment_shape,
    )
    return project_to_report_v01(
        verdict,
        failures=delta_failures,
        cache_summary=cache_summary,
        methodology=methodology,
        runtime=runtime,
    )


def _bucket_by_cohort(
    traces: Iterable[RawTrace],
    *,
    delta_fn: Callable[[RawTrace], float],
) -> tuple[tuple[_CohortBuckets, ...], list[FailureRecord]]:
    """Group traces by `cohort` field and compute the per-trace
    delta for each. Skipped traces (`skip_reason is not None`) are
    counted toward `selected` but excluded from `deltas`.

    `delta_fn` exceptions are captured as `FailureRecord`s
    (cardinal #1 — failure-as-data, not pipeline crash). The
    affected trace contributes to `selected` but not to `deltas`,
    matching the skip-reason path. Phase 9A.4 extends this pattern
    to cover the full `FAILURE_CODE_REGISTRY`.
    """
    selected_counts: dict[str, int] = {}
    deltas_by_cohort: dict[str, list[float]] = {}
    failures: list[FailureRecord] = []
    for rt in traces:
        selected_counts[rt.cohort] = selected_counts.get(rt.cohort, 0) + 1
        bucket_deltas = deltas_by_cohort.setdefault(rt.cohort, [])
        if rt.skip_reason is not None:
            continue
        try:
            bucket_deltas.append(delta_fn(rt))
        except Exception as exc:  # boundary catch; structured into FailureRecord per cardinal #1
            # Construct via `make_failure_record` so the registry
            # validates the code, supplies stage/scope defaults, and
            # enforces the required-details contract. `provider` and
            # `reason` are the registered required keys for
            # `scorer_unavailable`; `exc_type` and the optional
            # structured projections below are extension keys (extra
            # keys allowed per cardinal #6 extension point).
            details: dict[str, str] = {
                # TODO(Phase 4B): replace the hardcoded "stub" with a
                # real provider identifier sourced from the scorer
                # adapter's `adapter_metadata().adapter_id`. Forensic
                # reports under the real adapter MUST attribute scorer
                # failures to the actual provider (e.g.,
                # "inspect_ai", "anthropic") so the audit trail is
                # accurate. The 9A.1 shortcut hardcodes "stub" because
                # the pipeline doesn't yet receive the scorer in this
                # scope; that wires in 9A.2+ / Phase 4B.
                "provider": "stub",
                "reason": str(exc),
                "exc_type": type(exc).__name__,
            }
            # Phase 10.3 cardinal-#1 widening: project the typed
            # exception attributes raised by the cli_pipeline
            # closure into `details` so consumers read structured
            # fields, not parsed strings. `isinstance` narrowing
            # (NOT raw `getattr` duck-typing) so a third-party
            # exception happening to carry an attribute by the same
            # name is not silently promoted — failure classification
            # is type-level per cardinal #1.
            #
            # `_ReplayStageError` / `_ScorerStructuralError` live in
            # `whatifd.replay.closure_errors` (a neutral non-CLI
            # module) so this isinstance branch doesn't import
            # CLI-layer code from core. Import is at the file's
            # top level (not inside this handler) so an import
            # failure surfaces at module load — not silently per
            # exception.
            if isinstance(exc, _ReplayStageError):
                details["replay_code"] = exc.replay_code
            elif isinstance(exc, _ScorerStructuralError):
                details["rationale_classification"] = exc.rationale_classification
            failures.append(
                make_failure_record(
                    _PIPELINE_SCORER_FAILURE_CODE,
                    id=f"delta-fn-{rt.trace_id}",
                    message=f"delta_fn raised: {exc}",
                    trace_id=rt.trace_id,
                    details=details,
                )
            )
    buckets = tuple(
        _CohortBuckets(
            name=name,
            selected=selected_counts[name],
            deltas=tuple(deltas_by_cohort.get(name, [])),
        )
        for name in sorted(selected_counts)
    )
    return buckets, failures


def _cohort_result_from_bucket(
    bucket: _CohortBuckets,
    *,
    policy: DecisionPolicy,
    floor: TrustFloor,
) -> CohortResult:
    """Build a `CohortResult` from a per-cohort bucket. Counts
    improved / unchanged / regressed per `practical_delta_epsilon`;
    median + percentile CI from the deltas; floor failures from
    `compute_cohort_floor_failures`. Constructs the result exactly
    once — floor evaluation uses a lightweight `_FloorProbe`
    namespace so we don't materialize a discarded `CohortResult`
    just to compute the failure list."""
    eps = policy.practical_delta_epsilon
    improved = sum(1 for d in bucket.deltas if d > eps)
    regressed = sum(1 for d in bucket.deltas if d < -eps)
    unchanged = len(bucket.deltas) - improved - regressed
    scored = len(bucket.deltas)
    replayed = scored  # 9A.1 shortcut: stub treats every selected trace as replayed

    median: DecimalString | None = None
    ci_lower: DecimalString | None = None
    ci_upper: DecimalString | None = None
    ci_computable = False
    ci_unavailable_reason: CIUnavailableReason | None = "sample_too_small"
    if scored >= floor.min_scored_per_required_cohort:
        # Phase E.2: paired-percentile bootstrap (Phase E.1's algorithm
        # in `whatifd.statistical`) replaces the v0.1 empirical-quantile
        # shortcut. The bootstrap respects the per-trace delta as the
        # paired unit of analysis (cardinal #10) and produces a real
        # CI rather than a degenerate single-point empirical estimate.
        bootstrap_result = paired_percentile_bootstrap(
            bucket.deltas,
            resamples=BOOTSTRAP_RESAMPLES,
            ci_level=BOOTSTRAP_CI_LEVEL,
            seed=BOOTSTRAP_SEED,
        )
        median = to_decimal_string(bootstrap_result.median)
        ci_lower = to_decimal_string(bootstrap_result.ci_lower)
        ci_upper = to_decimal_string(bootstrap_result.ci_upper)
        ci_computable = True
        ci_unavailable_reason = None

    # Build a probe with just the fields `compute_cohort_floor_failures`
    # reads (selected, replayed, scored, replay-validity ratio is
    # derived from selected+replayed). A lightweight CohortResult is
    # the cleanest probe: typed, frozen, and the floor function's
    # signature already takes one. No discarded second instance —
    # this is the same object pattern used elsewhere in the codebase
    # for floor-then-finalize flows.
    probe = CohortResult(
        name=bucket.name,
        selected=bucket.selected,
        replayed=replayed,
        scored=scored,
        ci_computable=ci_computable,
        ci_unavailable_reason=ci_unavailable_reason,
        median_delta=median,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        floor_passed=True,  # provisional; corrected below
        floor_failures=[],
        improved_count=improved,
        unchanged_count=unchanged,
        regressed_count=regressed,
    )
    floor_failures = compute_cohort_floor_failures(probe, floor)
    if not floor_failures:
        # Probe already satisfies the floor; return as-is to avoid
        # an unnecessary copy.
        return probe
    # Floor failed — replace with the corrected `floor_passed` /
    # `floor_failures` state. Cardinal #2: this is the structural
    # signal compute_verdict consumes.
    return CohortResult(
        name=probe.name,
        selected=probe.selected,
        replayed=probe.replayed,
        scored=probe.scored,
        ci_computable=probe.ci_computable,
        ci_unavailable_reason=probe.ci_unavailable_reason,
        median_delta=probe.median_delta,
        ci_lower=probe.ci_lower,
        ci_upper=probe.ci_upper,
        floor_passed=False,
        floor_failures=floor_failures,
        improved_count=probe.improved_count,
        unchanged_count=probe.unchanged_count,
        regressed_count=probe.regressed_count,
        ci_meaningful=probe.ci_meaningful,
    )


__all__ = ["run_pipeline"]
