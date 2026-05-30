"""`DecisionFinding` — policy conclusion, one per conclusion.

The two-type rule's other half (see `whatifd/types/failure.py`):
- `FailureRecord` is what happened (operational fact, layer-pure).
- `DecisionFinding` is what it means (policy conclusion, may reference
  failures).

A finding may or may not derive from `FailureRecord`s. Aggregate baseline
regression has no underlying operational failure (every trace replayed,
scored, the cache worked — the baseline just regressed). Cache-miss-driven
floor failure has many.

Severity vocabulary is shared across the codebase (no separate enum for
failures vs findings):
- `info` — informational only; no verdict impact.
- `degrades_trust` — accumulates against thresholds; may downgrade verdict.
- `blocks_ship` — prevents Ship verdict; produces Don't Ship.
- `blocks_all` — forces Inconclusive regardless of policy state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from whatifd.types.primitives import JsonPrimitive

Severity = Literal["info", "degrades_trust", "blocks_ship", "blocks_all"]


@dataclass(frozen=True, slots=True)
class DecisionFinding:
    """A single policy-level conclusion about the run.

    `code` is registered in `FINDING_CODE_REGISTRY` (Phase 2); the registry
    pairs each code with its severity, message template, required `details`
    keys, and a fix-suggestion entry (per cardinal rule #8 — Inconclusive
    must be actionable).

    `derived_from_failures` may be empty: not every finding traces back
    to a failure (e.g., `baseline_regression_above_threshold` is computed
    from cohort stats, not from a `FailureRecord`).

    `details` is one of v0.1's three named extension points (cardinal #6).
    """

    code: str
    severity: Severity
    message: str
    derived_from_failures: list[str] = field(default_factory=list)
    details: Mapping[str, JsonPrimitive] = field(default_factory=dict)
