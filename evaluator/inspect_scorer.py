"""Inspect AI integration: the faithfulness ruler as a whatifd `InspectAIScorer`.

This is the production-shaped scorer slot (contrast `faithfulness.py`, which
calls Anthropic directly). Two things make it the "real" Inspect AI path:

  1. The judge call goes through Inspect AI's provider-agnostic model layer
     (`inspect_ai.model.get_model("anthropic/...")`) instead of the raw
     Anthropic SDK — so swapping judge providers is a one-string change.
  2. The scorer is wrapped in `whatifd_inspect_ai.InspectAIScorer`, the
     adapter whatifd's pipeline consumes. Its `score_fn` returns an
     `inspect_ai.scorer.Score` whose **value is the faithfulness DELTA**
     (replayed − original) — which whatifd reads as the per-trace delta
     (`cli_pipeline.build_delta_fn` returns `JudgeResult.score`).

The reference (tool results) is not carried on `ScoreCase`, so the scorer
closes over a `reference_of(trace_id)` lookup the harness populates while it
forks traces.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable

from inspect_ai.model import get_model
from inspect_ai.scorer import Score

from faithfulness import JUDGE_MODEL, RUBRIC  # reuse the exact same rubric

from whatifd.contract import ScoreCase
from whatifd_inspect_ai import InspectAIScorer

INSPECT_MODEL = f"anthropic/{JUDGE_MODEL}"  # inspect_ai provider/model syntax

_model = None


def _model_handle():
    global _model
    if _model is None:
        _model = get_model(INSPECT_MODEL)
    return _model


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"judge returned no JSON: {text[:150]!r}")
    return json.loads(m.group(0))


async def _judge_norm(response: str, reference: str) -> tuple[float, str]:
    """Faithfulness of `response` vs `reference`, normalized 1-5 -> [0,1],
    via Inspect AI's model layer."""
    out = await _model_handle().generate(
        RUBRIC.format(reference=reference[:8000], response=response[:8000])
    )
    data = _parse_json(out.completion)
    s = max(1, min(5, int(data["score"])))
    return (s - 1) / 4, str(data.get("reasoning", ""))


def build_inspect_scorer(reference_of: Callable[[str], str]) -> InspectAIScorer:
    """Construct the whatifd `InspectAIScorer`. `reference_of` maps a
    trace_id to the tool-results reference for that turn."""

    def score_fn(case: ScoreCase) -> Score:
        reference = reference_of(case.trace_id)

        async def _both() -> tuple[float, float, str, str]:
            orig, orig_why = await _judge_norm(case.original_output.text, reference)
            repl, repl_why = await _judge_norm(case.replayed_output.text, reference)
            return orig, repl, orig_why, repl_why

        orig, repl, _orig_why, repl_why = asyncio.run(_both())
        delta = repl - orig
        return Score(
            value=delta,
            explanation=f"faithfulness {orig:.2f} -> {repl:.2f} (delta {delta:+.2f}); replayed: {repl_why[:120]}",
        )

    return InspectAIScorer(
        score_fn=score_fn,
        judge_provider="anthropic",
        judge_model_id=JUDGE_MODEL,
        rubric_id="faithfulness-v1",
        rubric_text=RUBRIC,
    )


# --------------------------------------------------------------------------
# Config-loadable score_fn (the production `whatifd fork` path)
# --------------------------------------------------------------------------
# `whatifd.config.yaml` references this as
#   scorer:
#     adapter: inspect_ai
#     score_fn: python:inspect_scorer:score_fn
# Unlike `build_inspect_scorer` (which closes over the harness's reference
# map), this standalone score_fn RE-FETCHES the tool-results reference from
# Langfuse by `case.trace_id` — because whatifd's config path gives the
# scorer only the ScoreCase (trace_id + outputs), not the reference
# (RawTrace.metadata is dropped and the v0.2 ToolCache is empty). The scorer
# DOES carry trace_id, which is the hook that makes this liftable.
#
# Env: LANGFUSE_HOST (or LANGFUSE_BASE_URL) + LANGFUSE_PUBLIC_KEY +
# LANGFUSE_SECRET_KEY + ANTHROPIC_API_KEY.

_lf_api = None


def _langfuse():
    global _lf_api
    if _lf_api is None:
        import os

        from langfuse.api import LangfuseAPI

        host = os.environ.get("LANGFUSE_HOST") or os.environ["LANGFUSE_BASE_URL"]
        _lf_api = LangfuseAPI(
            base_url=host,
            username=os.environ["LANGFUSE_PUBLIC_KEY"],
            password=os.environ["LANGFUSE_SECRET_KEY"],
        )
    return _lf_api


def score_fn(case: ScoreCase) -> Score:
    """Config-loadable faithfulness scorer. Re-fetches the tool-results
    reference for `case.trace_id` from Langfuse, then returns the
    faithfulness delta (replayed - original) as an inspect_ai Score."""
    from faithfulness import extract_tool_results

    reference = extract_tool_results(_langfuse().trace.get(case.trace_id))

    async def _both() -> tuple[float, float]:
        orig, _ = await _judge_norm(case.original_output.text, reference)
        repl, _ = await _judge_norm(case.replayed_output.text, reference)
        return orig, repl

    orig, repl = asyncio.run(_both())
    return Score(value=repl - orig, explanation=f"faithfulness {orig:.2f} -> {repl:.2f}")
