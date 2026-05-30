"""Failure code registry — Phase 2.2, cardinal rule #1.

Cardinal rule #1 (failure-as-data) says every expected failure surfaces as
a structured `FailureRecord` in the report. The registry is the source of
truth for "what counts as an expected failure":

- Each entry pairs a code with its `stage`, `default_scope`, the required
  `details` keys (so the projection layer in Phase 5 can validate that
  every emitted record carries enough context to be actionable), a default
  `retryable` value, and a human-readable description.
- The registry is frozen at module import (`MappingProxyType`); no code
  path mutates it after definition.

`make_failure_record` is the canonical factory. It pulls defaults from the
registry and validates programmer-contract invariants (unknown code,
missing required details, scope/trace_id/cohort mismatch). These are
programmer errors, not runtime data failures — `ValueError` is the right
shape per the cardinal #1 doctrine ("expected failures are data;
contract violations are bugs in whatifd itself").

Phase 9 integration tests (per phases.md) inject every code in this
registry and verify each produces a structured `FailureRecord`, never an
unhandled exception. New codes added in v0.2+ go here; the test sweep
catches any code lacking a fix-suggestion entry (Phase 2.4).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from whatifd.types.failure import FailureRecord, Scope, Stage
from whatifd.types.primitives import JsonPrimitive


@dataclass(frozen=True, slots=True)
class FailureCodeSpec:
    """One row in `FAILURE_CODE_REGISTRY`.

    `stage` is sealed by the `Stage` literal (ingest/selection/replay/
    score/diff/decision/report). `default_scope` is the scope the factory
    uses when the caller does not override it; aggregation in Phase 2.7
    may emit a cohort-scope record for a code whose default is trace.

    `required_details` is the tuple of keys that MUST be present in the
    `FailureRecord.details` mapping. The factory raises if any are
    missing. Order is stable (tuple, not set) because schema rendering in
    Phase 7 lists the keys in declaration order.

    `description` is internal-only — adopters read this in code review
    when adding a new code. Renderer text comes from the fix-suggestion
    registry (Phase 2.4), not here.
    """

    stage: Stage
    default_scope: Scope
    required_details: tuple[str, ...]
    retryable_default: bool
    description: str


_REGISTRY_BUILDER: dict[str, FailureCodeSpec] = {
    # ----- ingest stage ---------------------------------------------------
    "trace_schema_mismatch": FailureCodeSpec(
        stage="ingest",
        default_scope="trace",
        required_details=("missing_field",),
        retryable_default=False,
        description=(
            "Trace format does not match the adapter's expected schema. "
            "Often emitted when a trace was recorded against an older agent "
            "version than the current one."
        ),
    ),
    "trace_invalid": FailureCodeSpec(
        stage="ingest",
        default_scope="trace",
        required_details=("reason",),
        retryable_default=False,
        description=(
            "Trace failed structural validation (e.g., empty messages, "
            "malformed tool calls, missing required fields)."
        ),
    ),
    # ----- replay stage ---------------------------------------------------
    "tool_cache_miss": FailureCodeSpec(
        stage="replay",
        default_scope="trace",
        required_details=("tool_name",),
        retryable_default=False,
        description=(
            "A tool call in the trace has no matching entry in the recorded "
            "tool cache. Usually means the tracer was not capturing tool "
            "outputs at trace time."
        ),
    ),
    "runner_timeout": FailureCodeSpec(
        stage="replay",
        default_scope="trace",
        required_details=("timeout_seconds",),
        retryable_default=False,
        description=(
            "The runner exceeded the configured timeout for this trace. "
            "Same trace replayed again deterministically times out, so this "
            "is not retryable in-run."
        ),
    ),
    "runner_exception": FailureCodeSpec(
        stage="replay",
        default_scope="trace",
        required_details=("exception_type", "message"),
        retryable_default=False,
        description=(
            "The runner raised an unhandled exception during replay. "
            "Indicates a bug in the runner or an unrecognized trace shape."
        ),
    ),
    # ----- score stage ----------------------------------------------------
    "scorer_unavailable": FailureCodeSpec(
        stage="score",
        default_scope="trace",
        required_details=("provider", "reason"),
        retryable_default=True,
        description=(
            "The scorer's underlying provider was unreachable or returned a "
            "transient error. Retried by the runner; persistent failures "
            "surface as `scorer_invalid_output` after backoff exhaustion."
        ),
    ),
    "scorer_invalid_output": FailureCodeSpec(
        stage="score",
        default_scope="trace",
        required_details=("provider",),
        retryable_default=False,
        description=(
            "The scorer returned a value outside the expected range or "
            "schema. Bug in the scorer prompt, the judge model output, or "
            "the parser between them."
        ),
    ),
    # ----- decision stage -------------------------------------------------
    "ci_uncomputable_for_required_cohort": FailureCodeSpec(
        stage="decision",
        default_scope="cohort",
        required_details=("cohort", "reason"),
        retryable_default=False,
        description=(
            "Bootstrap confidence interval could not be computed for a "
            "required cohort (e.g., zero variance, sample too small after "
            "score-stage failures)."
        ),
    ),
    # ----- cache subsystem (replay-stage, run-scope) ----------------------
    "cache_lock_unavailable": FailureCodeSpec(
        stage="replay",
        default_scope="run",
        required_details=("lock_path",),
        retryable_default=False,
        description=(
            "Could not acquire the scorer cache lock. Another whatifd "
            "process may hold it, or a previous run terminated abnormally "
            "and left the lock orphaned. See `whatifd cache unlock`."
        ),
    ),
    "cache_corruption_detected": FailureCodeSpec(
        stage="replay",
        default_scope="run",
        required_details=("cache_path",),
        retryable_default=False,
        description=(
            "One or more cache entries failed checksum validation. The "
            "cache is in an inconsistent state and cannot be trusted for a "
            "Ship verdict. See `whatifd cache rebuild --force`."
        ),
    ),
}

FAILURE_CODE_REGISTRY: Mapping[str, FailureCodeSpec] = MappingProxyType(_REGISTRY_BUILDER)
"""Frozen registry. Adding a code: append to `_REGISTRY_BUILDER` above and
ensure Phase 2.4's `FIX_SUGGESTION_REGISTRY` covers the new code (the
Phase 2 gate test enforces coverage)."""


def make_failure_record(
    code: str,
    *,
    id: str,
    message: str,
    trace_id: str | None = None,
    cohort: str | None = None,
    details: Mapping[str, JsonPrimitive] | None = None,
    scope: Scope | None = None,
    retryable: bool | None = None,
    aggregated_into: str | None = None,
) -> FailureRecord:
    """Construct a `FailureRecord`, pulling defaults from the registry.

    Resolution rules (in order):
    1. `code` must be a key in `FAILURE_CODE_REGISTRY` — else `ValueError`.
    2. `details` must include every key in `spec.required_details` — else
       `ValueError`. Extra keys are allowed (extension-point per cardinal #6).
    3. `scope` defaults to `spec.default_scope` when None.
    4. `retryable` defaults to `spec.retryable_default` when None.
    5. Scope/identifier consistency:
       - `scope == "trace"` requires `trace_id`, forbids `cohort`.
       - `scope == "cohort"` requires `cohort`, forbids `trace_id`.
       - `scope == "run"` forbids both.
       Mismatches raise `ValueError`.

    The factory is the only path that constructs `FailureRecord`s in
    production code paths; direct construction remains possible (frozen
    dataclasses cannot have `__init_subclass__` enforcement) but is
    flagged in code review and absent from the documented API surface.
    """
    spec = FAILURE_CODE_REGISTRY.get(code)
    if spec is None:
        known = ", ".join(sorted(FAILURE_CODE_REGISTRY))
        raise ValueError(
            f"unknown failure code {code!r}. Known codes: {known}. "
            "Add new codes to FAILURE_CODE_REGISTRY in failure_codes.py."
        )

    resolved_details = dict(details) if details is not None else {}
    missing = [k for k in spec.required_details if k not in resolved_details]
    if missing:
        raise ValueError(
            f"failure code {code!r} requires details keys {list(spec.required_details)}; "
            f"missing: {missing}. See FAILURE_CODE_REGISTRY[{code!r}].description."
        )

    resolved_scope: Scope = scope if scope is not None else spec.default_scope
    resolved_retryable = retryable if retryable is not None else spec.retryable_default

    if resolved_scope == "trace":
        if trace_id is None:
            raise ValueError(f"scope='trace' requires trace_id (code={code!r})")
        if cohort is not None:
            raise ValueError(f"scope='trace' forbids cohort (code={code!r})")
    elif resolved_scope == "cohort":
        if cohort is None:
            raise ValueError(f"scope='cohort' requires cohort (code={code!r})")
        if trace_id is not None:
            raise ValueError(f"scope='cohort' forbids trace_id (code={code!r})")
    else:  # run
        if trace_id is not None:
            raise ValueError(f"scope='run' forbids trace_id (code={code!r})")
        if cohort is not None:
            raise ValueError(f"scope='run' forbids cohort (code={code!r})")

    return FailureRecord(
        id=id,
        code=code,
        stage=spec.stage,
        scope=resolved_scope,
        message=message,
        trace_id=trace_id,
        cohort=cohort,
        retryable=resolved_retryable,
        details=resolved_details,
        aggregated_into=aggregated_into,
    )
