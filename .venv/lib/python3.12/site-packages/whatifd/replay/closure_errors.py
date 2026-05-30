"""Typed exceptions raised by `whatifd.cli_pipeline.build_delta_fn`'s
closure and consumed by `whatifd.pipeline.run_pipeline`'s exception
projection.

These live here (`whatifd.replay`) — NOT in `whatifd.cli_pipeline` —
because the CORE pipeline (`whatifd.pipeline`) reads them via
`isinstance` to project structured fields into `FailureRecord.details`.
Putting them in the CLI-layer module would create a core → CLI
import inversion: every non-CLI caller of `run_pipeline` (e.g.,
programmatic consumers, integration tests) would transitively load
the CLI pipeline module on first trace failure.

The shared module is at the replay-result layer because:
- `_ReplayStageError` projects a kernel `ReplayFailure` into the
  closure surface; replay-result is its natural home.
- `_ScorerStructuralError` fits less neatly but co-locating with
  the replay error keeps pipeline.py's import path single-source.

Cardinal #1: failure classification is type-level (`isinstance`),
NOT convention-based (`getattr` on attribute names).
"""

from __future__ import annotations


class _ReplayStageError(Exception):
    """Internal: replay-stage failure raised inside the cli_pipeline
    `delta_fn` closure to signal `run_pipeline`'s exception capture.

    Carries the kernel's `ReplayFailure.code` as a structured
    attribute (NOT only baked into the message). The pipeline reads
    `exc.replay_code` after `isinstance` narrowing so consumers
    walking the report graph see a typed code, not a parsed string.
    The pipeline currently converts this to a `scorer_unavailable`
    `FailureRecord` at the top-level `code` field (v0.1 scope);
    Phase 11+ may widen to per-stage codes.
    """

    def __init__(self, *, replay_code: str, message: str) -> None:
        super().__init__(message)
        self.replay_code = replay_code


class _ScorerStructuralError(Exception):
    """Internal: `JudgeResult.score is None` (cardinal-#1) raised
    so `run_pipeline`'s exception path captures it as a structured
    `FailureRecord`. Carries the rationale's `classification` as
    a typed attribute so downstream consumers attribute the
    failure without parsing the message string."""

    def __init__(self, *, rationale_classification: str, message: str) -> None:
        super().__init__(message)
        self.rationale_classification = rationale_classification


__all__ = ["_ReplayStageError", "_ScorerStructuralError"]
