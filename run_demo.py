#!/usr/bin/env python
"""Single-command demo of the whole whatifd faithfulness-rescue stack.

Runs three stages as one pipeline and prints the result:

  STAGE 0  whatifd 0.2.1 compatibility (offline)
           Load `whatifd.config.yaml` and build the scorer THROUGH whatifd's
           own 0.2.1 validators (experiment_shape<->selection cross-check,
           generalized `python:` loader). Proves the config still lifts.

  STAGE 1  v2 scorer-cache keying proof (offline, deterministic)
           Runs `probes/probe_scorer_cache.py`: a re-score after the replayed
           output changes MISSES under v0.2.1's v2 keying (F-2.1) instead of
           returning the stale v1-collision delta. No network.

  STAGE 2  faithfulness rescue, cache ENABLED (live)
           Runs `harness/whatifd_run.py` end-to-end against Langfuse +
           Anthropic with the scorer cache turned on, then prints the verdict
           and the live cache tally. Skipped if creds are absent or --offline.

Usage:
    ./.venv/bin/python run_demo.py            # all stages (live if creds present)
    ./.venv/bin/python run_demo.py --offline  # stages 0 + 1 only
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Make the project's modules importable the same way the harness expects
# (PYTHONPATH=evaluator:harness), so the demo runs as a plain `python run_demo.py`.
sys.path[:0] = [str(ROOT / "evaluator"), str(ROOT / "harness"), str(ROOT / "probes")]


def _banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def stage0_compatibility() -> None:
    _banner("STAGE 0 — whatifd 0.2.1 compatibility (offline)")
    from whatifd.adapters.factory import build_scorer
    from whatifd.config import load_config

    cfg = load_config(ROOT / "whatifd.config.yaml")
    scorer = build_scorer(cfg.scorer)
    print(f"  load_config() passed the 0.2.1 experiment_shape<->selection validator")
    print(f"    experiment_shape = {cfg.experiment_shape}")
    print(f"    source.adapter   = {cfg.source.adapter}  (spans_provider={cfg.source.spans_provider})")
    print(f"  build_scorer() resolved python:inspect_scorer:score_fn -> {type(scorer).__name__}")
    print(f"    rubric_id = {cfg.scorer.rubric_id}")
    print("  RESULT: config + scorer lift cleanly under 0.2.1")


def stage1_cache_proof() -> None:
    _banner("STAGE 1 — v2 scorer-cache keying proof (offline, deterministic)")
    import probe_scorer_cache  # runs its assertions; SystemExit on any failure

    probe_scorer_cache.main()
    print("  RESULT: v2 keying prevents the F-2.1 stale-collision (all checks passed)")


def _have_creds() -> bool:
    anthropic_ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
    langfuse_ok = bool(os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST"))
    # The harness can also source Langfuse creds from its settings.json.
    settings = Path("/home/voseghale/projects/trading/.claude/settings.json")
    return anthropic_ok and (langfuse_ok or settings.exists())


def stage2_live() -> None:
    _banner("STAGE 2 — faithfulness rescue pipeline, cache ENABLED (live)")
    import whatifd_run

    # Flip the scorer cache on for the demo so scoring rides the v2 cache
    # path; the harness reports the live cache tally + writes the report.
    whatifd_run.SCORER_CACHE_MODE = "on"
    whatifd_run.main()
    print("  RESULT: end-to-end verdict produced with the v2 scorer cache wired in")


def main() -> None:
    offline = "--offline" in sys.argv
    _banner("whatifd faithfulness-rescue — DEMO")
    print("  whatifd 0.2.1 | scorer cache: on (live stage) | judge: claude-haiku-4-5")

    stage0_compatibility()
    stage1_cache_proof()

    if offline:
        _banner("STAGE 2 skipped (--offline)")
    elif _have_creds():
        stage2_live()
    else:
        _banner("STAGE 2 skipped — no creds")
        print("  Set ANTHROPIC_API_KEY + LANGFUSE_BASE_URL/PUBLIC_KEY/SECRET_KEY to run the")
        print("  live pipeline, or pass --offline to acknowledge the offline-only run.")

    _banner("DEMO COMPLETE")


if __name__ == "__main__":
    main()
