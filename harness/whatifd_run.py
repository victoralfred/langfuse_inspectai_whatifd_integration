"""End-to-end whatifd run against live Langfuse, using the corrected
faithfulness evaluator as the ruler.

Pipeline (the role-swap from README §"role swap"):
  - SOURCE  : fork the Claude Code AGENT turns (not the evaluator traces).
  - COHORT  : faithfulness(original response vs tool results) — low → failure,
              high → baseline. Real signal from the fixed evaluator.
  - RUNNER  : re-synthesize the agent's final summary from (user request +
              tool results) under a CANDIDATE system prompt — the change
              being tested.
  - SCORER  : faithfulness(replayed vs tool results) − faithfulness(original
              vs tool results) = delta. Same ruler on both sides (cardinal
              #10).
  - VERDICT : whatifd.run_pipeline → failure_rescue Ship/Don't-Ship/Inconclusive.

Cost: ~3 Haiku calls per turn (cohort score + replay + replayed score).

In production you would not hand-roll the scorer — you would point
`whatifd fork` at an `InspectAIScorer` carrying this same rubric (README §3).
This programmatic harness is the local, dependency-light embodiment.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import anthropic
from langfuse.api import LangfuseAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluator"))
from faithfulness import (  # noqa: E402
    JUDGE_MODEL,
    extract_response,
    extract_tool_results,
    score_faithfulness,
)
from inspect_scorer import build_inspect_scorer  # noqa: E402

from whatifd.adapters.protocols import AdapterMetadata, RawTrace  # noqa: E402
from whatifd.contract import ReplayOutput, ScoreCase, TraceInput, TraceOutput  # noqa: E402
from whatifd.cache.summary import CachePolicySnapshot, CacheSummary  # noqa: E402
from whatifd.pipeline import run_pipeline  # noqa: E402
from whatifd.serialization import assert_no_unredacted_sensitive, encode_report_v01  # noqa: E402
from whatifd.types.manifest import EnvironmentFingerprint, RunManifest  # noqa: E402
from whatifd.types.policy import DecisionPolicy, TrustFloor  # noqa: E402
from whatifd.types.sensitive import Sensitive  # noqa: E402
from whatifd.types.statistical import (  # noqa: E402
    BootstrapMethodDisclosure,
    ClusterKeySupport,
    EffectSizeDisclosure,
    JudgeMethodDisclosure,
    MethodologyDisclosure,
    MultiplicityDisclosure,
)

# --- config ---------------------------------------------------------------
# Scorer slot backend. "inspect" routes scoring through Inspect AI
# (whatifd_inspect_ai.InspectAIScorer + inspect_ai's model layer) — the
# production path. "custom" uses the direct-Anthropic judge in faithfulness.py.
SCORER_BACKEND = "inspect"  # "inspect" | "custom"
FAILURE_BELOW = 0.6  # normalized faithfulness below this → failure cohort
MAX_TURNS = 100
# The change under test: a system prompt that pushes the agent to only state
# what the tool results support. The experiment asks whether it RESCUES the
# low-faithfulness turns without regressing the faithful ones.
CANDIDATE_SYSTEM_PROMPT = (
    "You are a coding agent writing a final turn summary. State ONLY what the "
    "tool results below actually support. Do not claim a file was written, a "
    "command succeeded, or a state changed unless a tool result shows it. If "
    "unsure, say so explicitly rather than asserting."
)

_env = json.loads(Path("/home/voseghale/projects/trading/.claude/settings.json").read_text())["env"]
_api = LangfuseAPI(base_url=_env["LANGFUSE_BASE_URL"], username=_env["LANGFUSE_PUBLIC_KEY"], password=_env["LANGFUSE_SECRET_KEY"])
_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _user_message(trace) -> str:
    inp = getattr(trace, "input", None)
    if isinstance(inp, dict):
        return str(inp.get("content", ""))
    return str(inp or "")


def synthesize(user_message: str, tool_results: str, system_prompt: str) -> str:
    """Runner: re-synthesize the final turn summary under the candidate prompt."""
    msg = _client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=700,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": (
                f"User request for this turn:\n{user_message[:3000]}\n\n"
                f"Tool results you observed:\n{tool_results[:8000]}\n\n"
                "Write the final summary of what you did this turn."
            ),
        }],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")


class FaithfulnessSource:
    """Forks agent turns, scores original faithfulness for cohorting, and
    caches the reference (tool results) + original score for the delta_fn."""

    def __init__(self) -> None:
        self.reference: dict[str, str] = {}
        self.user_msg: dict[str, str] = {}
        self.original_norm: dict[str, float] = {}
        self.original_text: dict[str, str] = {}

    def iter_traces(self):
        emitted = 0
        for stub in _api.trace.list(page=1, limit=50).data:
            if not (stub.name or "").startswith("Claude Code - Turn"):
                continue
            if emitted >= MAX_TURNS:
                break
            trace = _api.trace.get(stub.id)
            reference = extract_tool_results(trace)
            response = extract_response(trace)
            orig = score_faithfulness(response, reference, client=_client)
            cohort = "failure" if orig.normalized < FAILURE_BELOW else "baseline"
            self.reference[trace.id] = reference
            self.user_msg[trace.id] = _user_message(trace)
            self.original_norm[trace.id] = orig.normalized
            self.original_text[trace.id] = response
            print(f"  [{cohort:8}] {trace.name}: original faithfulness {orig.score}/5")
            yield RawTrace(
                trace_id=trace.id,
                cohort=cohort,
                user_message=Sensitive(self.user_msg[trace.id], classification="user_content"),
                original_response=Sensitive(response, classification="user_content"),
                metadata=MappingProxyType({}),
            )
            emitted += 1

    def adapter_metadata(self):
        return AdapterMetadata(adapter_id="faithfulness-langfuse", package_version="0.1.0", sdk_version="4.7.1")

    def cluster_key_support(self):
        return ClusterKeySupport(available_keys=())


def make_delta_fn(source: FaithfulnessSource):
    # Inspect AI path: build the whatifd InspectAIScorer once, closing over the
    # per-trace tool-results reference the source cached.
    scorer = build_inspect_scorer(lambda tid: source.reference[tid]) if SCORER_BACKEND == "inspect" else None

    def delta_fn(rt: RawTrace) -> float:
        ref = source.reference[rt.trace_id]
        replayed = synthesize(source.user_msg[rt.trace_id], ref, CANDIDATE_SYSTEM_PROMPT)

        if scorer is not None:
            # Construct the ScoreCase whatifd's Scorer contract consumes; the
            # InspectAIScorer scores BOTH sides (cardinal #10) and returns the
            # delta as JudgeResult.score — exactly what cli_pipeline does.
            case = ScoreCase(
                trace_id=rt.trace_id,
                cohort=rt.cohort,
                input=TraceInput(user_message=source.user_msg[rt.trace_id]),
                original_output=TraceOutput(text=source.original_text[rt.trace_id]),
                replayed_output=ReplayOutput(text=replayed),
            )
            judge = scorer.score(case)
            if judge.score is None:
                raise RuntimeError(f"InspectAIScorer structural failure on {rt.trace_id}")
            print(f"    [inspect] replay {rt.trace_id[:8]}: delta {judge.score:+.2f}")
            return judge.score

        # Custom path (direct-Anthropic judge).
        replayed_score = score_faithfulness(replayed, ref, client=_client)
        delta = replayed_score.normalized - source.original_norm[rt.trace_id]
        print(f"    [custom] replay {rt.trace_id[:8]}: {source.original_norm[rt.trace_id]:.2f} -> {replayed_score.normalized:.2f} (delta {delta:+.2f})")
        return delta

    return delta_fn


def _methodology() -> MethodologyDisclosure:
    return MethodologyDisclosure(
        unit_of_analysis="paired_trace_delta",
        primary_metric="faithfulness",
        primary_endpoints=("failure.faithfulness", "baseline.faithfulness"),
        cohorts=("failure", "baseline"),
        bootstrap=BootstrapMethodDisclosure(
            method="unavailable", resamples=None, seed=None,
            sample_unit="paired_trace_delta", ci_level="0.950", cluster_key=None,
            assumptions=(), unavailable_reason="live demo; small n, deterministic CI not bootstrapped",
        ),
        multiplicity=MultiplicityDisclosure(primary_endpoint_count=2, correction="none", reason="one metric per cohort"),
        judge=JudgeMethodDisclosure(
            scorer="faithfulness-llm-judge", scorer_version="0.1.0",
            judge_provider="anthropic", judge_model=JUDGE_MODEL, judge_model_version=None,
            rendered_prompt_hash="0" * 16, rubric_hash="0" * 16,
            scorer_cache_enabled=False, scorer_cache_mode="off",
            scorer_cache_hits=0, scorer_cache_misses=0,
            reproducibility_addressed=False, reliability_measured=False,
            validity_measured=False, calibration_measured=False, bias_audit_measured=False,
        ),
        effect_size=EffectSizeDisclosure(practical_delta="0.050", practical_delta_source="policy", judge_noise_floor=None),
        per_trace_inference="descriptive_only",
        causal_claim_scope="associated_under_cached_tool_replay",
    )


def _cache_summary() -> CacheSummary:
    return CacheSummary(
        schema_version="v1", key_version="v1", mode="off",
        storage_profile="normalized_result_only", storage_path=".whatifd/cache",
        hits=0, misses=0, writes=0, stale_hits=0, corrupted_entries=0,
        policy=CachePolicySnapshot(mode="off", warn_after_days=30, block_after_days=90, storage_profile="normalized_result_only"),
        policy_violations=(), oldest_hit_age_days=None, models_distribution=MappingProxyType({}),
    )


def _manifest() -> RunManifest:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return RunManifest(
        experiment_id="faithfulness-rescue", started_at=now, finished_at=now, duration_ms=0,
        whatif_version="0.2.1", config_hash="0" * 64, selection_seed=42,
        source="langfuse", target="resynthesize-under-candidate-prompt",
        trust_floor=TrustFloor(), decision_policy=DecisionPolicy(),
        environment=EnvironmentFingerprint(python="3.12", platform="linux", whatif_version="0.2.1"),
    )


def main() -> None:
    print(f"Langfuse: {_env['LANGFUSE_BASE_URL']}  |  judge: {JUDGE_MODEL}  |  scorer: {SCORER_BACKEND}")
    print("Cohorting agent turns by original faithfulness...")
    source = FaithfulnessSource()
    report = run_pipeline(
        source, delta_fn=make_delta_fn(source),
        floor=TrustFloor(), policy=DecisionPolicy(),
        runtime=_manifest(), methodology=_methodology(), cache_summary=_cache_summary(),
    )
    assert_no_unredacted_sensitive(report)

    print(f"\nVERDICT: {report.verdict_state}")
    for c in report.cohort_results:
        print(f"  {c.name}: selected={c.selected} scored={c.scored} median_delta={c.median_delta} "
              f"floor_passed={c.floor_passed}")
    for f in report.decision_findings:
        print(f"  finding [{getattr(f, 'severity', '?')}] {getattr(f, 'code', '?')}: {getattr(f, 'message', '')[:120]}")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    out = Path(__file__).resolve().parent.parent / "reports" / f"faithfulness-rescue-{stamp}.json"
    out.write_text(json.dumps(json.loads(encode_report_v01(report)), indent=2))
    print(f"report: {out}")


if __name__ == "__main__":
    main()
