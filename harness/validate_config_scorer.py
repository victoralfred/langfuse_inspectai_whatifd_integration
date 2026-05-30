"""Validate that the Inspect AI scorer LIFTS into the YAML config — i.e. that
whatifd's own machinery (load_config -> build_scorer) constructs the scorer
from `whatifd.config.yaml` and scores a real case, with the score_fn
re-fetching the tool-results reference from Langfuse by trace_id.

Proves the production scorer path end-to-end without needing the (blocked)
runner/cohorting slots. Run:
    PYTHONPATH=evaluator:harness ./.venv/bin/python harness/validate_config_scorer.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "evaluator"))  # so python:inspect_scorer:score_fn resolves

# Map the trading project's Langfuse creds into the env the score_fn reads.
_env = json.loads(Path("/home/voseghale/projects/trading/.claude/settings.json").read_text())["env"]
os.environ["LANGFUSE_HOST"] = _env["LANGFUSE_BASE_URL"]
os.environ["LANGFUSE_BASE_URL"] = _env["LANGFUSE_BASE_URL"]
os.environ["LANGFUSE_PUBLIC_KEY"] = _env["LANGFUSE_PUBLIC_KEY"]
os.environ["LANGFUSE_SECRET_KEY"] = _env["LANGFUSE_SECRET_KEY"]

from langfuse.api import LangfuseAPI

from whatifd.adapters.factory import build_scorer
from whatifd.config import load_config
from whatifd.contract import ReplayOutput, ScoreCase, TraceInput, TraceOutput

# 1. whatifd loads the YAML and builds the scorer from scorer.adapter=inspect_ai.
cfg = load_config(_ROOT / "whatifd.config.yaml")
scorer = build_scorer(cfg.scorer)
print(f"config scorer.adapter = {cfg.scorer.adapter!r}")
print(f"build_scorer(...) -> {type(scorer).__name__}  (rubric_id={cfg.scorer.rubric_id})")

# 2. Build a real ScoreCase against a live agent turn (the score_fn will
#    re-fetch that turn's tool results as the reference).
api = LangfuseAPI(base_url=os.environ["LANGFUSE_HOST"], username=os.environ["LANGFUSE_PUBLIC_KEY"], password=os.environ["LANGFUSE_SECRET_KEY"])
_turns = [t for t in api.trace.list(page=1, limit=50).data if (t.name or "").startswith("Claude Code - Turn")]
if not _turns:
    raise SystemExit("no 'Claude Code - Turn' agent traces on page 1 to score against")
turn = _turns[0]
print(f"scoring against live agent turn: {turn.name} ({turn.id[:8]})")
full = api.trace.get(turn.id)
original = full.output.get("content", "") if isinstance(full.output, dict) else str(full.output)

case = ScoreCase(
    trace_id=turn.id,
    cohort="failure",
    input=TraceInput(user_message="(unused by the faithfulness scorer)"),
    original_output=TraceOutput(text=original),
    replayed_output=ReplayOutput(
        text="Done. I created only the files the tool results confirm were written, and I made no claims the tool outputs do not support."
    ),
)

# 3. whatifd's scorer scores it — score_fn re-fetches the reference internally.
result = scorer.score(case)
print(f"\nscorer.score(case) -> JudgeResult.score (the delta) = {result.score:+.2f}")
print(f"rationale: {str(result.rationale)[:200]}")
print("\nOK — the Inspect AI scorer + rubric are fully config-loadable via whatifd.")
