"""`ReplaySuccess | ReplayFailure` — typed replay-stage result.

Phase 6.1 of the v0.1 implementation plan. The replay stage either
produces a `ReplayOutput` (the user runner returned cleanly) or a
typed failure (cache miss, runner timeout, runner exception). The
sealed union `ReplayResult` is what the pipeline (Phase 6.3) yields
per trace; downstream stages branch on type.

## Why a sealed union

Cardinal #1 (failures-as-data): expected replay failures are NOT
exceptions. The pipeline catches them at well-defined boundaries
and converts them into `ReplayFailure` instances that flow through
the same generator chain as successes. The only thing that escapes
as an exception is an unhandled bug in whatifd itself.

Cardinal #6 (typed boundaries): `ReplayResult = ReplaySuccess |
ReplayFailure` is a closed Python sealed-union (frozen dataclasses,
no inheritance hierarchy). Downstream code uses `match` /
`isinstance` to dispatch; mypy strict catches missing branches via
`assert_never`.

## Relationship to `FailureRecord`

`ReplayFailure` is the IN-PIPELINE shape — light, replay-stage
specific, carries just what the pipeline needs to make a routing
decision. `FailureRecord` (`whatifd.types.failure`) is the
report-level shape — has stage/scope/id assigned, fits in
`ReportV01.failures`. The pipeline projects `ReplayFailure` →
`FailureRecord` via `whatifd.decision.failure_codes.make_failure_record`
when assembling the report (Phase 2.7 aggregation / Phase 9
integration).

The intermediate exists because:
1. The pipeline doesn't know the stable `id` ("failure_001") to
   assign — that's done at aggregation when the full failure list
   is sorted.
2. The pipeline doesn't carry registry knowledge (stage, default
   scope, required-details rules) — that's `make_failure_record`'s
   job.
3. Construction-time validation here would force every Phase 6 unit
   test to import the registry, defeating the point of a small
   typed result.

The validation we DO enforce at construction:
- `code` is one of the replay-stage codes in `FAILURE_CODE_REGISTRY`.
  This is a cheap registry lookup and catches typos at the call
  site, not at report-assembly time when the trace context is gone.

## Cardinal alignment

- **#1 failures-as-data:** `ReplayFailure` is the structured
  representation; nothing in the replay pipeline raises a
  whatifd-internal exception for an expected condition.
- **#6 typed boundaries:** the union is a closed Python type;
  callers `match` on it. No `dict[str, Any]` carries replay state.
- **#9 orchestration not compute:** these are tiny dataclasses, not
  computational state. The replay pipeline orchestrates user code
  (the runner) and adapter code (the scorer); the result types
  exist purely to route between them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from whatifd.decision.failure_codes import FAILURE_CODE_REGISTRY
from whatifd.types.primitives import JsonPrimitive

if TYPE_CHECKING:
    # `ReplayOutput` is the public Pydantic boundary type from
    # `whatifd.contract`. Type-only here to keep `whatifd.replay`
    # importable without pulling Pydantic at module-load — the actual
    # value flows through at runtime, where Pydantic is already
    # loaded by whoever produced the ReplayOutput. Same lazy-import
    # discipline as `whatifd.serialization.encoder` for `ReportV01`.
    from whatifd.contract import ReplayOutput


@dataclass(frozen=True, slots=True)
class ReplaySuccess:
    """The user runner returned cleanly for this trace.

    `output` is the Pydantic `ReplayOutput` produced by the runner —
    we hand it on to the scorer adapter unchanged. The pipeline does
    not inspect `output.text`; that's the scorer's job.
    """

    trace_id: str
    cohort: str
    output: ReplayOutput


@dataclass(frozen=True, slots=True)
class ReplayFailure:
    """A typed replay-stage failure.

    `code` MUST appear in `FAILURE_CODE_REGISTRY` with `stage="replay"`.
    The constructor validates this so a typo at the call site fails
    immediately, not at report-assembly time when the trace context
    is gone. `details` is the typed extension-point map (cardinal
    #6) — required-details enforcement happens at projection to
    `FailureRecord` via `make_failure_record`.

    The pipeline catches three known failure conditions:
    - `tool_cache_miss` (raised from `whatifd.replay.tool_cache`,
      Phase 6.2)
    - `runner_timeout` (raised by the pipeline's timeout wrapper,
      Phase 6.3)
    - `runner_exception` (any non-system exception escaping the
      runner; Phase 6.3 catches and converts)

    Additional replay-stage codes can be added to the registry; the
    constructor accepts any whose `stage == "replay"`.
    """

    trace_id: str
    cohort: str
    code: str
    message: str
    details: Mapping[str, JsonPrimitive] = field(default_factory=dict)

    def __post_init__(self) -> None:
        spec = FAILURE_CODE_REGISTRY.get(self.code)
        if spec is None:
            raise ValueError(
                f"ReplayFailure code {self.code!r} is not in "
                "FAILURE_CODE_REGISTRY. Cardinal #1: failure codes are "
                "registered in `whatifd/decision/failure_codes.py` so the "
                "report's failure list is closed-set, not free-form. Add "
                "the code to the registry or use an existing one."
            )
        if spec.stage != "replay":
            raise ValueError(
                f"ReplayFailure code {self.code!r} is registered with "
                f"stage={spec.stage!r}, not 'replay'. The replay pipeline "
                "only emits replay-stage codes; other stages emit their "
                "own typed failures."
            )


ReplayResult = ReplaySuccess | ReplayFailure
"""Sealed union over replay-stage outcomes. Downstream pipeline code
matches on the variant; `mypy --strict` + `typing.assert_never` in
the default branch catches missed cases at type-check time.
"""
