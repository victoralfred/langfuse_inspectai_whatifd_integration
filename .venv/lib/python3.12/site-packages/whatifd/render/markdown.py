"""`render_full_report` — full Markdown report for a `ReportV01`.

Phase 7.1 of the v0.1 implementation plan. The canonical artifact:
the Markdown document `whatifd fork` writes alongside the JSON
report, and the reference document the summary's forward-reference
jump links resolve into.

## Sections

The report is composed of bounded section helpers, each producing
a list of lines:

1. **Verdict header** — `# whatifd verdict: <Verdict>`.
2. **Reason** — bold one-line summary; clean Ship gets all-passed,
   non-Ship surfaces the highest-severity finding's message.
3. **Stats** — per-cohort breakdown with median delta + CI.
4. **Replay validity** — `<a id="replay-validity">` anchor; per-
   cohort selected/replayed/scored counts.
5. **Floor evaluation** — only rendered when at least one floor
   failure is present (clean Ship omits the table for compactness).
6. **Suggested next steps** — `<a id="fix">` anchor; v0.1 7.1a
   ships the anchor + a verdict-specific placeholder. Phase 7.1b
   wires `FIX_SUGGESTION_REGISTRY` templates per blocking finding.
7. **Methodology** — cardinal #10 disclosure pulled from
   `report.methodology`. Surfaces the five reliability concepts
   (reproducibility / reliability / validity / calibration / bias)
   so the reader sees what was and wasn't measured.
8. **Manifest pointer** — `[Manifest →](manifest.json)`.

The summary's forward-reference links (`#fix`, `#replay-validity`)
resolve to the anchors here when `whatifd fork` writes summary +
full-report to the same Markdown file.

## Phase 7.1 split

- **7.1a** ✅ — section skeleton + anchors + methodology block.
- **7.1b** ✅ — `FIX_SUGGESTION_REGISTRY` templates wired into the
  "Suggested next steps" section. Each blocking finding renders as
  `### <summary>` + numbered steps. Findings sorted by severity
  rank (highest first); unregistered codes hit a defensive
  fallback (the cardinal-#8 coverage test in
  `tests/unit/whatifd/decision/` pins this is unreachable for
  registered codes).
- **7.1c** outstanding — walkthrough-match tests for all six
  `docs/walkthroughs/*.md` scenarios (Phase 7 gate).

## Cardinal alignment

- **#8 actionable Inconclusive:** the `<a id="fix">` anchor exists
  for non-Ship verdicts; 7.1b wires the registry templates so the
  Inconclusive verdict is structurally actionable.
- **#10 disclosure necessary:** the Methodology section renders
  every required disclosure field, including the five reliability
  concepts marked "not measured" by default — never silently
  omitted (the disclosure-vs-silence test is the load-bearing pin
  for cardinal #10).
- **#2 floor cannot be bypassed:** the Floor evaluation table
  appears whenever any cohort failed a floor rule, regardless of
  verdict. The verdict-state-vs-floor logic is upstream; this
  renderer surfaces the floor record faithfully.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from whatifd.decision.fix_suggestions import FIX_SUGGESTION_REGISTRY
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
    from whatifd.types.cohort import CohortResult, FloorFailure
    from whatifd.types.finding import DecisionFinding
    from whatifd.types.statistical import MethodologyDisclosure


def render_full_report(report: ReportV01) -> str:
    """Return the full Markdown report for `report`.

    Pure function over the typed wire shape. Output is a complete
    Markdown document terminated with a trailing newline.
    """
    sections: list[list[str]] = [
        _verdict_header(report),
        _reason_paragraph(report),
        _stats_section(report.cohort_results),
        _replay_validity_section(report.cohort_results),
    ]

    floor_section = _floor_evaluation_section(report.cohort_results)
    if floor_section:
        sections.append(floor_section)

    sections.append(_suggested_next_steps_section(report))
    sections.append(_methodology_section(report.methodology))
    sections.append(_manifest_pointer())

    # Join sections with blank-line separators; strip trailing
    # whitespace; ensure exactly one trailing newline.
    body = "\n\n".join("\n".join(s) for s in sections)
    return body.rstrip() + "\n"


# ---------------------------------------------------------------------------
# Section helpers
# ---------------------------------------------------------------------------


def _verdict_header(report: ReportV01) -> list[str]:
    return [f"# whatifd verdict: {_VERDICT_LABEL[report.verdict_state]}"]


def _reason_paragraph(report: ReportV01) -> list[str]:
    if report.verdict_state == "ship":
        return ["**All floor rules passed. All policy rules passed.**"]

    finding = _highest_severity_finding(report.decision_findings)
    if finding is not None:
        return [f"**{finding.message}**"]

    floor_rule = _top_floor_failure_summary(report.cohort_results)
    if floor_rule is not None:
        return [f"**{floor_rule}**"]

    return ["**(no finding available — contract violation upstream)**"]


def _stats_section(cohort_results: list[CohortResult]) -> list[str]:
    """`## Stats` followed by per-cohort lines with median Δ + CI."""
    lines = ["## Stats", ""]
    if not cohort_results:
        lines.append("(No cohorts reported.)")
        return lines

    by_name = {c.name: c for c in cohort_results}
    if (failure := by_name.get(_COHORT_FAILURE)) is not None:
        lines.append(_cohort_stats_line("Failures", failure))
    if (baseline := by_name.get(_COHORT_BASELINE)) is not None:
        lines.append(_cohort_stats_line("Baseline", baseline))
    known = {_COHORT_FAILURE, _COHORT_BASELINE}
    for c in cohort_results:
        if c.name in known:
            continue
        lines.append(_cohort_stats_line(c.name, c))
    return lines


def _cohort_stats_line(label: str, c: CohortResult) -> str:
    delta = c.median_delta if c.median_delta is not None else "n/a"
    ci = _ci_string(c)
    return (
        f"**{label} ({c.scored}):**   "
        f"improved {c.improved_count}   "
        f"unchanged {c.unchanged_count}   "
        f"regressed {c.regressed_count}   "
        f"median Δ {delta}   {ci}"
    )


def _ci_string(c: CohortResult) -> str:
    """Format the cohort's CI bounds, or a not-computed fallback."""
    if c.ci_lower is not None and c.ci_upper is not None:
        return f"CI [{c.ci_lower}, {c.ci_upper}]"
    if c.ci_unavailable_reason is not None:
        return f"(CI not computed: {c.ci_unavailable_reason})"
    return "(CI not computed)"


