"""Decision-pipeline guards — Phase 2.5.

Guards are pure functions that read post-floor cohort results and the
decision policy, then emit structured `DecisionFinding`s describing
policy-level conclusions. The guard chain is the composition layer —
each guard contributes 0+ findings; the verdict computation in Phase
2.6 reads the resulting list and selects Ship / Don't Ship / Inconclusive.

## Discipline

- A guard never raises on precondition mismatch (e.g., required cohort
  missing) — it emits no finding and lets the floor or another guard
  catch the case. Structural integrity violations (e.g., a non-numeric
  `DecimalString` reaching `parse_decimal_string`) DO raise; per
  cardinal #1 those are bugs, not data.
- A guard only ever emits findings via `make_decision_finding`, never
  via `DecisionFinding(...)` directly — the registry-level severity is
  load-bearing per cardinal #2.
- Guards do NOT mutate inputs. `cohort_results` and `policy` are frozen
  dataclasses; the guard returns a **fresh** `list[DecisionFinding]`.
  The fresh-list contract is documented but not runtime-enforced —
  code review is the safety net for the class-level-mutable footgun.
- A guard reads ONLY data on the inputs it's passed. Reaching into
  global state (cache contents, environment) belongs upstream — the
  upstream computes the relevant fields and stuffs them into
  `CohortResult` or `DecisionPolicy` before the guard chain runs.
- `DecisionFinding.details` payloads are typed `Mapping[str, JsonPrimitive]`
  per cardinal rule #6 — never `dict[str, Any]`. When the
  `primary_endpoint` guard lands (Phase 2.6) and any guard is tempted
  to put structured data in `details`, that data must serialize to
  `JsonPrimitive` (str / int / float / bool / None). Nested structures
  belong as new typed fields on the finding or new finding codes, not
  as opaque dicts.

## Phase 2.5 lands the protocol + chain composer + two guards

This PR proves the pattern. Subsequent sub-phases add more guards:
- `baseline_regression` and `failure_improvement` need per-trace rate
  fields on `CohortResult` (improvement/regression counts) — extension
  is its own PR.
- `ci_availability` needs `ci_unavailable_for_required_cohort` in
  `FINDING_CODE_REGISTRY` — registry extension is its own PR.
- `cache_staleness` needs cache metadata from Phase 3.
- `primary_endpoint` (cardinal #10) needs the multiple-endpoint
  resolution logic from Phase 2.6 — lands with verdict computation.
"""

from whatifd.decision.guards.ci_availability import ci_availability_guard
from whatifd.decision.guards.improvement_observation import improvement_observation_guard
from whatifd.decision.guards.practical_delta import practical_delta_guard
from whatifd.decision.guards.primary_endpoint import primary_endpoint_guard
from whatifd.decision.guards.protocol import Guard, run_guards

__all__ = [
    "Guard",
    "ci_availability_guard",
    "improvement_observation_guard",
    "practical_delta_guard",
    "primary_endpoint_guard",
    "run_guards",
]
