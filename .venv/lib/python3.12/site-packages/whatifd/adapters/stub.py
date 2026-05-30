"""Synthetic stub adapter — Phase 4A.3.

Closes the Phase 4A gate. The stub satisfies `TraceSource` and
`Scorer` and is the harness target for Phase 4A.2's conformance
suite (and the driver for Phase 9A integration tests).

## Why a stub at all

Phase 9A drives the full pipeline (replay → score → decision →
render → CLI) end-to-end against a controlled adapter so we can
pin the architectural invariants — six walkthrough scenarios,
every `FAILURE_CODE_REGISTRY` entry, determinism byte-equality —
without depending on real Langfuse/Inspect AI fixtures. The stub
is fixture-driven: tests construct one with a list of canned
`RawTrace` and a scoring function, then run the pipeline. It is
NOT a mock or a faker — it implements the protocols faithfully,
just with deterministic inputs.

## Why under `whatifd.adapters` and not `tests/`

The stub is consumed by tests that live OUTSIDE this repo's test
tree (skill-benchmarks, future contributor reproductions). Putting
it under `whatifd.adapters` makes it importable from anywhere
`whatifd` is installed, while the lazy-load contract guarantees
that core never accidentally pulls it (`tests/unit/whatifd/adapters/
test_protocols.py::TestLazyLoad::test_core_modules_do_not_load_adapters`
includes the stub module in its scan).

## Cardinal alignment

- **#5 Sensitive[T]:** every text the stub emits goes through the
  `Sensitive[str]` constructor at the boundary. The harness pins
  this for every emitted trace and every score.
- **#1 failures-as-data:** the stub scorer accepts a `score_fn`
  that may return `None` to signal structural scoring failure;
  the pipeline converts that into `FailureRecord` per cardinal #1.
- **#10 statistical claims:** `StubTraceSource.cluster_key_support()`
  is parameterizable so integration tests can exercise both the
  "source provides clusters" and "source does not" branches of
  the methodology disclosure.

## What the stub does NOT do

- Reach over the network. The stub is in-memory; any test that
  expects HTTP-shaped error paths (timeouts, retries, 5xx) belongs
  in Phase 4B real-adapter tests.
- Hash judge prompts cryptographically. The stub returns
  pre-computed hex digests for `cache_key_components` so the cache
  subsystem's hex-digest invariants are exercised; the values are
  derived from input identifiers, not real prompt content.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from whatifd.adapters.protocols import (
    AdapterMetadata,
    JudgeResult,
    RawTrace,
    Scorer,
    TraceSource,
)
from whatifd.cache.keying.v1 import CacheKeyComponents
from whatifd.contract import ScoreCase
from whatifd.types.sensitive import Sensitive
from whatifd.types.statistical import ClusterKeySupport

STUB_ADAPTER_ID: Final[str] = "stub"
STUB_PACKAGE_VERSION: Final[str] = "0.1.0"


def _wrap(value: str, *, classification: str) -> Sensitive[str]:
    return Sensitive(value, classification=classification)


def _hash16(*parts: str) -> str:
    """Produce a 16-hex-char digest from the joined parts. Matches
    `CacheKeyComponents.__post_init__` invariant (≥16 lowercase hex)."""
    h = hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()
    return h[:16]


@dataclass(frozen=True, slots=True)
class StubTraceSpec:
    """Plain-text inputs used to construct a stub `RawTrace` row.

    Tests describe traces in plain strings; the stub wraps them in
    `Sensitive[str]` at construction. `cohort` defaults to
    `"failure"` so the most common scenario (failure-rescue) reads
    cleanly; pass `"baseline"` explicitly for baseline rows.
    `cluster_key` is `None` by default; pass a string to populate
    the per-trace cluster signal.
    """

    trace_id: str
    user_message: str
    original_response: str
    cohort: str = "failure"
    cluster_key: str | None = None
    skip_reason: str | None = None

    def to_raw_trace(self) -> RawTrace:
        """Project this spec into a `RawTrace`. Co-located with
        `StubTraceSpec` so the wrap discipline (cardinal #5) lives
        with the data shape; `StubTraceSource.iter_traces` and any
        future builder reuse this single path. As Phase 9A grows
        more builders, none re-implement the wrap step."""
        return RawTrace(
            trace_id=self.trace_id,
            cohort=self.cohort,
            user_message=_wrap(self.user_message, classification="user_content"),
            original_response=_wrap(self.original_response, classification="user_content"),
            cluster_key=self.cluster_key,
            skip_reason=self.skip_reason,
        )


@dataclass(frozen=True, slots=True)
class StubTraceSource:
    """Fixture-driven `TraceSource` for the harness and Phase 9A.

    Construct with a list of `StubTraceSpec` rows; `iter_traces()`
    yields one `RawTrace` per spec, in order. The generator semantics
    are real (it's a `def __iter__` returning a generator), so the
    harness's iterator-not-list assertion passes.

    `cluster_key_support` is parameterizable so a test can exercise
    both the "source provides clusters" and "source does not" report-
    methodology branches.
    """

    specs: list[StubTraceSpec]
    cluster_key_support_value: ClusterKeySupport = field(
        default_factory=lambda: ClusterKeySupport(available_keys=())
    )
    _metadata: AdapterMetadata = field(
        default_factory=lambda: AdapterMetadata(
            adapter_id=STUB_ADAPTER_ID, package_version=STUB_PACKAGE_VERSION
        ),
    )

    def iter_traces(self) -> Iterator[RawTrace]:
        for spec in self.specs:
            yield spec.to_raw_trace()

    def adapter_metadata(self) -> AdapterMetadata:
        return self._metadata

    def cluster_key_support(self) -> ClusterKeySupport:
        return self.cluster_key_support_value


# Default scoring function used by `StubScorer` when the caller
# doesn't supply one — emits 0.5 for every case so the pipeline
# can run without configuration. Not realistic; tests that need
# scenario-shaped scores pass an explicit `score_fn`.
def _default_score_fn(case: ScoreCase) -> float | None:
    return 0.5


@dataclass(frozen=True, slots=True)
class StubScorer:
    """Fixture-driven `Scorer`.

    `score_fn` is a callable `(ScoreCase) -> float | None`; returning
    `None` signals structural scoring failure (cardinal #1). The
    rationale is fixed at construction so tests can assert the
    `Sensitive[str]` wrap; pass `rationale_fn` for case-dependent
    text.
    """

    score_fn: Callable[[ScoreCase], float | None] = field(default=_default_score_fn)
    rationale_fn: Callable[[ScoreCase], str] = field(
        default=lambda case: f"stub rationale for {case.trace_id}"
    )
    judge_model_id: str = "stub-judge"
    judge_model_snapshot: str | None = None
    _metadata: AdapterMetadata = field(
        default_factory=lambda: AdapterMetadata(
            adapter_id=f"{STUB_ADAPTER_ID}-scorer",
            package_version=STUB_PACKAGE_VERSION,
        ),
    )

    def score(self, case: ScoreCase) -> JudgeResult:
        return JudgeResult(
            trace_id=case.trace_id,
            score=self.score_fn(case),
            rationale=_wrap(self.rationale_fn(case), classification="judge_rationale"),
            judge_model_id=self.judge_model_id,
            judge_model_snapshot=self.judge_model_snapshot,
        )

    def cache_key_components(self, case: ScoreCase) -> CacheKeyComponents:
        # Hex digests derived from input identifiers so distinct
        # ScoreCases produce distinct keys (the cache subsystem's
        # determinism invariant) without ever touching raw judge
        # prompts (cardinal #5).
        return CacheKeyComponents(
            whatif_schema_version="v0.1",
            whatif_scorer_adapter_version=STUB_PACKAGE_VERSION,
            scorer_type="stub",
            scorer_package_version=STUB_PACKAGE_VERSION,
            judge_provider="stub",
            judge_model_id=self.judge_model_id,
            judge_model_snapshot=self.judge_model_snapshot,
            rendered_prompt_hash=_hash16("prompt", case.trace_id),
            rubric_hash=_hash16("rubric", "v0.1"),
            scoring_parameters_hash=_hash16("params", self.judge_model_id),
            score_case_serialization_version="v1",
            score_case_hash=_hash16("case", case.trace_id, case.cohort),
        )

    def adapter_metadata(self) -> AdapterMetadata:
        return self._metadata


def make_default_stub_source(
    *,
    failures: Iterable[tuple[str, str, str]] = (),
    baselines: Iterable[tuple[str, str, str]] = (),
) -> StubTraceSource:
    """Convenience builder: take `(trace_id, user_message, original_response)`
    triples and build a `StubTraceSource` with the appropriate cohort
    labels. Used by Phase 9A integration tests that don't need the
    full `StubTraceSpec` surface.

    **Iteration order:** all `failures` rows are emitted first, then
    all `baselines` rows. `iter_traces` does NOT interleave. Phase 9A
    scenarios that depend on cohort interleaving (e.g., a stratified
    bootstrap test where the order matters) must construct
    `StubTraceSpec` rows directly and pass them to
    `StubTraceSource(specs=...)` in the desired order.

    **Limitation:** this builder does NOT accept `cluster_key` or
    `skip_reason`. Tests that need either field must construct
    `StubTraceSpec` rows directly and pass them to
    `StubTraceSource(specs=...)`. A `**kwargs` forwarding form was
    considered but rejected because the triple-based shape is the
    common case (90% of Phase 9A scenarios) and the explicit
    constructor path keeps the rare cluster-key / skip-reason cases
    legible at the call site."""
    specs: list[StubTraceSpec] = []
    for tid, um, orig in failures:
        specs.append(
            StubTraceSpec(trace_id=tid, user_message=um, original_response=orig, cohort="failure")
        )
    for tid, um, orig in baselines:
        specs.append(
            StubTraceSpec(trace_id=tid, user_message=um, original_response=orig, cohort="baseline")
        )
    return StubTraceSource(specs=specs)


# Static protocol witness: TYPE_CHECKING-only assignment so mypy
# verifies StubTraceSource and StubScorer satisfy the protocols
# without paying runtime construction cost on import. A signature
# drift in the stub (e.g., a Phase 4B refactor that adds a new
# protocol method) fails at type-check time. The runtime
# `runtime_checkable` `isinstance(...)` checks in the conformance
# harness are the second line of defense.
if TYPE_CHECKING:
    _trace_source_witness: TraceSource = StubTraceSource(specs=[])
    _scorer_witness: Scorer = StubScorer()


__all__ = [
    "STUB_ADAPTER_ID",
    "STUB_PACKAGE_VERSION",
    "StubScorer",
    "StubTraceSource",
    "StubTraceSpec",
    "make_default_stub_source",
]
