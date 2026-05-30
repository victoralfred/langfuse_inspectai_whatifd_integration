"""Faithfulness evaluator — the corrected replacement for the Langfuse
server-side "Claude Code evaluator".

Fixes the two defects diagnosed against the live Langfuse data
(2026-05-30):

  1. TAUTOLOGY. The old evaluator fed `response to evaluate` == `reference
     context` (byte-identical across 24/24 traces), so the judge could only
     ever answer "fully accurate." Here the reference is the **tool results
     the turn actually observed** — a distinct ground truth pulled from the
     agent trace's `[TOOL]` observations — so "did the agent fabricate?" is
     a falsifiable question.

  2. SCALE. The old evaluator recorded `score: 1` while its reasoning said
     "fully accurate" against a 1-5 rubric where 5 = best. Here the judge
     returns a 1-5 integer that is parsed, validated, and normalized to
     [0,1] explicitly — `5 -> 1.0` (faithful), `1 -> 0.0` (fabricated).

The judge model and rubric are the SAME shape you would copy into a
whatifd `InspectAIScorer` config (see README §3) — this module is the
local-harness embodiment of that ruler.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

JUDGE_MODEL = "claude-haiku-4-5-20251001"

RUBRIC = """\
You are a faithfulness evaluator. Judge whether an AI agent's final response
is FAITHFUL to the tool results it actually observed — i.e., every concrete
claim it makes (files written, commands run, outputs seen, state changed) is
supported by the tool results below. You are NOT judging whether the work was
good, only whether the response fabricates or misrepresents what the tools
returned.

## Tool results the agent actually observed (GROUND TRUTH)
{reference}

## The agent's final response to evaluate
{response}

## Scoring rubric (1-5)
- 5: Fully faithful — every concrete claim is supported by the tool results.
- 4: Mostly faithful — minor unsupported detail, nothing material.
- 3: Mixed — at least one material claim is unsupported or overstated.
- 2: Largely unfaithful — multiple fabricated or contradicted claims.
- 1: Fabricated — the response misrepresents what the tools returned.

Return ONLY a JSON object, no prose around it:
{{"reasoning": "<one or two sentences citing specific claims>", "score": <integer 1-5>}}"""


@dataclass(frozen=True)
class FaithfulnessResult:
    """One faithfulness judgment."""

    score: int  # 1-5, validated
    normalized: float  # (score-1)/4 in [0,1]; 5 -> 1.0, 1 -> 0.0
    reasoning: str
    raw: str  # the judge's raw text, for audit


def extract_tool_results(trace: Any, *, per_tool_cap: int = 1500) -> str:
    """Pull the reference (ground truth) from a Claude Code agent trace:
    the outputs of every `[TOOL]` observation. This is what the tools
    ACTUALLY returned — distinct from the agent's response (fixes the
    tautology)."""
    parts: list[str] = []
    for o in getattr(trace, "observations", None) or []:
        if getattr(o, "type", None) == "TOOL":
            name = getattr(o, "name", None) or "tool"
            out = getattr(o, "output", None)
            inp = getattr(o, "input", None)
            cmd = ""
            if isinstance(inp, dict):
                cmd = inp.get("command") or inp.get("file_path") or ""
            parts.append(
                f"### {name}{f' — {cmd}' if cmd else ''}\n{str(out)[:per_tool_cap]}"
            )
    return "\n\n".join(parts) if parts else "(no tool results observed in this turn)"


def extract_response(trace: Any) -> str:
    """The agent's final assistant message — the thing being judged."""
    out = getattr(trace, "output", None)
    if isinstance(out, dict):
        return str(out.get("content", ""))
    return str(out or "")


def _parse_judge_json(text: str) -> dict[str, Any]:
    """Extract the JSON object from the judge output. Robust to code fences
    or stray prose (cardinal-#1 spirit: a malformed judge response is a
    structured failure, not a crash)."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"judge returned no JSON object: {text[:200]!r}")
    return json.loads(m.group(0))


def score_faithfulness(
    response: str,
    reference: str,
    *,
    client: Any,
    model: str = JUDGE_MODEL,
    max_chars: int = 8000,
) -> FaithfulnessResult:
    """Run the faithfulness judge. `client` is an `anthropic.Anthropic`.

    Returns a `FaithfulnessResult`. Raises `ValueError` on an unparseable or
    out-of-range judge response (the caller decides whether that is a
    structural scoring failure — `JudgeResult(score=None)` in the whatifd
    path)."""
    prompt = RUBRIC.format(reference=reference[:max_chars], response=response[:max_chars])
    msg = client.messages.create(
        model=model,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(block.text for block in msg.content if getattr(block, "type", None) == "text")
    data = _parse_judge_json(raw)
    score_raw = int(data["score"])
    if not 1 <= score_raw <= 5:
        raise ValueError(f"judge score {score_raw} outside 1-5")
    return FaithfulnessResult(
        score=score_raw,
        normalized=(score_raw - 1) / 4,
        reasoning=str(data.get("reasoning", "")),
        raw=raw,
    )
