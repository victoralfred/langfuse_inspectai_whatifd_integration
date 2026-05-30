"""`render_ci_status` — one-line CI status string for a `ReportV01`.

Phase 7.3 of the v0.1 implementation plan. Produces a single line
of ≤80 visible characters suitable for a CI-check title, GitHub
status row, or Slack notification:

  ✓ whatifd: Ship — failures 14/20 ↑, baseline 17/20 stable
  ✗ whatifd: Don't Ship — baseline regressed 6/20 (median Δ -0.18)
  ⚠ whatifd: Inconclusive — baseline cohort below floor (3 < 5 min_scored…)

The format is `<glyph> whatifd: <Verdict> — <reason>` where:

- `<glyph>` is `✓` (Ship) / `✗` (Don't Ship) / `⚠` (Inconclusive).
- `<Verdict>` is the human label, NOT the wire `verdict_state`.
- `<reason>` is verdict-derived:
  - **Ship:** improved/total summary across required cohorts.
  - **Don't Ship / Inconclusive:** the highest-severity decision
    finding's message, or — if no findings — the top floor-failure
    rule. The decision pipeline guarantees that a non-Ship verdict
    has at least one finding or floor failure (cardinal #2 + #8),
    so we always have a reason; the test fixtures pin this.

## Length budget

Visible-character length ≤80. The reason is truncated with `…` if
the full string would exceed the budget. The glyph + prefix
(`✓ whatifd: Ship — `) is 17-25 characters depending on verdict,
leaving 55-63 chars for the reason on most calls.

NOTE: visible-character count is `len(string)` since the format is
ASCII + the three single-codepoint glyphs in `_GLYPH` (`✓` U+2713,
`✗` U+2717, `⚠` U+26A0). We do NOT use Unicode east-asian-width
measurement; the glyphs are narrow, the ASCII text contributes one
column per char, and budget-conscious renderers (GitHub status,
terminal status bars) treat string length and visible width
identically for this content. The pin
`TestGlyphCodePointStability::test_glyphs_are_single_code_point`
fails if a future contributor adds a multi-codepoint glyph (e.g.,
an emoji built from regional-indicator pairs), forcing them to
re-evaluate this width claim.

## Cardinal alignment

- **#8 actionable Inconclusive:** the reason for an Inconclusive
  verdict surfaces a registered finding or floor rule; it is NOT a
  generic "experiment failed" string. Operators reading the CI
  status get enough to know what to fix without opening the full
  report.
- **#2 floor cannot be bypassed:** if the floor failed, the
  verdict is Inconclusive and the reason cites the floor rule.
- **#10 disclosure necessary:** the CI status is the COMPACT form;
  methodology disclosure lives in the full report. A consumer who
  trusts only the CI status sees the verdict, not the methodology
  caveats — this is by design (the compact format can't carry the
  full disclosure), and the full report is the canonical artifact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from whatifd.render._constants import (
    COHORT_BASELINE as _COHORT_BASELINE,
)
from whatifd.render._constants import (
    COHORT_FAILURE as _COHORT_FAILURE,
)
from whatifd.render._constants import (
    SEVERITY_RANK as _SEVERITY_RANK,
)
from whatifd.render._constants import (
    VERDICT_LABEL as _LABEL,
)

if TYPE_CHECKING:
    from whatifd.report.models_v01 import ReportV01
    from whatifd.types.cohort import CohortResult
    from whatifd.types.finding import DecisionFinding

# Per-format budgets per the plan. The 80-char ceiling is
# load-bearing for GitHub status-row truncation; tighter
# downstream consumers can re-truncate.
_MAX_LINE_CHARS = 80

_GLYPH = {
    "ship": "✓",
    "dont_ship": "✗",
    "inconclusive": "⚠",
}

# Glyph, label, cohort-name, and severity-rank constants are
# imported from `whatifd.render._constants` (single source of truth
# across all Phase 7 renderers). `_LABEL` is the alias bound at
# the import block above.


def render_ci_status(report: ReportV01) -> str:
    """Return the one-line CI status string for `report`.

    Pure function over the typed wire shape. Never raises for a
    valid `ReportV01`; an empty cohort_results / failures /
    decision_findings list is handled (clean Ship has no findings;
    a non-Ship verdict has at least one per the decision-pipeline
    contract).
    """
    glyph = _GLYPH[report.verdict_state]
    label = _LABEL[report.verdict_state]
    reason = _reason_for(report)
    full = f"{glyph} whatifd: {label} — {reason}"

    if len(full) <= _MAX_LINE_CHARS:
        return full

    # Truncate the reason; keep glyph + prefix intact so the
    # verdict is always legible.
    prefix = f"{glyph} whatifd: {label} — "
    budget = _MAX_LINE_CHARS - len(prefix) - 1  # 1 char for the ellipsis
    truncated_reason = reason[:budget].rstrip() + "…"
    return prefix + truncated_reason


def _reason_for(report: ReportV01) -> str:
    """Verdict-specific reason text. Always a non-empty string."""
    if report.verdict_state == "ship":
        return _ship_reason(report.cohort_results)

    # Non-Ship: the highest-severity decision finding's message, or
    # if none, the top floor-failure rule across cohort_results. The
    # decision pipeline guarantees at least one of these is present
    # for non-Ship verdicts.
    finding = _highest_severity_finding(report.decision_findings)
    if finding is not None:
        return finding.message

    floor_rule = _top_floor_failure_rule(report.cohort_results)
    if floor_rule is not None:
        return floor_rule

    # Defensive fallback: a non-Ship verdict with no findings AND
    # no floor failures shouldn't happen per the decision-pipeline
    # contract, but the renderer is a leaf — we surface the
    # contract violation as a recognizable string rather than
    # raising, so the operator still sees a status row.
    return "(no finding available — contract violation upstream)"


def _ship_reason(cohort_results: list[CohortResult]) -> str:
    """Compose the clean-Ship summary from cohort stats.

    Format: `failures X/Y ↑, baseline Z/Y stable` for the standard
    failure-rescue shape. If a cohort named `failure` or `baseline`
    is missing, falls back to a per-cohort summary.
    """
    by_name = {c.name: c for c in cohort_results}
    failure = by_name.get(_COHORT_FAILURE)
    baseline = by_name.get(_COHORT_BASELINE)
    if failure is not None and baseline is not None:
        return (
            f"failures {failure.improved_count}/{failure.scored} ↑, "
            f"baseline {baseline.scored - baseline.regressed_count}/"
            f"{baseline.scored} stable"
        )

    # Generic fallback: list each cohort's improved/scored.
    parts = [f"{c.name} {c.improved_count}/{c.scored} ↑" for c in cohort_results]
    return ", ".join(parts) if parts else "no cohorts"


def _highest_severity_finding(
    findings: list[DecisionFinding],
) -> DecisionFinding | None:
    """Pick the finding with the highest severity; ties broken by
    list order (stable, deterministic).

    Uses strict subscript `_SEVERITY_RANK[f.severity]` (NOT
    `.get(..., 0)`): `Severity` is a closed Literal, and a value
    outside the Literal arriving here is schema drift — most likely
    a Phase v0.2 severity added to the type without updating this
    rank table. Surfacing the drift as `KeyError` here is
    preferable to silently demoting the new severity below `info`,
    which would produce a wrong CI status (the new severity might
    semantically be the highest).
    """
    if not findings:
        return None
    return max(findings, key=lambda f: _SEVERITY_RANK[f.severity])


def _top_floor_failure_rule(cohort_results: list[CohortResult]) -> str | None:
    """First floor failure across cohort_results, formatted as
    `<rule> (<observed> < <threshold> ...)`. Returns None if no
    cohort has a floor failure.
    """
    for cohort in cohort_results:
        if cohort.floor_failures:
            ff = cohort.floor_failures[0]
            return f"{cohort.name} cohort below floor ({ff.observed} < {ff.threshold} {ff.rule})"
    return None
