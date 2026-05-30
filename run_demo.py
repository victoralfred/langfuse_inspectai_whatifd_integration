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

  STAGE 2  faithfulness rescue (live)
           Runs `harness/whatifd_run.py` end-to-end against Langfuse +
           Anthropic, then prints the verdict and (for the inspect judge) the
           live cache tally. Skipped if creds are absent or --offline.

The judge backend for the scorer slot is selectable:
  --judge inspect    Inspect AI's model layer (whatifd_inspect_ai). Default.
  --judge anthropic  the raw Anthropic SDK judge in faithfulness.py.

Usage:
    ./.venv/bin/python run_demo.py                      # inspect judge, cache on
    ./.venv/bin/python run_demo.py --judge anthropic    # raw-Anthropic judge
    ./.venv/bin/python run_demo.py --cache off          # disable the scorer cache
    ./.venv/bin/python run_demo.py --offline            # stages 0 + 1 only
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Make the project's modules importable the same way the harness expects
# (PYTHONPATH=evaluator:harness:probes), so the demo runs as a plain
# `python run_demo.py`.
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
    print("  load_config() passed the 0.2.1 experiment_shape<->selection validator")
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


def stage2_live(judge: str, cache_mode: str) -> None:
    _banner("STAGE 2 — faithfulness rescue pipeline (live)")
    import whatifd_run

    # Select the scorer-slot judge backend and cache mode at runtime. Both
    # are read by whatifd_run.main()/make_delta_fn at call time.
    whatifd_run.SCORER_BACKEND = judge          # "inspect" | "anthropic"
    whatifd_run.SCORER_CACHE_MODE = cache_mode   # off|on|auto|read_only|refresh
    print(f"  judge backend = {judge}  |  scorer cache = {cache_mode}")
    if judge == "anthropic" and cache_mode != "off":
        # whatifd's scorer cache wraps a whatifd Scorer (the InspectAIScorer).
        # The raw-Anthropic delta path is not a whatifd Scorer, so the cache
        # does not engage for it — the run reports cache off, by design.
        print("  note: the scorer cache applies to the inspect judge only; the raw-"
              "Anthropic\n        path does not go through a whatifd Scorer, so the "
              "cache stays off.")
    whatifd_run.main()
    print("  RESULT: end-to-end verdict produced via the selected judge backend")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="whatifd faithfulness-rescue demo (Langfuse source + selectable judge)."
    )
    p.add_argument(
        "--judge",
        choices=["inspect", "anthropic"],
        default="inspect",
        help="judge backend for the scorer slot: Inspect AI's model layer "
        "(default) or the raw Anthropic SDK.",
    )
    p.add_argument(
        "--cache",
        choices=["off", "on", "auto", "read_only", "refresh"],
        default="on",
        help="scorer-cache mode for the live stage (default: on; effective only "
        "with --judge inspect).",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="run only the offline stages (0 + 1); skip the live pipeline.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    _banner("whatifd faithfulness-rescue — DEMO")
    print(f"  whatifd 0.2.1 | source: langfuse | judge: {args.judge} | "
          f"cache: {args.cache} | judge model: claude-haiku-4-5")

    stage0_compatibility()
    stage1_cache_proof()

    if args.offline:
        _banner("STAGE 2 skipped (--offline)")
    elif _have_creds():
        stage2_live(args.judge, args.cache)
    else:
        _banner("STAGE 2 skipped — no creds")
        print("  Set ANTHROPIC_API_KEY + LANGFUSE_BASE_URL/PUBLIC_KEY/SECRET_KEY to run the")
        print("  live pipeline, or pass --offline to acknowledge the offline-only run.")

    _banner("DEMO COMPLETE")


if __name__ == "__main__":
    main()
