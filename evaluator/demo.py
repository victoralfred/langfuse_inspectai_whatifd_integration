"""Validate the corrected faithfulness evaluator on real agent turns.

Proves the fix: reference (tool results) is distinct from the response, and
the judge produces a real 1-5 score (not the old constant 1). Run:
    ./.venv/bin/python evaluator/demo.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import anthropic
from langfuse.api import LangfuseAPI

from faithfulness import extract_response, extract_tool_results, score_faithfulness

_env = json.loads(Path("/home/voseghale/projects/trading/.claude/settings.json").read_text())["env"]
api = LangfuseAPI(
    base_url=_env["LANGFUSE_BASE_URL"],
    username=_env["LANGFUSE_PUBLIC_KEY"],
    password=_env["LANGFUSE_SECRET_KEY"],
)
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

turns = [t for t in api.trace.list(page=1, limit=50).data if (t.name or "").startswith("Claude Code - Turn")]
print(f"agent turns: {len(turns)}\n")

for stub in turns:
    trace = api.trace.get(stub.id)
    reference = extract_tool_results(trace)
    response = extract_response(trace)
    # Prove they are distinct (the old evaluator's bug was reference == response).
    distinct = reference.strip()[:200] != response.strip()[:200]
    print(f"===== {trace.name} =====")
    print(f"reference (tool results): {len(reference)} chars | response: {len(response)} chars | distinct={distinct}")
    result = score_faithfulness(response, reference, client=client)
    print(f"  faithfulness score: {result.score}/5  (normalized {result.normalized:.2f})")
    print(f"  reasoning: {result.reasoning}")
    print()
