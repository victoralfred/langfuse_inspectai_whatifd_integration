"""Score distribution across the evaluator traces + LLM-key availability.

Determines whether the data contains a real `failure` cohort (scores below
threshold) or is degenerate (all-pass), which decides whether failure_rescue
is even applicable vs regression_check.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

_env = json.loads(Path("/home/voseghale/projects/trading/.claude/settings.json").read_text())["env"]
from langfuse.api import LangfuseAPI

api = LangfuseAPI(
    base_url=_env["LANGFUSE_BASE_URL"],
    username=_env["LANGFUSE_PUBLIC_KEY"],
    password=_env["LANGFUSE_SECRET_KEY"],
)

traces = list(api.trace.list(page=1, limit=50).data)

evaluator = [t for t in traces if (getattr(t, "output", None) or {}).get("score") is not None \
             if isinstance(getattr(t, "output", None), dict)]
print(f"total traces: {len(traces)}")
print(f"evaluator traces (output.score present): {len(evaluator)}")

scores = [t.output["score"] for t in evaluator]
print(f"\nscore value distribution: {dict(Counter(scores))}")
print(f"min={min(scores)} max={max(scores)} distinct={sorted(set(scores))}")

# Bucket at a few candidate thresholds.
for thr in (0.5, 0.6, 0.8, 1.0):
    fails = sum(1 for s in scores if s < thr)
    print(f"  threshold {thr}: failure={fails}  baseline={len(scores)-fails}")

# Score type — int vs float?
print(f"\nscore python types: {Counter(type(s).__name__ for s in scores)}")

# --- LLM key availability (for a real replay) ------------------------------
print("\n--- LLM keys available? ---")
for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_API_KEY"):
    in_settings = k in _env
    in_os = bool(os.environ.get(k))
    print(f"  {k}: in trading settings={in_settings}  in os.environ={in_os}")
print(f"\nall trading-settings env keys: {sorted(_env.keys())}")
