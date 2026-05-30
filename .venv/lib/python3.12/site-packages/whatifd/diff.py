"""`whatifd diff` — compare two whatifd reports.

Phase 8.4 of the v0.1 implementation plan. Surfaced in walkthrough
scenario 6 (rerun-after-fix): an engineer fixes an issue an
earlier run flagged, reruns, and wants to see "did anything
change". The diff is a Markdown summary of structural deltas
between two `ReportV01` JSON files.

## Scope (v0.1)

- `verdict_state` transitions (e.g., `dont_ship` → `ship`).
- `cohort_results`: per-cohort counts (improved / unchanged /
  regressed / scored) and `median_delta`.
- `decision_findings`: added / removed codes.
- `failures`: count change.

## Deferred to v0.2

- Per-trace evidence diff (which traces newly improved /
  regressed). Requires the per-trace evidence schema that's
  cascade-tracked but not in v0.1 `ReportV01`.
- Full structural recursive diff of methodology / cache_summary
  / runtime. v0.1 surfaces verdict-relevant deltas; deeper
  structural drift is a debugging concern, not a verdict
  concern.

## Cardinal alignment

- **#1 failures-as-data:** read errors (file missing, parse fail)
  return typed `DiffError`; consumers branch rather than catch
  exceptions.
- **#6 typed boundaries:** the diff result is a frozen dataclass
  with typed sub-records, not a free-form dict.
- **#10 statistical claims match design:** the diff renders
  cohort numbers as deltas — it does NOT add inference (e.g.,
  "this is a regression" without the policy threshold the
  decision pipeline applied). The verdict-state transition is
  the load-bearing claim; everything else is descriptive.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from whatifd.render import VERDICT_LABEL


class DiffError(Exception):
    """Raised when one of the input report files cannot be read or
    parsed. Distinct from a genuine ValueError on shape mismatch:
    `DiffError` is the boundary error (file/path/parse), shape
    issues bubble up as KeyError or AttributeError when the diff
    walks an unexpected report.
    """


@dataclass(frozen=True, slots=True)
class CohortDelta:
    """Per-cohort comparison row. All counts are `prev → new`."""

    name: str
    selected_prev: int
    selected_new: int
    scored_prev: int
    scored_new: int
    improved_prev: int
    improved_new: int
    regressed_prev: int
    regressed_new: int
    unchanged_prev: int
    unchanged_new: int
    median_delta_prev: str | None
    median_delta_new: str | None


@dataclass(frozen=True, slots=True)
class FindingDelta:
    """Decision findings present in one report but not the other.
    `direction="added"` means present in `new` and not `prev`;
    `direction="removed"` is the inverse.
    """

    code: str
    severity: str
    direction: Literal["added", "removed"]
    message: str


@dataclass(frozen=True, slots=True)
class DiffReport:
    """Structured comparison of two `ReportV01` shapes.

    Frozen + slotted; consumers (CLI renderer, future programmatic
    callers) read fields directly. The renderer
    (`render_diff_markdown`) is the standard surface; alternative
    renderers can be added by walking this dataclass.
    """

    verdict_state_prev: str
    verdict_state_new: str
    schema_version_prev: str
    schema_version_new: str
    failures_prev: int
    failures_new: int
    cohorts: tuple[CohortDelta, ...]
    findings: tuple[FindingDelta, ...]

    @property
    def is_empty(self) -> bool:
        """True iff every diff-relevant field is unchanged. Co-locates
        the invariant with the data so renderers (and any future
        programmatic consumer) don't re-examine all fields from
        outside the dataclass. The findings short-circuit is
        belt-and-suspenders: the renderer already guards via
        `elif report.is_empty` after the Findings section, so a
        non-empty `findings` tuple already bypasses this property
        in practice. The check stays here so a future programmatic
        caller (test fixture, alternative renderer) can call
        `is_empty` standalone and get the right answer without
        replicating the renderer's branching."""
        if self.findings:
            return False
        if self.verdict_state_prev != self.verdict_state_new:
            return False
        if self.schema_version_prev != self.schema_version_new:
            return False
        if self.failures_prev != self.failures_new:
            return False
        for c in self.cohorts:
            if (
                c.selected_prev != c.selected_new
                or c.scored_prev != c.scored_new
                or c.improved_prev != c.improved_new
                or c.regressed_prev != c.regressed_new
                or c.unchanged_prev != c.unchanged_new
                or c.median_delta_prev != c.median_delta_new
            ):
                return False
        return True


