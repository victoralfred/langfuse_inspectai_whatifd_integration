"""Scorer-result caching on whatifd's v2 cache-keying path.

whatifd's `whatifd fork` CLI ships the scorer cache DISABLED: `cli.py`
emits a `mode="off"` `CacheSummary` and notes the cache subsystem is
"exercised programmatically by callers that want it" (the Phase 10.5
TODO). This module is that caller. `CachedScorer` wraps any whatifd
`Scorer` — anything exposing `.score(case)` + `.cache_key_components(
case)` + `.adapter_metadata()` — with whatifd's own storage + keying
primitives, turning a score into:

    components = inner.cache_key_components(case)   # v2 CacheKeyComponents
    key        = build_cache_key(components)        # "v2:<sha256>"
    entry      = read_entry(root, key)              # storage lookup
    ...on miss: inner.score(case) -> write_entry(root, key, entry)

Why this exercises the v0.2.1 fix: the v2 `CacheKeyComponents` adds
`replayed_output_hash` (F-2.1). Re-scoring a trace after its replayed
output changes — same trace_id / cohort / input, different replayed
text — now yields a DIFFERENT key, so the lookup MISSES and re-scores.
Under v1 keying those two calls collided on one key and the second
returned the first call's stale delta (a silent wrong verdict).
`probes/probe_scorer_cache.py` asserts that property offline.

Modes mirror `whatifd.types.policy.ScorerCacheMode`
(`off` / `on` / `auto` / `read_only` / `refresh`):
  - off        : bypass the cache entirely (passthrough to inner).
  - on / auto  : read existing entries; on miss score and persist.
  - read_only  : read existing entries; on miss score but DO NOT write.
  - refresh    : ignore existing entries (always re-score) and overwrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from whatifd.adapters.protocols import JudgeResult
from whatifd.cache.keying import CACHE_KEY_VERSION, build_cache_key
from whatifd.cache.storage import (
    CACHE_SCHEMA_VERSION,
    CacheEntry,
    CacheResult,
    CacheSchemaMismatchError,
    init_cache,
    read_entry,
    write_entry,
)
from whatifd.cache.summary import CachePolicySnapshot, CacheSummary
from whatifd.contract import ScoreCase
from whatifd.types.sensitive import Sensitive

# Which modes consult / persist on-disk entries. `auto` behaves as `on`
# (the harness does not run whatifd's `resolve_cache_mode` policy
# resolver; it opts in explicitly, so auto == read+write here).
_READ_MODES = {"on", "auto", "read_only"}
_WRITE_MODES = {"on", "auto", "refresh"}
_VALID_MODES = {"off", "on", "auto", "read_only", "refresh"}


def _fmt_delta(value: float) -> str:
    """Fixed-precision decimal string so the score round-trips byte-stably
    through the cache (cardinal #4 determinism). `float(_fmt_delta(x))`
    recovers the value the pipeline reads as the per-trace delta."""
    return f"{value:.10f}"


@dataclass
class CacheCounters:
    """Live tally accumulated across `CachedScorer.score` calls."""

    hits: int = 0
    misses: int = 0
    writes: int = 0
    stale_hits: int = 0  # reserved (no TTL policy at this layer)
    corrupted_entries: int = 0
    score_calls: int = 0  # times the INNER scorer actually ran (judge calls)
    models_distribution: dict[str, int] = field(default_factory=dict)

    def tally(self) -> str:
        return (
            f"hits={self.hits} misses={self.misses} writes={self.writes} "
            f"corrupted={self.corrupted_entries} judge_calls={self.score_calls}"
        )


class CachedScorer:
    """Wrap a whatifd `Scorer` with content-addressed result caching.

    Satisfies the same surface the pipeline / harness use on a scorer
    (`score`, `cache_key_components`, `adapter_metadata`), so it drops
    in transparently wherever an `InspectAIScorer` was used.
    """

    def __init__(
        self,
        inner,
        *,
        cache_root: Path | str,
        mode: str = "on",
        store_rationale: bool = False,
    ) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"cache mode {mode!r} not in {sorted(_VALID_MODES)}")
        self.inner = inner
        self.mode = mode
        self.cache_root = Path(cache_root)
        self.store_rationale = store_rationale
        self.counters = CacheCounters()
        if mode != "off":
            # Idempotent; writes meta.json recording the storage + key
            # versions. Raises CacheSchemaMismatchError if an existing
            # cache at this root was built with a different storage schema.
            init_cache(self.cache_root)

    # --- Scorer-shape passthroughs ---------------------------------------
    def cache_key_components(self, case: ScoreCase):
        return self.inner.cache_key_components(case)

    def adapter_metadata(self):
        return self.inner.adapter_metadata()

    # --- Cached scoring ---------------------------------------------------
    def score(self, case: ScoreCase) -> JudgeResult:
        if self.mode == "off":
            self.counters.score_calls += 1
            return self.inner.score(case)

        components = self.inner.cache_key_components(case)
        key = build_cache_key(components)  # "v2:<digest>"

        if self.mode in _READ_MODES:
            try:
                entry = read_entry(self.cache_root, key)
            except CacheSchemaMismatchError:
                # On-disk entry is corrupted or version-skewed: a DATA
                # condition (cardinal #1), not a crash. Count it and
                # fall through to a re-score.
                self.counters.corrupted_entries += 1
                entry = None
            if entry is not None:
                self.counters.hits += 1
                model = entry.key_components.judge_model_id
                self.counters.models_distribution[model] = (
                    self.counters.models_distribution.get(model, 0) + 1
                )
                return self._reconstruct(case, entry)

        # Miss (or refresh): run the real scorer.
        self.counters.misses += 1
        self.counters.score_calls += 1
        result = self.inner.score(case)

        # Never persist a structural failure (score is None) — re-scoring
        # next run is the correct behavior, matching v2's miss-on-skew.
        if self.mode in _WRITE_MODES and result.score is not None:
            self._persist(key, components, result)
            self.counters.writes += 1
        return result

    def _persist(self, key: str, components, result: JudgeResult) -> None:
        rationale: str | None = None
        if self.store_rationale and result.rationale is not None:
            rationale = result.rationale.unwrap(
                reason="persist judge rationale to local scorer cache (full_judge_io profile)"
            )
        entry = CacheEntry(
            cache_key_version=CACHE_KEY_VERSION,
            cache_schema_version=CACHE_SCHEMA_VERSION,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            key_components=components,
            result=CacheResult(
                score_delta=_fmt_delta(float(result.score)),
                # verdict/confidence are pipeline-level concepts; at the
                # scorer boundary the payload is the faithfulness delta.
                verdict="n/a",
                confidence="n/a",
                rationale=rationale,
            ),
        )
        write_entry(self.cache_root, key, entry)

    def _reconstruct(self, case: ScoreCase, entry: CacheEntry) -> JudgeResult:
        kc = entry.key_components
        text = entry.result.rationale or (
            "cache hit (rationale not persisted under normalized_result_only profile)"
        )
        return JudgeResult(
            trace_id=case.trace_id,
            score=float(entry.result.score_delta),
            rationale=Sensitive(text, classification="judge_rationale"),
            judge_model_id=kc.judge_model_id,
            judge_model_snapshot=kc.judge_model_snapshot,
        )

    # --- Reporting --------------------------------------------------------
    def cache_summary(self) -> CacheSummary:
        """Truthful `CacheSummary` built from the live counters. key_version
        reflects the installed keying module (v2), so an enabled run reports
        the v2 key version the entries were actually addressed under."""
        c = self.counters
        profile = "full_judge_io" if self.store_rationale else "normalized_result_only"
        return CacheSummary(
            schema_version=CACHE_SCHEMA_VERSION,
            key_version=CACHE_KEY_VERSION,
            mode=self.mode,
            storage_profile=profile,
            storage_path=str(self.cache_root),
            hits=c.hits,
            misses=c.misses,
            writes=c.writes,
            stale_hits=c.stale_hits,
            corrupted_entries=c.corrupted_entries,
            policy=CachePolicySnapshot(
                mode=self.mode,
                warn_after_days=30,
                block_after_days=90,
                storage_profile=profile,
            ),
            policy_violations=(),
            oldest_hit_age_days=None,
            models_distribution=MappingProxyType(dict(c.models_distribution)),
        )
