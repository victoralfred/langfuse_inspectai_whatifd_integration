"""Decision pipeline: floor evaluation, guard chain, verdict computation.

This package implements cardinal rules #1 (failure-as-data), #2 (trust
floor cannot be bypassed), and #8 (Inconclusive must be actionable).

Phase ordering (per `.claude/skills/whatifd-design/phases.md`):
  1.4 verdict (this Phase) — `decision/floor.py` lands the closure-captured
                             FloorPassedProof witness; full evaluator body
                             arrives in Phase 2.1.
  2.1 floor evaluator      — replaces the Phase 1.4 stub with real rule
                             evaluation against ExperimentResult and
                             TrustFloor.
  2.2-2.7                  — failure code registry, finding code registry,
                             fix-suggestion registry (cardinal #8), guard
                             chain, verdict computation, aggregation.
"""