def load_report(path: Path) -> dict[str, Any]:
    """Read and parse a whatifd report JSON file.

    Returns the raw dict — diff operates on the wire shape rather
    than reconstructing `ReportV01` because (a) the diff is
    presentational and doesn't need full Pydantic validation, and
    (b) reading a v0.2 report into v0.1 types would fail
    spuriously when the diff path is exactly the case operators
    most need (cross-version comparison during migration).
    """
    if not path.exists():
        raise DiffError(f"report file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DiffError(f"cannot read {path}: {exc}") from exc
    try:
        # `json.loads` is fine here: the banned-import lint targets
        # `json.dumps` outside `whatifd/serialization/` (cardinal #5
        # last-line redaction defense). Reading is unrestricted.
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DiffError(f"JSON parse error in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DiffError(f"report file {path} must parse to a mapping; got {type(data).__name__}")
    return data


def compute_diff(prev: dict[str, Any], new: dict[str, Any]) -> DiffReport:
    """Build a `DiffReport` from two raw report dicts.

    Missing fields raise `KeyError` — the diff requires both
    inputs to be valid report shapes. Use `load_report` to surface
    file-level errors as `DiffError`; shape errors are programmer
    bugs upstream and propagate.
    """
    return DiffReport(
        verdict_state_prev=prev["verdict_state"],
        verdict_state_new=new["verdict_state"],
        schema_version_prev=prev["schema_version"],
        schema_version_new=new["schema_version"],
        failures_prev=len(prev.get("failures", [])),
        failures_new=len(new.get("failures", [])),
        cohorts=_diff_cohorts(prev, new),
        findings=_diff_findings(prev, new),
    )


def _diff_cohorts(prev: dict[str, Any], new: dict[str, Any]) -> tuple[CohortDelta, ...]:
    """Build per-cohort deltas keyed on cohort `name`.

    **Asymmetric cohorts** (a cohort name present in only one report —
    a new cohort added in the second run, or an old cohort dropped):
    the absent side's counters render as zero, NOT as a missing row.
    A new `failure` cohort with 10 selected appears as `0→10` for
    every counter, surfacing the addition; a dropped cohort appears
    as `10→0`. This is deliberate: the cohort row IS the diff signal,
    and an operator looking at scenario 6 (rerun-after-fix) needs to
    see a new cohort in the table rather than have it silently absent.
    Median delta uses `n/a` on the absent side (handled by
    `_pair_str`). Tracked in CHANGELOG so operators encountering
    `0→N` rows know what they mean."""
    prev_by_name = {c["name"]: c for c in prev.get("cohort_results", [])}
    new_by_name = {c["name"]: c for c in new.get("cohort_results", [])}
    all_names = sorted(set(prev_by_name) | set(new_by_name))
    deltas: list[CohortDelta] = []
    for name in all_names:
        p = prev_by_name.get(name, {})
        n = new_by_name.get(name, {})
        deltas.append(
            CohortDelta(
                name=name,
                selected_prev=int(p.get("selected", 0)),
                selected_new=int(n.get("selected", 0)),
                scored_prev=int(p.get("scored", 0)),
                scored_new=int(n.get("scored", 0)),
                improved_prev=int(p.get("improved_count", 0)),
                improved_new=int(n.get("improved_count", 0)),
                regressed_prev=int(p.get("regressed_count", 0)),
                regressed_new=int(n.get("regressed_count", 0)),
                unchanged_prev=int(p.get("unchanged_count", 0)),
                unchanged_new=int(n.get("unchanged_count", 0)),
                median_delta_prev=p.get("median_delta"),
                median_delta_new=n.get("median_delta"),
            )
        )
    return tuple(deltas)


@dataclass(frozen=True, slots=True)
class _FindingSource:
    """One half of the findings diff: the keys to emit in some
    direction, paired with the source dict that holds the full
    record. Bucketed so we can emit `added` (NEW first, for operator
    triage) before `removed` in a single pass. Frozen + slotted to
    match the rest of the module; promotes the prior verbose inline
    `tuple[int, set[...], dict[...], Literal[...]]` annotation into
    a named shape that's easier to extend in v0.2 (e.g., a
    `severity_changed` bucket when severity-transition deltas earn
    their own row)."""

    keys: frozenset[tuple[str, str]]
    by_key: Mapping[tuple[str, str], Mapping[str, Any]]
    direction: Literal["added", "removed"]


def _diff_findings(prev: dict[str, Any], new: dict[str, Any]) -> tuple[FindingDelta, ...]:
    """Identify findings present in one report but not the other.

    Keyed on (code, severity) so a finding that changed severity
    appears as both removed-and-added — the renderer surfaces both
    rows so the operator sees the transition.
    """
    prev_keys = {(f["code"], f["severity"]): f for f in prev.get("decision_findings", [])}
    new_keys = {(f["code"], f["severity"]): f for f in new.get("decision_findings", [])}
    # Wrap source dicts in MappingProxyType so the frozen `by_key`
    # field cannot be mutated through the dataclass surface — frozen=True
    # only blocks field rebinding, not mutation of a mutable dict.
    sources = (
        _FindingSource(
            keys=frozenset(new_keys.keys() - prev_keys.keys()),
            by_key=MappingProxyType(new_keys),
            direction="added",
        ),
        _FindingSource(
            keys=frozenset(prev_keys.keys() - new_keys.keys()),
            by_key=MappingProxyType(prev_keys),
            direction="removed",
        ),
    )
    deltas: list[FindingDelta] = []
    for src in sources:
        for key in sorted(src.keys):
            f = src.by_key[key]
            deltas.append(
                FindingDelta(
                    code=f["code"],
                    severity=f["severity"],
                    direction=src.direction,
                    message=f.get("message", ""),
                )
            )
    return tuple(deltas)


def render_diff_markdown(report: DiffReport) -> str:
    """Render the diff as Markdown. Output ends with a single
    trailing newline (artifact-style). The caller pipes to stdout
    or splices into a PR-comment surface."""
    lines: list[str] = []
    lines.append("# whatifd diff")
    lines.append("")

    # Verdict transition
    prev_label = VERDICT_LABEL.get(report.verdict_state_prev, report.verdict_state_prev)
    new_label = VERDICT_LABEL.get(report.verdict_state_new, report.verdict_state_new)
    if report.verdict_state_prev == report.verdict_state_new:
        lines.append(f"**Verdict:** {new_label} (unchanged)")
    else:
        lines.append(f"**Verdict:** {prev_label} → {new_label}")

    if report.schema_version_prev != report.schema_version_new:
        lines.append(f"**Schema:** {report.schema_version_prev} → {report.schema_version_new}")

    lines.append("")

    # Failures count — suppressed when unchanged, matching Schema's
    # behavior. The verdict line is kept always-present (it's the
    # load-bearing claim per cardinal #10); Schema and Failures are
    # descriptive and read cleaner when only the deltas surface.
    # A nonzero unchanged-failure count is still visible via the
    # Cohorts table and via the per-report views; the diff doesn't
    # need to repeat it.
    if report.failures_prev != report.failures_new:
        lines.append(
            f"**Failures:** {report.failures_prev} → {report.failures_new} "
            f"({_signed(report.failures_new - report.failures_prev)})"
        )
        lines.append("")

    # Cohort table
    if report.cohorts:
        lines.append("## Cohorts")
        lines.append("")
        lines.append("| Cohort | Selected | Scored | Improved | Regressed | Median Δ |")
        lines.append("|--------|----------|--------|----------|-----------|----------|")
        for c in report.cohorts:
            lines.append(
                f"| {c.name} "
                f"| {_pair(c.selected_prev, c.selected_new)} "
                f"| {_pair(c.scored_prev, c.scored_new)} "
                f"| {_pair(c.improved_prev, c.improved_new)} "
                f"| {_pair(c.regressed_prev, c.regressed_new)} "
                f"| {_pair_str(c.median_delta_prev, c.median_delta_new)} |"
            )
        lines.append("")

    # Findings deltas
    if report.findings:
        lines.append("## Findings")
        lines.append("")
        for f in report.findings:
            arrow = "+" if f.direction == "added" else "-"
            lines.append(f"- **{arrow}** `{f.code}` ({f.severity}): {f.message}")
        lines.append("")
    elif report.is_empty:
        lines.append("(No changes detected.)")
        lines.append("")

    body = "\n".join(lines).rstrip()
    return body + "\n"


def _signed(n: int) -> str:
    """Render an int with explicit sign (`+5`, `-3`, `0`)."""
    if n > 0:
        return f"+{n}"
    return str(n)


def _pair(prev: int, new: int) -> str:
    """Render a `prev → new (±delta)` cell, suppressing the delta
    when unchanged."""
    if prev == new:
        return f"{new}"
    return f"{prev}→{new} ({_signed(new - prev)})"


def _pair_str(prev: str | None, new: str | None) -> str:
    """Render a string-valued (median delta) cell. `None` rendered
    as `n/a`. Both-None compares equal as the same `n/a` string, so
    a cohort that lacks a median in both reports renders as a single
    `n/a` (unchanged) rather than `n/a→n/a` — the unchanged path is
    deliberate for None-equality."""
    p = prev if prev is not None else "n/a"
    n = new if new is not None else "n/a"
    if p == n:
        return n
    return f"{p}→{n}"


__all__ = [
    "CohortDelta",
    "DiffError",
    "DiffReport",
    "FindingDelta",
    "compute_diff",
    "load_report",
    "render_diff_markdown",
]