def _replay_validity_section(cohort_results: list[CohortResult]) -> list[str]:
    """The summary's `#replay-validity` jump-link target. Anchor +
    per-cohort selected/replayed/scored counts.
    """
    lines = [
        '<a id="replay-validity"></a>',
        "## Replay validity",
        "",
    ]
    if not cohort_results:
        lines.append("(No cohorts reported.)")
        return lines

    for c in cohort_results:
        replay_pct = f"{(c.replayed / c.selected * 100):.1f}%" if c.selected else "n/a"
        score_pct = f"{(c.scored / c.selected * 100):.1f}%" if c.selected else "n/a"
        lines.append(
            f"**{c.name}:** {c.selected} selected, "
            f"{c.replayed} replayed ({replay_pct}), "
            f"{c.scored} scored ({score_pct})."
        )
    return lines


def _floor_evaluation_section(
    cohort_results: list[CohortResult],
) -> list[str]:
    """Floor table — only rendered when at least one cohort has a
    floor failure. Clean Ship omits this section entirely."""
    # Iterate `failing` directly for the rows: the cohorts without
    # floor_failures are filtered upfront, so the row-emission loop
    # only walks the cohorts that actually contribute. Avoids
    # iterating cohorts whose `floor_failures` happens to be empty
    # (currently harmless via the empty-list short-circuit, but
    # fragile if the type ever widens to allow `None`).
    failing = [c for c in cohort_results if c.floor_failures]
    if not failing:
        return []

    lines = [
        "## Floor evaluation",
        "",
        "| Rule | Cohort | Observed | Threshold | Status |",
        "|------|--------|----------|-----------|--------|",
    ]
    for c in failing:
        for ff in c.floor_failures:
            lines.append(_floor_row(c.name, ff))
    return lines


def _floor_row(cohort_name: str, ff: FloorFailure) -> str:
    return f"| **{ff.rule}** | **{cohort_name}** | **{ff.observed}** | **{ff.threshold}** | **✗** |"


