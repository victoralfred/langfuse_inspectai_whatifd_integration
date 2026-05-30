"""Offline proof that the scorer cache rides whatifd's v0.2.1 v2 keying
path AND that the F-2.1 fix (replayed-output in the key) prevents the
silent stale-collision the v1 keying had.

No network, no API keys, deterministic. Uses a fake `score_fn` (counts
invocations, returns a value derived from the replayed text) wrapped in
the REAL `InspectAIScorer` — so the cache keys are computed by the real
v2 `cache_key_components`, not a stand-in.

Run:
    PYTHONPATH=evaluator ./.venv/bin/python probes/probe_scorer_cache.py

Exits non-zero if any invariant fails.
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluator"))

from cached_scorer import CachedScorer  # noqa: E402

from whatifd.cache.keying import build_cache_key  # noqa: E402
from whatifd.contract import (  # noqa: E402
    ReplayOutput,
    ScoreCase,
    TraceInput,
    TraceOutput,
)
from whatifd_inspect_ai import InspectAIScorer  # noqa: E402


class _FakeScore:
    """Minimal stand-in for inspect_ai.scorer.Score (value + explanation),
    which is all `InspectAIScorer._project_score` reads."""

    def __init__(self, value: float, explanation: str) -> None:
        self.value = value
        self.explanation = explanation


def _make_scorer() -> tuple[InspectAIScorer, dict]:
    state = {"calls": 0, "seen": []}

    def fake_score_fn(case: ScoreCase) -> _FakeScore:
        state["calls"] += 1
        state["seen"].append(case.replayed_output.text)
        # Value derived from the replayed text so a wrong (stale) hit on a
        # changed replayed output would surface as the WRONG delta.
        value = 0.25 if case.replayed_output.text == "REPLAY-A" else 0.75
        return _FakeScore(value, explanation=f"scored {case.replayed_output.text}")

    scorer = InspectAIScorer(
        score_fn=fake_score_fn,
        judge_provider="anthropic",
        judge_model_id="claude-haiku-4-5-20251001",
        rubric_id="faithfulness-v1",
        rubric_text="(probe rubric)",
    )
    return scorer, state


def _case(replayed: str) -> ScoreCase:
    return ScoreCase(
        trace_id="T-1",
        cohort="failure",
        input=TraceInput(user_message="same user message"),
        original_output=TraceOutput(text="ORIGINAL"),
        replayed_output=ReplayOutput(text=replayed),
    )


def _check(label: str, ok: bool) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        raise SystemExit(f"probe failed: {label}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        scorer, state = _make_scorer()
        cached = CachedScorer(scorer, cache_root=tmp, mode="on")

        case_a = _case("REPLAY-A")
        case_b = _case("REPLAY-B")  # same trace/cohort/input/original; new replay

        key_a = build_cache_key(scorer.cache_key_components(case_a))
        key_b = build_cache_key(scorer.cache_key_components(case_b))

        print("keys:")
        print(f"  case A -> {key_a}")
        print(f"  case B -> {key_b}")
        _check("v2 key prefix on the wire", key_a.startswith("v2:") and key_b.startswith("v2:"))

        # The ONLY component differing between A and B is replayed_output_hash.
        # Equalize it and the keys collapse to one — i.e. that field is what
        # the v0.2.1 F-2.1 fix added, and what stops the stale collision.
        comp_a = scorer.cache_key_components(case_a)
        comp_b = scorer.cache_key_components(case_b)
        _check("replayed_output_hash differs between A and B",
               comp_a.replayed_output_hash != comp_b.replayed_output_hash)
        comp_b_as_a = dataclasses.replace(comp_b, replayed_output_hash=comp_a.replayed_output_hash)
        _check("keys collapse when replayed_output_hash is equalized "
               "(the v1 collision the fix removes)",
               build_cache_key(comp_b_as_a) == key_a)

        # --- exercise the cache ---
        r1 = cached.score(case_a)   # miss -> write, judge runs once
        _check("first score is a miss+write (judge invoked)", state["calls"] == 1)
        _check("r1 delta round-trips through cache encoding", abs(r1.score - 0.25) < 1e-9)

        r2 = cached.score(case_a)   # identical case -> HIT, judge NOT re-run
        _check("re-scoring identical case HITS (judge not re-invoked)", state["calls"] == 1)
        _check("cache hit returns the same delta", abs(r2.score - 0.25) < 1e-9)

        r3 = cached.score(case_b)   # changed replayed text -> MISS under v2
        _check("changed replayed output MISSES under v2 (no stale hit)", state["calls"] == 2)
        _check("re-scored delta reflects the NEW replayed output, not the stale one",
               abs(r3.score - 0.75) < 1e-9)

        c = cached.counters
        _check("counters: hits=1 misses=2 writes=2",
               c.hits == 1 and c.misses == 2 and c.writes == 2)

        summary = cached.cache_summary()
        _check("CacheSummary reports key_version v2", summary.key_version == "v2")
        _check("CacheSummary counters match live tally",
               summary.hits == 1 and summary.misses == 2 and summary.writes == 2)

        print("\nlive cache tally:", c.tally())
        print("CacheSummary:", f"mode={summary.mode} key_version={summary.key_version} "
              f"hits={summary.hits} misses={summary.misses} writes={summary.writes} "
              f"models={dict(summary.models_distribution)}")
        print("\nUnder v1 keying, the third score would have HIT key_a and returned "
              "0.25 (the stale REPLAY-A delta). v2 keying re-scored to 0.75. Fix confirmed.")


if __name__ == "__main__":
    main()
