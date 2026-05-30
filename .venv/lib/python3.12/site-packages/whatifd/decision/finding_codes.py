"""Finding code registry — Phase 2.3.

The policy-conclusion half of the two-type rule (the operational half is
`failure_codes.py`):

- `FailureRecord` is what happened — emitted by adapters and stored in
  the report's `failures` list. Stage-bound, scope-bound, raw.
- `DecisionFinding` is what it means — emitted by guards (Phase 2.5) and
  aggregation (Phase 2.7), stored in the report's `findings` list.
  Severity-bound; the verdict layer reads severities to decide
  Ship / Don't Ship / Inconclusive.

`FINDING_CODE_REGISTRY` pairs each finding code with its `severity`, a
`message_template` (Phase 7 renderer reads this to format the user-
facing string), the required `details` keys, and a
`derived_from_failures_expectation` that the factory enforces:

- `"never"` — the finding stands alone (e.g.,
  `baseline_regression_above_threshold` is computed from cohort stats,
  no underlying operational failure).
- `"sometimes"` — either is valid (no constraint).
- `"always"` — the finding wraps one or more `FailureRecord` ids
  (e.g., `cohort_systemic_failure` rolls up trace-scope failures).

Severity intentionally cannot be overridden by the factory — it defines
verdict impact and changing it per-call would break the trust-floor
discipline. New severities (or remappings) require editing the registry
itself.

Phase 2.4 (fix-suggestion registry) keys off finding codes for
`blocks_ship` and `blocks_all` severities so every blocking finding
yields actionable next steps (cardinal #8). The cross-registry coverage
test lands with Phase 2.4 — Phase 2.3 only ensures shape and validation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from whatifd.types.finding import DecisionFinding, Severity
from whatifd.types.primitives import JsonPrimitive

DerivedFromFailuresExpectation = Literal["never", "sometimes", "always"]


@dataclass(frozen=True, slots=True)
class FindingCodeSpec:
    """One row in `FINDING_CODE_REGISTRY`.

    `message_template` is a Python format string with named fields that
    correspond 1:1 with `required_details`. The factory does NOT auto-
    render the message — Phase 7 (render) reads the template; the
    factory takes the caller-composed `message` so callers retain
    control of phrasing for stack traces and logs. The template's role
    here is documentation + eventual renderer source.

    `derived_from_failures_expectation` is enforced by the factory:
    `"never"` requires `derived_from_failures` to be empty; `"always"`
    requires it non-empty; `"sometimes"` is unconstrained.

    `description` is internal-only — adopters read this when adding a
    new code. User-facing text comes from Phase 2.4's fix-suggestion
    registry, not here.
    """

    severity: Severity
    message_template: str
    required_details: tuple[str, ...]
    derived_from_failures_expectation: DerivedFromFailuresExpectation
    description: str


_REGISTRY_BUILDER: dict[str, FindingCodeSpec] = {
    # ----- info severity (no verdict impact) ------------------------------
    "improvement_observed": FindingCodeSpec(
        severity="info",
        message_template=(
            "failure cohort median delta {median_delta} above practical-delta "
            "threshold {threshold} (improvement observed)"
        ),
        required_details=("median_delta", "threshold"),
        derived_from_failures_expectation="never",
        description=(
            "Failure cohort showed improvement above the practical-delta "
            "threshold. Information for the report; does not by itself "
            "drive the verdict. Carries the threshold so the finding is "
            "self-describing for the renderer."
        ),
    ),
    "cache_mode_inferred": FindingCodeSpec(
        severity="info",
        message_template=(
            "scorer cache mode resolved to {resolved_mode} from input {input_mode} "
            "based on environment signal {env_signal}"
        ),
        required_details=("input_mode", "resolved_mode", "env_signal"),
        derived_from_failures_expectation="never",
        description=(
            "User config left the cache mode at `auto` and the resolution "
            "logic inferred a concrete mode from the environment (e.g., "
            "CI=true → on). Emitted so CI runs disclose the mode they "
            "actually used; cardinal #1 (failures-as-data) extends to "
            "config-resolution decisions — the manifest should never "
            "imply the user picked a mode they didn't pick."
        ),
    ),
    # ----- blocks_ship severity (DontShip) --------------------------------
    "baseline_regression_above_threshold": FindingCodeSpec(
        severity="blocks_ship",
        message_template=("baseline regression rate {observed} exceeds threshold {threshold}"),
        required_details=("observed", "threshold"),
        derived_from_failures_expectation="never",
        description=(
            "Baseline cohort regressed beyond "
            "DecisionPolicy.max_baseline_regression_ratio. Computed from "
            "cohort stats; not derived from individual failures."
        ),
    ),
    "failure_improvement_below_threshold": FindingCodeSpec(
        severity="blocks_ship",
        message_template=("failure-cohort improvement rate {observed} below threshold {threshold}"),
        required_details=("observed", "threshold"),
        derived_from_failures_expectation="never",
        description=(
            "Failure cohort improvement is below "
            "DecisionPolicy.min_failure_improvement_ratio. The change "
            "does not credibly rescue failures."
        ),
    ),
    "practical_delta_below_threshold": FindingCodeSpec(
        severity="blocks_ship",
        message_template=(
            "median delta {median_delta} below practical-delta threshold {threshold}"
        ),
        required_details=("median_delta", "threshold"),
        derived_from_failures_expectation="never",
        description=(
            "Observed effect is within the practical-delta epsilon "
            "(likely noise). Cardinal rule #10 stance: small statistical "
            "wins inside the noise floor are not shippable."
        ),
    ),
    # ----- blocks_all severity (Inconclusive) -----------------------------
    "cache_corruption_detected": FindingCodeSpec(
        severity="blocks_all",
        message_template="cache corruption detected at {cache_path}; verdict cannot be trusted",
        required_details=("cache_path",),
        derived_from_failures_expectation="always",
        description=(
            "Run-scope failure forces Inconclusive. Pairs with "
            "FAILURE_CODE_REGISTRY['cache_corruption_detected']."
        ),
    ),
    "cache_lock_unavailable": FindingCodeSpec(
        severity="blocks_all",
        message_template="could not acquire scorer cache lock at {lock_path}",
        required_details=("lock_path",),
        derived_from_failures_expectation="always",
        description=(
            "Run-scope failure prevents scoring. Pairs with "
            "FAILURE_CODE_REGISTRY['cache_lock_unavailable']."
        ),
    ),
    "ci_unavailable_for_required_cohort": FindingCodeSpec(
        severity="blocks_all",
        message_template=("CI unavailable for required cohort {cohort}: {reason}"),
        required_details=("cohort", "reason"),
        derived_from_failures_expectation="always",
        description=(
            "Confidence interval could not be computed for a required "
            "cohort (sample too small, zero variance, computation failed). "
            "Pairs with FAILURE_CODE_REGISTRY['ci_uncomputable_for_required_cohort']. "
            "Forces Inconclusive — verdicts that depend on cohort-level "
            "uncertainty cannot be rendered without it. Per "
            "V0_1_DECISION_RECORD §6, v0.1 has no escape hatch; "
            "policy.max_ci_width is the lever for accepting wider CIs."
        ),
    ),
    "cohort_systemic_failure": FindingCodeSpec(
        severity="blocks_all",
        message_template=("{percent} of {cohort} cohort traces shared failure code {code}"),
        required_details=("cohort", "percent", "code"),
        derived_from_failures_expectation="always",
        description=(
            "Phase 2.7 aggregation emit: when ≥50% of a cohort's traces "
            "fail with the same code. Forces Inconclusive — the run is "
            "dominated by a systemic issue rather than measuring the "
            "change. "
            "Field contract: `percent` is a DecimalString ratio "
            "(e.g., '0.730' for 73%) per cardinal rule #4 — the renderer "
            "formats it as a percent string at display time. Callers pass "
            "the ratio in `details`; Phase 2.7 composes a human-readable "
            "`message` separately (the caller-composed `message` is "
            "authoritative; the template here is renderer documentation)."
        ),
    ),
}

FINDING_CODE_REGISTRY: Mapping[str, FindingCodeSpec] = MappingProxyType(_REGISTRY_BUILDER)
"""Frozen registry. Adding a code: append to `_REGISTRY_BUILDER` above. If
the new code's severity is `blocks_ship` or `blocks_all`, ensure Phase
2.4's `FIX_SUGGESTION_REGISTRY` covers it (the Phase 2.4 gate test
enforces coverage)."""


def make_decision_finding(
    code: str,
    *,
    message: str,
    derived_from_failures: Sequence[str] | None = None,
    details: Mapping[str, JsonPrimitive] | None = None,
) -> DecisionFinding:
    """Construct a `DecisionFinding`, pulling severity from the registry.

    Resolution rules (in order):
    1. `code` must be a key in `FINDING_CODE_REGISTRY` — else `ValueError`.
    2. `details` must include every key in `spec.required_details` — else
       `ValueError`. Extra keys allowed (extension-point per cardinal #6).
    3. `derived_from_failures_expectation` is enforced:
       - `"never"`: `derived_from_failures` must be empty (or None) — else `ValueError`.
       - `"always"`: `derived_from_failures` must be non-empty — else `ValueError`.
       - `"sometimes"`: no constraint.

    Severity is taken from the spec; not overrideable by callers. Changing
    severity per-call would break trust-floor discipline (cardinal #2:
    severity drives verdict). New severities require editing the registry.
    """
    spec = FINDING_CODE_REGISTRY.get(code)
    if spec is None:
        known = ", ".join(sorted(FINDING_CODE_REGISTRY))
        raise ValueError(
            f"unknown finding code {code!r}. Known codes: {known}. "
            "Add new codes to FINDING_CODE_REGISTRY in finding_codes.py."
        )

    resolved_details = dict(details) if details is not None else {}
    missing = [k for k in spec.required_details if k not in resolved_details]
    if missing:
        raise ValueError(
            f"finding code {code!r} requires details keys {list(spec.required_details)}; "
            f"missing: {missing}. See FINDING_CODE_REGISTRY[{code!r}].description."
        )

    resolved_failures = list(derived_from_failures) if derived_from_failures else []
    if spec.derived_from_failures_expectation == "never" and resolved_failures:
        raise ValueError(
            f"finding code {code!r} expects no derived_from_failures "
            f"(stands alone); got {resolved_failures}."
        )
    if spec.derived_from_failures_expectation == "always" and not resolved_failures:
        raise ValueError(
            f"finding code {code!r} expects derived_from_failures to be non-empty "
            "(it wraps one or more FailureRecords); got empty list."
        )

    return DecisionFinding(
        code=code,
        severity=spec.severity,
        message=message,
        derived_from_failures=resolved_failures,
        details=resolved_details,
    )