def _suggested_next_steps_section(report: ReportV01) -> list[str]:
    """The summary's `#fix` jump-link target. Renders one
    `FIX_SUGGESTION_REGISTRY` template per blocking finding:

      ### <summary>

      1. step 1
      2. step 2
      ...

    Findings are sorted by severity rank (highest first) so the
    most-blocking suggestion appears at the top. Cardinal #8: the
    section is structurally non-empty for any non-Ship verdict; the
    coverage test in `tests/unit/whatifd/decision/` already pins
    that every floor rule + every blocking finding code has a
    registered fix suggestion, so the `KeyError` fallback path
    below is defensive only.
    """
    lines = ['<a id="fix"></a>', "## Suggested next steps", ""]
    if report.verdict_state == "ship":
        lines.append("No actionable findings — the verdict is Ship.")
        return lines

    blocking = [f for f in report.decision_findings if f.severity in {"blocks_all", "blocks_ship"}]
    if blocking:
        # Sort by severity rank (highest first), stable on input order.
        blocking_sorted = sorted(blocking, key=lambda f: -_SEVERITY_RANK[f.severity])
        for i, finding in enumerate(blocking_sorted):
            if i > 0:
                lines.append("")  # blank line between templates
            template = FIX_SUGGESTION_REGISTRY.get(finding.code)
            if template is not None:
                lines.append(f"### {template.summary}")
                lines.append("")
                for n, step in enumerate(template.steps, start=1):
                    lines.append(f"{n}. {step}")
            else:
                # Defensive fallback for an unregistered finding
                # code. The coverage test pins this is unreachable
                # for production codes; the fallback exists so a
                # mid-development addition of a new code surfaces a
                # recognizable string rather than crashing the
                # render.
                lines.append(f"### `{finding.code}` (no registered template)")
                lines.append("")
                lines.append(finding.message)
        return lines

    floor_rule = _top_floor_failure_summary(report.cohort_results)
    if floor_rule is not None:
        lines.append(floor_rule)
        return lines

    lines.append("(No actionable suggestions available.)")
    return lines


def _methodology_section(m: MethodologyDisclosure) -> list[str]:
    """Cardinal #10 disclosure. Renders every required field; the
    five reliability concepts (reproducibility / reliability /
    validity / calibration / bias) are surfaced explicitly even
    when False — silence is the failure mode #10 was written to
    prevent.
    """
    j = m.judge
    b = m.bootstrap
    e = m.effect_size
    mu = m.multiplicity

    bootstrap_line = (
        f"Bootstrap: {b.method}"
        if b.method == "unavailable"
        else f"Bootstrap: {b.method}, B={b.resamples}, seed={b.seed}"
    )

    cluster_line = (
        f"Cluster: {b.cluster_key}"
        if b.cluster_key is not None
        else "Cluster: none (i.i.d. assumption)"
    )

    reliability = (
        f"reproducibility={_yn(j.reproducibility_addressed)}, "
        f"reliability={_yn(j.reliability_measured)}, "
        f"validity={_yn(j.validity_measured)}, "
        f"calibration={_yn(j.calibration_measured)}, "
        f"bias={_yn(j.bias_audit_measured)}"
    )

    return [
        "## Methodology",
        "",
        f"- Unit: {m.unit_of_analysis} · Primary metric: {m.primary_metric}",
        f"- Endpoints: {', '.join(m.primary_endpoints)}",
        f"- Cohorts: {', '.join(m.cohorts)}",
        f"- {bootstrap_line} · CI level: {b.ci_level}",
        f"- {cluster_line} · Multiplicity: {mu.correction}",
        f"- Per-trace inference: {m.per_trace_inference}",
        f"- Causal scope: {m.causal_claim_scope}",
        (
            f"- Judge: {j.judge_model} · Cache: "
            f"{'enabled' if j.scorer_cache_enabled else 'disabled'} "
            f"({j.scorer_cache_hits} hits, {j.scorer_cache_misses} misses)"
        ),
        f"- Practical delta: {e.practical_delta} ({e.practical_delta_source})",
        f"- Reliability state: {reliability}",
    ]


def _manifest_pointer() -> list[str]:
    return ["[Manifest →](manifest.json)"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _yn(flag: bool) -> str:
    return "yes" if flag else "no"


def _highest_severity_finding(
    findings: list[DecisionFinding],
) -> DecisionFinding | None:
    """Severity rank shared with `ci_status` and `summary` via
    `_constants` — the three formats agree on which finding 'wins'."""
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


__all__ = ["render_full_report"]
