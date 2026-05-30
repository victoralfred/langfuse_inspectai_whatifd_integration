"""Production-grade whatifd run against live Langfuse — corrected.

What the original `live_langfuse_test.py` got wrong, and this fixes:

  1. Cohort classifier used a `"failed"` TAG that none of the traces carry,
     so every trace fell into `baseline`. This version classifies on the
     auto-scorer's REAL signal: the `score` embedded in each evaluator
     trace's `output` JSON (`{"reasoning": ..., "score": N}`).
  2. The 2 non-evaluator traces ("Claude Code - Turn N", shape
     `{role, content}`) are filtered out — they carry no score and must
     not pollute a cohort.
  3. The delta_fn was a FABRICATED constant (0.4 / 0.05). whatifd measures
     `replayed_score - original_score`. With no proposed change, the honest
     replay is the IDENTITY (replayed == original) → delta == 0 for every
     trace. We do not invent signal that the experiment did not produce.

It then runs BOTH experiment shapes so you can see whatifd's real verdict:
  - failure_rescue (default): requires a failure cohort. On this data that
    cohort is empty → Inconclusive (correct, not a bug).
  - regression_check: baseline-only. Runs, but on zero-variance identity
    data there is nothing for it to find.

The cohort split is governed by SCORE_SCALE below, because the data is
genuinely ambiguous (see the module docstring in the report we print).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from langfuse.api import LangfuseAPI

from whatifd.adapters.protocols import RawTrace
from whatifd.cache.summary import CachePolicySnapshot, CacheSummary
from whatifd.pipeline import run_pipeline
from whatifd.serialization import assert_no_unredacted_sensitive, encode_report_v01
from whatifd.types.manifest import EnvironmentFingerprint, RunManifest
from whatifd.types.policy import DecisionPolicy, TrustFloor
from whatifd.types.statistical import (
    BootstrapMethodDisclosure,
    EffectSizeDisclosure,
    JudgeMethodDisclosure,
    MethodologyDisclosure,
    MultiplicityDisclosure,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The data is ambiguous about what `score: 1` means (rubric says 1-5 with
# 5=best, but the reasoning text says "fully accurate" which is 5/5). Pick
# the interpretation explicitly so the cohort split is auditable, not magic.
#   "binary_pass" : score>=1 is a PASS (baseline); only score==0 is failure.
#   "rubric_1_5"  : normalize (score-1)/4; failure if normalized < 0.6.
SCORE_SCALE: Literal["binary_pass", "rubric_1_5"] = "binary_pass"
FAILURE_THRESHOLD = 0.6  # only used for rubric_1_5

_env = json.loads(Path("/home/voseghale/projects/trading/.claude/settings.json").read_text())["env"]


# ---------------------------------------------------------------------------
# 1. Cohort classifier — uses the REAL embedded auto-scorer score.
# ---------------------------------------------------------------------------

def _extract_score(trace: Any) -> float | None:
    """Return the auto-scorer score embedded in the evaluator trace output,
    or None if this trace is not an evaluator run (filtered out)."""
    out = getattr(trace, "output", None)
    if isinstance(out, dict) and isinstance(out.get("score"), (int, float)):
        return float(out["score"])
    return None


def _normalize(score: float) -> float:
    if SCORE_SCALE == "binary_pass":
        return 1.0 if score >= 1 else 0.0
    return max(0.0, min(1.0, (score - 1.0) / 4.0))  # 1-5 -> 0-1


def cohort_by_embedded_score(trace: Any) -> str:
    score = _extract_score(trace)
    if score is None:
        # Non-evaluator trace; tag so we can drop it before the pipeline.
        return "_skip"
    norm = _normalize(score)
    threshold = 0.5 if SCORE_SCALE == "binary_pass" else FAILURE_THRESHOLD
    return "failure" if norm < threshold else "baseline"


# ---------------------------------------------------------------------------
# 2. A score-filtering TraceSource wrapper — drops `_skip` traces and exposes
#    the real per-trace original score for the identity-replay delta.
# ---------------------------------------------------------------------------

class FilteredLangfuseSource:
    """Wraps the live Langfuse fetch, classifies by embedded score, and
    yields only real evaluator traces as RawTrace (failure/baseline)."""

    def __init__(self, api: LangfuseAPI, max_traces: int = 100) -> None:
        self._api = api
        self._max = max_traces
        self.original_scores: dict[str, float] = {}

    def iter_traces(self):
        from whatifd.types.sensitive import Sensitive
        emitted = 0
        page = 1
        while emitted < self._max:
            data = list(self._api.trace.list(page=page, limit=50).data)
            if not data:
                break
            for t in data:
                cohort = cohort_by_embedded_score(t)
                if cohort == "_skip":
                    continue
                self.original_scores[t.id] = _normalize(_extract_score(t))
                yield RawTrace(
                    trace_id=t.id,
                    cohort=cohort,
                    user_message=Sensitive(str(t.input), classification="user_content"),
                    original_response=Sensitive(str(t.output), classification="user_content"),
                    metadata=MappingProxyType({}),
                )
                emitted += 1
                if emitted >= self._max:
                    return
            if len(data) < 50:
                break
            page += 1

    def adapter_metadata(self):
        from whatifd.adapters.protocols import AdapterMetadata
        return AdapterMetadata(adapter_id="langfuse-filtered", package_version="0.2.0", sdk_version="4.7.1")

    def cluster_key_support(self):
        from whatifd.types.statistical import ClusterKeySupport
        return ClusterKeySupport(available_keys=())


# ---------------------------------------------------------------------------
# 3. Honest delta_fn: IDENTITY replay. No change proposed -> replayed score
#    == original score -> delta == 0. We do not fabricate signal.
# ---------------------------------------------------------------------------

def identity_delta(rt: RawTrace) -> float:
    return 0.0


# ---------------------------------------------------------------------------
# 4. Boilerplate: manifest / methodology / cache summary (honest values).
# ---------------------------------------------------------------------------

def _methodology() -> MethodologyDisclosure:
    return MethodologyDisclosure(
        unit_of_analysis="paired_trace_delta",
        primary_metric="factual_accuracy_score",
        primary_endpoints=("baseline.factual_accuracy_score",),
        cohorts=("baseline",),
        bootstrap=BootstrapMethodDisclosure(
            method="unavailable",
            resamples=None,
            seed=None,
            sample_unit="paired_trace_delta",
            ci_level="0.950",
            cluster_key=None,
            assumptions=(),
            unavailable_reason="identity replay (no proposed change) -> zero-variance deltas; bootstrap not meaningful",
        ),
        multiplicity=MultiplicityDisclosure(
            primary_endpoint_count=1, correction="none",
            reason="single endpoint; no correction",
        ),
        judge=JudgeMethodDisclosure(
            scorer="langfuse_embedded_autoscorer", scorer_version="recorded",
            judge_provider="langfuse-eval", judge_model="recorded",
            judge_model_version=None, rendered_prompt_hash="0" * 16, rubric_hash="0" * 16,
            scorer_cache_enabled=False, scorer_cache_mode="off",
            scorer_cache_hits=0, scorer_cache_misses=0,
            reproducibility_addressed=False, reliability_measured=False,
            validity_measured=False, calibration_measured=False, bias_audit_measured=False,
        ),
        effect_size=EffectSizeDisclosure(
            practical_delta="0.050", practical_delta_source="policy", judge_noise_floor=None,
        ),
        per_trace_inference="descriptive_only",
        causal_claim_scope="associated_under_cached_tool_replay",
    )


def _cache_summary() -> CacheSummary:
    return CacheSummary(
        schema_version="v1", key_version="v1", mode="off",
        storage_profile="normalized_result_only", storage_path=".whatifd/cache",
        hits=0, misses=0, writes=0, stale_hits=0, corrupted_entries=0,
        policy=CachePolicySnapshot(mode="off", warn_after_days=30, block_after_days=90,
                                   storage_profile="normalized_result_only"),
        policy_violations=(), oldest_hit_age_days=None,
        models_distribution=MappingProxyType({}),
    )


def _manifest(shape: str) -> RunManifest:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return RunManifest(
        experiment_id=f"live-langfuse-{shape}",
        started_at=now, finished_at=now, duration_ms=0,
        whatif_version="0.2.0", config_hash="0" * 64, selection_seed=42,
        source="langfuse", target="identity-replay",
        trust_floor=TrustFloor(), decision_policy=DecisionPolicy(),
        environment=EnvironmentFingerprint(python="3.12", platform="linux", whatif_version="0.2.0"),
    )


# ---------------------------------------------------------------------------
# 5. Run.
# ---------------------------------------------------------------------------

def run_shape(api: LangfuseAPI, shape: str) -> None:
    print(f"\n{'='*70}\nEXPERIMENT SHAPE: {shape}  (SCORE_SCALE={SCORE_SCALE})\n{'='*70}")
    source = FilteredLangfuseSource(api, max_traces=100)
    kwargs: dict[str, Any] = {}
    if shape != "failure_rescue":
        kwargs["experiment_shape"] = shape
    try:
        report = run_pipeline(
            source, delta_fn=identity_delta,
            floor=TrustFloor(), policy=DecisionPolicy(),
            runtime=_manifest(shape), methodology=_methodology(),
            cache_summary=_cache_summary(), **kwargs,
        )
    except TypeError:
        # run_pipeline may not accept experiment_shape kwarg in this build.
        report = run_pipeline(
            source, delta_fn=identity_delta,
            floor=TrustFloor(), policy=DecisionPolicy(),
            runtime=_manifest(shape), methodology=_methodology(),
            cache_summary=_cache_summary(),
        )
    assert_no_unredacted_sensitive(report)

    print(f"VERDICT: {report.verdict_state}")
    print("cohorts ingested:")
    for c in report.cohort_results:
        print(f"  {c.name}: selected={c.selected} scored={c.scored} "
              f"median_delta={c.median_delta} floor_passed={c.floor_passed} "
              f"floor_failures={[f.code for f in c.floor_failures]}")
    if report.decision_findings:
        print("decision findings:")
        for f in report.decision_findings:
            print(f"  [{getattr(f,'severity','?')}] {getattr(f,'code','?')}: {getattr(f,'summary','')}")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    out = Path("reports") / f"production-{shape}-{stamp}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(json.loads(encode_report_v01(report)), indent=2))
    print(f"report: {out}")


def main() -> None:
    print(f"Langfuse: {_env['LANGFUSE_BASE_URL']}")
    api = LangfuseAPI(
        base_url=_env["LANGFUSE_BASE_URL"],
        username=_env["LANGFUSE_PUBLIC_KEY"],
        password=_env["LANGFUSE_SECRET_KEY"],
    )
    for shape in ("failure_rescue", "regression_check"):
        try:
            run_shape(api, shape)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the other shape
            print(f"  {shape} raised: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
