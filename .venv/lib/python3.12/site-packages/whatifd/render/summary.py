"""`render_summary` — compact-form Markdown summary for a `ReportV01`.

Phase 7.2 of the v0.1 implementation plan. Produces a ≤30-line
Markdown block suitable for a PR comment, Slack post, or any
medium that wants more detail than the one-line CI status but
isn't the full report.

## Format

  # whatifd verdict: <Verdict>

  **<Reason line, 1-2 sentences.>**

  **Failures (N):**   improved A   unchanged B   regressed C   median Δ ±D
  **Baseline (N):**   improved A   unchanged B   regressed C   median Δ ±D

  Replay validity: X/Y traces. Cache: H hits, M misses.

  [Suggested next steps ↓](#fix) · [Replay details ↓](#replay-validity) · [Manifest →](manifest.json)

The blank lines and trailing jump-link bar are part of the
contract: PR-comment renderers (GitHub, GitLab) collapse adjacent
lines without paragraph breaks, so the spacing is load-bearing.

## Compact-Ship degenerate case

A clean Ship doesn't need the "Suggested next steps" jump link
(there are no findings to act on). The compact-Ship form omits
that link from the bar:

  [Replay details ↓](#replay-validity) · [Manifest →](manifest.json)

The total line count for clean Ship lands at ≤10; for a non-Ship
verdict with a long reason it can reach 15-20. The 30-line ceiling
gives headroom for future additions (e.g., methodology one-liner)
without breaking the budget contract.

## Forward-reference jump links

The links target anchors in the FULL report (Phase 7.1):

- `#fix` — the "Suggested next steps" section produced from
  `FIX_SUGGESTION_REGISTRY` lookups for blocking findings.
- `#replay-validity` — the "Replay validity" section in the full
  report.
- `manifest.json` — sibling artifact at the bundle write site.

Phase 7.1 owns the anchors. The summary section ships with the
forward-reference links so a consumer that splices summary +
full-report (Phase 8 CLI does this) gets working in-document
navigation.

## Cardinal alignment

- **#8 actionable Inconclusive:** the "Suggested next steps" jump
  link routes the reader to the registered fix-suggestion content.
  No Inconclusive verdict surfaces without the link.
- **#10 disclosure necessary:** the summary form points at the
  full report's Methodology block via the trailing jump-link bar
  (added in Phase 7.1 once the full-report anchors land). v0.1
  summary stops at the manifest pointer; the full report is the
  canonical methodology disclosure surface.
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
    VERDICT_LABEL as _VERDICT_LABEL,
)

if TYPE_CHECKING:
    from whatifd.report.models_v01 import ReportV01
    from whatifd.types.cohort import CohortResult
    from whatifd.types.finding import DecisionFinding

_MAX_LINES = 30


def render_summary(report: ReportV01) -> str:
    """Return the compact Markdown summary for `report`.

    Pure function over the typed wire shape. Output is ≤30 lines;
    a `ValueError` is raised if the rendered block exceeds the
    budget — that surfaces a renderer bug or a `ReportV01` shape
    that the budget can no longer accommodate (cardinal #1: fail
    loud rather than silently truncating).
    """
    lines: list[str] = []
    lines.append(f"# whatifd verdict: {_VERDICT_LABEL[report.verdict_state]}")
    lines.append("")

    reason = _reason_block(report)
    if reason:
        lines.extend(reason)
        lines.append("")

    stats = _stats_block(report.cohort_results)
    if stats:
        lines.extend(stats)
        lines.append("")

    validity = _replay_validity_line(report)
    if validity:
        lines.append(validity)
        lines.append("")

    lines.append(_jump_link_bar(report))

    rendered = "\n".join(lines).rstrip() + "\n"
    line_count = rendered.count("\n")
    if line_count > _MAX_LINES:
        raise ValueError(
            f"render_summary produced {line_count} lines; budget is "
            f"{_MAX_LINES}. Check whether a new section was added "
            "without trimming an existing one, or whether a finding "
            "message is unusually long."
        )
    return rendered


def _reason_block(report: ReportV01) -> list[str]:
    """1-2 sentence reason. For non-Ship verdicts uses the
    highest-severity finding's message wrapped in bold; for clean
    Ship returns a single all-passed sentence."""
    if report.verdict_state == "ship":
        return ["**All floor rules passed. All policy rules passed.**"]

    finding = _highest_severity_finding(report.decision_findings)
    if finding is not None:
        return [f"**{finding.message}**"]

    floor_rule = _top_floor_failure_summary(report.cohort_results)
    if floor_rule is not None:
        return [f"**{floor_rule}**"]

    # Defensive: matches the ci_status fallback wording for
    # consistency across formats.
    return ["**(no finding available — contract violation upstream)**"]


def _stats_block(cohort_results: list[CohortResult]) -> list[str]:
    """One bold line per known cohort plus generic lines for any
    extra cohorts. Mirrors the walkthrough format:
    `**Failures (N):**   improved A   unchanged B   regressed C   median Δ ±D`.
    """
    by_name = {c.name: c for c in cohort_results}
    out: list[str] = []
    if (failure := by_name.get(_COHORT_FAILURE)) is not None:
        out.append(_cohort_line("Failures", failure))
    if (baseline := by_name.get(_COHORT_BASELINE)) is not None:
        out.append(_cohort_line("Baseline", baseline))
    # Any other cohorts (v0.2+ shapes) appear with their literal
    # name. Stable order via the original cohort_results list.
    known = {_COHORT_FAILURE, _COHORT_BASELINE}
    for c in cohort_results:
        if c.name in known:
            continue
        out.append(_cohort_line(c.name, c))
    return out


def _cohort_line(label: str, c: CohortResult) -> str:
    delta = c.median_delta if c.median_delta is not None else "n/a"
    return (
        f"**{label} ({c.scored}):**   "
        f"improved {c.improved_count}   "
        f"unchanged {c.unchanged_count}   "
        f"regressed {c.regressed_count}   "
        f"median Δ {delta}"
    )


def _replay_validity_line(report: ReportV01) -> str | None:
    """Single line summarizing replay + cache stats. Returns None
    if no cohort_results (degenerate; nothing to summarize).

    The simple sum across cohorts assumes every cohort uses the
    same `selected/replayed/scored` semantics: each trace is
    counted once at each stage in exactly one cohort. v0.1
    failure-rescue and the planned v0.2 regression-check shapes
    both satisfy this — cohorts partition traces, they don't
    overlap. If a future shape introduces overlapping cohorts
    (e.g., the same trace appearing in both `regression_baseline`
    and `treatment`), this sum would double-count and the line
    needs reweighting.
    """
    if not report.cohort_results:
        return None
    total_replayed = sum(c.replayed for c in report.cohort_results)
    total_selected = sum(c.selected for c in report.cohort_results)
    cs = report.cache_summary
    return (
        f"Replay validity: {total_replayed}/{total_selected} traces. "
        f"Cache: {cs.hits} hits, {cs.misses} misses."
    )


def _jump_link_bar(report: ReportV01) -> str:
    """Trailing jump-link bar. Compact-Ship omits the
    'Suggested next steps' link because there are no actionable
    findings on a clean Ship."""
    links = []
    if report.verdict_state != "ship":
        links.append("[Suggested next steps ↓](#fix)")
    links.append("[Replay details ↓](#replay-validity)")
    links.append("[Manifest →](manifest.json)")
    return " · ".join(links)


def _highest_severity_finding(
    findings: list[DecisionFinding],
) -> DecisionFinding | None:
    """Severity rank is shared with `whatifd.render.ci_status` to
    keep the two formats aligned on which finding 'wins' when
    multiple are present (cross-format consistency)."""
    if not findings:
        return None
    return max(findings, key=lambda f: _SEVERITY_RANK[f.severity])


def _top_floor_failure_summary(cohort_results: list[CohortResult]) -> str | None:
    for cohort in cohort_results:
        if cohort.floor_failures:
            ff = cohort.floor_failures[0]
            return (
                f"{cohort.name} cohort below floor: "
                f"{ff.rule} requires {ff.threshold}, observed {ff.observed}"
            )
    return None


__all__ = ["render_summary"]
