"""`FailureRecord` — operational fact, one per event.

Cardinal rule #1: failure-as-data. Every expected failure (cache miss,
runner timeout, scorer error, schema mismatch) appears as a structured
`FailureRecord` in the JSON report. No silent crashes; no generic
"failed" buckets; unhandled exceptions are bugs in whatifd itself.

The two-type scope rule (the bright line):
- `scope="trace"` — adapter emits one per affected trace event.
- `scope="cohort"` — core emits one per cohort-level event after
  aggregation (e.g., "scorer API unavailable for 73% of failure cohort").
- `scope="run"` — core emits one per run-level event (e.g., "manifest
  signature missing").

Adapters never emit cohort-scope records — they don't have visibility.

`FailureRecord` carries no `verdict_impact` field. Verdict consequences
live on `DecisionFinding`. This keeps the operational layer pure:
`FailureRecord` is what happened, `DecisionFinding` is what it means.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from whatifd.types.primitives import JsonPrimitive

Stage = Literal[
    "ingest",
    "selection",
    "replay",
    "score",
    "diff",
    "decision",
    "report",
]

Scope = Literal["trace", "cohort", "run"]


@dataclass(frozen=True, slots=True)
class FailureRecord:
    """A single operational failure event.

    `id` is stable within a run (e.g., "failure_001") for cross-referencing
    from `DecisionFinding.derived_from_failures`. `code` is registered in
    `FAILURE_CODE_REGISTRY` (Phase 2); the registry pairs each code with
    its stage, default scope, and required `details` keys.

    Field constraints (enforced by validators in Phase 5 schema, not at
    construction time here — frozen dataclasses don't run validators):
    - `trace_id` is required when `scope == "trace"`, None otherwise.
    - `cohort` is required when `scope == "cohort"`, None otherwise.
    - `aggregated_into` is set when this trace-scope record was folded
      into a cohort-scope record by core's aggregation logic
      (`whatifd/decision/aggregation.py`, Phase 2).

    The `details` map is one of v0.1's three named extension points
    (per cardinal rule #6); keys may be added without a schema bump,
    typed values stable.
    """

    id: str
    code: str
    stage: Stage
    scope: Scope
    message: str
    trace_id: str | None
    cohort: str | None
    retryable: bool
    details: Mapping[str, JsonPrimitive] = field(default_factory=dict)
    aggregated_into: str | None = None
