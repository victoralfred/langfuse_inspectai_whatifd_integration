"""Live Langfuse → run_pipeline smoke test.

Reads up to 40 traces from your Langfuse, splits them into `failure` /
`baseline` cohorts by tag, runs them through `whatifd.pipeline.run_pipeline`
with a deterministic `delta_fn` (no agent runner, no Inspect AI), and
writes the resulting ReportV01 to ./reports/.

Verdict will probably be Inconclusive (deterministic delta_fn means no
real signal) — that's fine. The point is to see the Langfuse adapter
ingest your real traces, the pipeline construct a real ReportV01, and
the cardinal-#5 graph walk + cardinal-#10 methodology disclosure run
against your data end-to-end.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from langfuse.api import LangfuseAPI
from whatifd_langfuse import LangfuseTraceSource

from whatifd.adapters.protocols import RawTrace
from whatifd.cache.summary import CachePolicySnapshot, CacheSummary
from whatifd.pipeline import run_pipeline
from whatifd.serialization import (
    assert_no_unredacted_sensitive,
    encode_report_v01,
)
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
# 1. Cohort classifier: how to split your traces into failure vs baseline.
# ---------------------------------------------------------------------------
#
# Pick ONE of the strategies below — the one that matches how you tag /
# label traces in Langfuse today.

def cohort_by_tag(trace) -> str:
    """Tag-based: a trace tagged `failed` is a failure; everything else baseline."""
    tags = trace.tags or []
    return "failure" if "failed" in tags else "baseline"

# Alternative classifiers — uncomment ONE if tags don't fit your data:

# def cohort_by_score(trace) -> str:
#     """Score-based: scores under 0.6 are failures."""
#     scores = getattr(trace, "scores", None) or []
#     for s in scores:
#         if getattr(s, "value", 1.0) < 0.6:
#             return "failure"
#     return "baseline"

# def cohort_by_metadata(trace) -> str:
#     """Metadata-based: e.g., metadata.outcome == 'error'."""
#     md = trace.metadata or {}
#     return "failure" if md.get("outcome") == "error" else "baseline"


# ---------------------------------------------------------------------------
# 2. Deterministic delta_fn — no Inspect AI, no agent runner needed.
# ---------------------------------------------------------------------------
#
# In a real run this would invoke your runner + scorer. Here it's a
# constant-per-cohort delta so the pipeline runs end-to-end against your
# real Langfuse data without any other dependencies. The verdict will
# reflect this (likely Inconclusive); see Going further for the real
# runner + Inspect AI wiring.

def delta_fn(rt: RawTrace) -> float:
    return 0.4 if rt.cohort == "failure" else 0.05


# ---------------------------------------------------------------------------
# 3. Construct LangfuseTraceSource against your real Langfuse.
# ---------------------------------------------------------------------------

api = LangfuseAPI(
    base_url=os.environ["LANGFUSE_HOST"],
    username=os.environ["LANGFUSE_PUBLIC_KEY"],
    password=os.environ["LANGFUSE_SECRET_KEY"],
)

source = LangfuseTraceSource(
    api=api,
    cohort_classifier=cohort_by_tag,    # swap if you uncommented an alternative above
    page_limit=50,
    max_traces=40,                      # safety cap so we don't drain a prod project
    sdk_version="live-langfuse-smoke",
)


# ---------------------------------------------------------------------------
# 4. Manifest + methodology + cache summary boilerplate.
# ---------------------------------------------------------------------------

now = datetime.now(timezone.utc).isoformat(timespec="seconds")

manifest = RunManifest(
    experiment_id="live-langfuse-smoke",
    started_at=now,
    finished_at=now,
    duration_ms=0,
    whatif_version="0.1.0",
    config_hash="0" * 64,           # not load-bearing for a smoke test
    selection_seed=42,
    source="langfuse",
    target="deterministic-delta-fn",
    trust_floor=TrustFloor(),
    decision_policy=DecisionPolicy(),
    environment=EnvironmentFingerprint(
        python="3.12",
        platform="linux",
        whatif_version="0.2.0",
    ),
)

methodology = MethodologyDisclosure(
    unit_of_analysis="paired_trace_delta",
    primary_metric="faithfulness",
    primary_endpoints=("failure.faithfulness", "baseline.faithfulness"),
    cohorts=("failure", "baseline"),
    bootstrap=BootstrapMethodDisclosure(
        # v0.2 ships the doctrinally-correct paired-percentile bootstrap.
        # For a smoke test with a deterministic delta_fn there's no
        # random sampling worth re-running, so keeping `unavailable`
        # is honest — but for real Langfuse runs you'd declare
        # method="paired_percentile_bootstrap" with resamples=2000
        # and a fixed seed.
        method="unavailable",
        resamples=None,
        seed=None,
        sample_unit="paired_trace_delta",
        ci_level="0.950",
        cluster_key=None,
        assumptions=(),
        unavailable_reason="smoke test — deterministic delta_fn, no random sampling",
    ),
    multiplicity=MultiplicityDisclosure(
        primary_endpoint_count=2,
        correction="none",
        reason="single primary metric per cohort; no correction applied",
    ),
    judge=JudgeMethodDisclosure(
        scorer="deterministic",
        scorer_version="0.1.0",
        judge_provider="none",
        judge_model="none",
        judge_model_version=None,
        rendered_prompt_hash="0" * 16,
        rubric_hash="0" * 16,
        scorer_cache_enabled=False,
        scorer_cache_mode="off",
        scorer_cache_hits=0,
        scorer_cache_misses=0,
        reproducibility_addressed=False,
        reliability_measured=False,
        validity_measured=False,
        calibration_measured=False,
        bias_audit_measured=False,
    ),
    effect_size=EffectSizeDisclosure(
        practical_delta="0.050",
        practical_delta_source="policy",
        judge_noise_floor=None,
    ),
    per_trace_inference="descriptive_only",
    causal_claim_scope="associated_under_cached_tool_replay",
)

cache_summary = CacheSummary(
    schema_version="v1",
    key_version="v1",
    mode="off",
    storage_profile="normalized_result_only",
    storage_path=".whatifd/cache",
    hits=0,
    misses=0,
    writes=0,
    stale_hits=0,
    corrupted_entries=0,
    policy=CachePolicySnapshot(
        mode="off",
        warn_after_days=30,
        block_after_days=90,
        storage_profile="normalized_result_only",
    ),
    policy_violations=(),
    oldest_hit_age_days=None,
    models_distribution=MappingProxyType({}),
)


# ---------------------------------------------------------------------------
# 5. Run the pipeline.
# ---------------------------------------------------------------------------

report = run_pipeline(
    source,
    delta_fn=delta_fn,
    floor=TrustFloor(),
    policy=DecisionPolicy(),
    runtime=manifest,
    methodology=methodology,
    cache_summary=cache_summary,
)

# Cardinal-#5 graph walk: refuse to write any unwrapped Sensitive[T].
assert_no_unredacted_sensitive(report)


# ---------------------------------------------------------------------------
# 6. Write artifacts.
# ---------------------------------------------------------------------------

reports_dir = Path("reports")
reports_dir.mkdir(exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")

json_path = reports_dir / f"live-langfuse-{stamp}.json"
json_path.write_text(json.dumps(json.loads(encode_report_v01(report)), indent=2))

print(f"Verdict: {report.verdict_state}")
print(f"JSON:    {json_path}")
print(f"Cohorts seen: {[c.name for c in report.cohort_results]}")
print(f"Traces ingested per cohort: " + ", ".join(
    f"{c.name}={c.selected}" for c in report.cohort_results
))