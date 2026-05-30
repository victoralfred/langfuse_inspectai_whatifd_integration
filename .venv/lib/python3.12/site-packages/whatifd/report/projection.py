"""Projection: internal types → `ReportV01` wire format.

Phase 5.2 of the v0.1 implementation plan; pairs with Phase 5.1
(`models_v01.py` — the wire types) and the upcoming serialization
sub-phases (5.3 encoder, 5.4 graph walk, 5.5 schema gen).

## Cardinal #2 contract — verdict input must be the sealed union

`project_to_report_v01` takes a `Verdict` (the internal sealed union
`Ship | DontShip | Inconclusive`) as input — NOT a bare
`verdict_state: str`, NOT the components of a Ship. This is structural,
not stylistic:

- The only way to obtain a `Ship` instance is through `compute_verdict`
  / `evaluate_floor`, which produce and consume `FloorPassedProof`.
- The witness-token closure-capture in `whatifd/decision/floor.py`
  prevents external construction of `FloorPassedProof`, and `Ship`
  requires one.
- Therefore: if the caller has a `Verdict` to pass in, the floor was
  verified at the appropriate point upstream.

A `project_to_report_v01(verdict_state: str, ...)` shape would re-open
the cardinal #2 bypass — a caller could synthesize `verdict_state="ship"`
without ever touching the witness machinery. The `Verdict` input is the
type-level chokepoint that closes that bypass.

## What this module flattens

The internal `Verdict` carries verdict-specific data on each variant
(Ship has `findings`; DontShip has `findings + blocking_findings`;
Inconclusive has those plus `floor_failures`). The wire format is
flat: a single `decision_findings` list and the `verdict_state`
literal. Projection takes a Verdict in, takes other typed inputs
(failures, cache_summary, methodology, runtime), and returns a
fully-populated `ReportV01`.

## What this module does NOT do

- **Bootstrap CI / stats.** Cohort-level CI is computed upstream; the
  `cohort_results` carrying CI live on the Verdict already.
- **Methodology assembly.** The caller builds `MethodologyDisclosure`
  from configured policy + observed run state and passes it in.
- **Sensitive-data redaction.** Phase 5.4's
  `assert_no_unredacted_sensitive` is the structural defense before
  the `WhatifJSONEncoder` (5.3) sees the report. Projection is just
  field-copying; it does not unwrap `Sensitive[T]`.
- **Schema generation.** Phase 5.5 produces the JSON Schema artifact
  from the `ReportV01` type signature; projection consumes the type,
  not the schema.

## Cardinal alignment

- **#1 (failures-as-data):** `failures` is required, accepts an empty
  sequence (clean run). `decision_findings` is derived from the
  Verdict's typed findings list, not parsed from strings.
- **#2 (floor cannot be bypassed):** `Verdict` input is the
  type-level enforcement — see preamble.
- **#6 (typed boundaries):** all inputs are typed; the function
  returns a `ReportV01` with no `dict[str, Any]` boundary anywhere.
- **#10 (statistical claims match design):** `methodology:
  MethodologyDisclosure` is required.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import assert_never

from whatifd.cache.summary import CacheSummary
from whatifd.report.models_v01 import (
    REPORT_SCHEMA_URI,
    REPORT_SCHEMA_VERSION,
    ReportV01,
    VerdictState,
)
from whatifd.types.cohort import CohortResult
from whatifd.types.failure import FailureRecord
from whatifd.types.finding import DecisionFinding
from whatifd.types.manifest import RunManifest
from whatifd.types.statistical import MethodologyDisclosure
from whatifd.types.verdict import DontShip, Inconclusive, Ship, Verdict


def project_to_report_v01(
    verdict: Verdict,
    *,
    failures: Sequence[FailureRecord],
    cache_summary: CacheSummary,
    methodology: MethodologyDisclosure,
    runtime: RunManifest,
) -> ReportV01:
    """Flatten internal types into the v0.1 wire-format `ReportV01`.

    The single load-bearing constraint is the `verdict: Verdict`
    parameter — see module docstring for the cardinal #2 rationale.
    Once that's satisfied, the rest is field-copying.

    Inputs:

    - `verdict`: the resolved internal `Verdict`. Carries
      `cohort_results` and `findings` on each variant; projection
      reads them and flattens into the wire-format siblings.
    - `failures`: run-level operational failures. Empty on clean
      runs; non-empty when (e.g.) trace ingestion or scoring
      surfaced typed `FailureRecord` data per cardinal #1.
    - `cache_summary`: required `CacheSummary` from Phase 3.5.
    - `methodology`: required `MethodologyDisclosure` per cardinal
      #10. The caller builds this from policy + observed run state.
    - `runtime`: the `RunManifest` carrying timestamps, config hash,
      trust floor, decision policy, environment fingerprint, and
      sensitive-unwrap audit log. Stamped into `ReportV01.runtime`
      (the only non-deterministic field) and additionally provides
      `trust_floor` / `decision_policy` for the report's top-level
      typed copies.

    `trust_floor` and `decision_policy` are READ from `runtime` rather
    than passed separately because the manifest is the canonical
    source — passing them as parallel arguments would invite drift
    between manifest content and report top-level fields. If a future
    schema bump separates manifest policy from "policy applied to this
    verdict" (e.g., for partial-run resumption), that's a v0.2 change
    with its own projection signature.
    """
    verdict_state, cohort_results, decision_findings = _flatten_verdict(verdict)
    return ReportV01(
        schema_version=REPORT_SCHEMA_VERSION,
        schema_uri=REPORT_SCHEMA_URI,
        experiment_shape=runtime.experiment_shape,
        verdict_state=verdict_state,
        cohort_results=cohort_results,
        failures=list(failures),
        decision_findings=decision_findings,
        cache_summary=cache_summary,
        trust_floor=runtime.trust_floor,
        decision_policy=runtime.decision_policy,
        methodology=methodology,
        runtime=runtime,
    )


def _flatten_verdict(
    verdict: Verdict,
) -> tuple[VerdictState, list[CohortResult], list[DecisionFinding]]:
    """Map a sealed `Verdict` to its wire-format flat triple.

    Uses `match` + `assert_never` for type-system-enforced
    exhaustiveness — adding a new `Verdict` variant in v1.0 (e.g.,
    `ConditionallyShip`) without a `case` here is a mypy-strict
    failure, not a silent bug. Matches the dispatch pattern used by
    `whatifd/decision/guards/primary_endpoint.py`.

    The wire format takes only `findings` (not `blocking_findings`)
    because blocking findings are a derived view — `blocking_findings`
    is `findings` filtered by severity, and downstream consumers
    (renderers, dashboards) compute the filter themselves from the
    severity-tagged `decision_findings` list. Including a parallel
    `blocking_findings` field on the wire would invite drift between
    the two.

    **Inconclusive.floor_failures handling (v0.1 design choice):** the
    internal `Inconclusive` carries `floor_failures: list[FloorFailure]`
    aggregating run-level floor failures (including run-scope
    structural failures like "required cohort missing" that have no
    per-cohort home). v0.1 `ReportV01` has no top-level `floor_failures`
    field — per-cohort failures flow through `cohort_results[].floor_failures`
    (preserved), but run-level aggregates are dropped on the wire.
    For most run shapes this is information-preserving (the failures
    appear under their cohort); for the missing-cohort case the
    information is lost from the wire format. Cascade-tracked under
    "Run-level FloorFailure projection" for v0.2 schema decision.
    Tests pin the current behavior so the drop is intentional, not
    accidental.
    """
    # The three arms are structurally parallel today (all variants
    # carry `cohort_results` and `findings` with identical wire
    # treatment) — the dispatch is solely about the verdict-state
    # literal. They are kept separate rather than collapsed into a
    # state-only match because:
    #   - explicit per-variant returns let mypy strict verify each arm
    #     of the sealed union against the typed return shape, catching
    #     drift if a future variant gains/loses the read fields;
    #   - a v1.0 `ConditionallyShip` variant could legitimately diverge
    #     (e.g., extra `acceptance_record: AcceptanceRecord` field
    #     surfaced via projection); a state-only match would silently
    #     drop that field, while explicit arms force the contributor
    #     to handle it.
    # If the variants stay parallel through v0.2+, refactor then.
    match verdict:
        case Ship():
            return ("ship", list(verdict.cohort_results), list(verdict.findings))
        case DontShip():
            return ("dont_ship", list(verdict.cohort_results), list(verdict.findings))
        case Inconclusive():
            return (
                "inconclusive",
                list(verdict.cohort_results),
                list(verdict.findings),
            )
        case _ as never:
            assert_never(never)
