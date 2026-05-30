"""Cache summary — Phase 3.5.

`CacheSummary` is the typed object that becomes the `cache_summary`
field on `ReportV01`. Closes Phase 3 (cache subsystem) per the v0.1
implementation plan; pairs with Phase 3.1/3.2/3.3/3.4 (keying,
storage, lock, mode resolution).

## Why this object exists

Per cardinal #5 + the enforcement table row "Cache disclosure cannot
be disabled": every `ReportV01` carries a complete record of what the
scorer cache did during the run. A reviewer reading the manifest can
answer:

- Which key/schema versions were active? (`schema_version`,
  `key_version` — versioned-package constants from
  `whatifd.cache.keying.v1`/`storage.v1`).
- What mode did the cache run in? (`mode` — sealed
  `ScorerCacheMode`, resolved by Phase 3.4).
- What storage profile? (`storage_profile` — sealed
  `ScorerCacheStorageProfile`; gates whether `rationale` was stored).
- How did it perform? (`hits`, `misses`, `writes`, `stale_hits`,
  `corrupted_entries`).
- What policy was active and were any policy thresholds tripped?
  (`policy: CachePolicySnapshot`, `policy_violations: tuple[
  PolicyViolationRecord, ...]`).

The schema-validation test in Phase 5 asserts a `ReportV01` cannot be
constructed without a `cache_summary` (required field). Schema's
`required_fields` enforce content, not just presence — every field
above is required at the type level (no `Optional[...]` hiding
unset state behind `None`).

## Required vs optional

Required (always populated, no fallback):
- `schema_version`, `key_version`, `mode`, `storage_profile`,
  `storage_path`, `hits`, `misses`, `writes`, `stale_hits`,
  `corrupted_entries`, `policy`, `policy_violations`.

Optional (typed `... | None`; populated when meaningful):
- `oldest_hit_age_days`: `int | None` — days since the oldest cache
  entry was created, when at least one entry exists.
- `models_distribution`: `Mapping[str, int]` — judge model id → hit
  count when the cache produced hits across multiple judge models;
  empty mapping when no hits or single-model.

`models_distribution` is `Mapping`, not `dict[str, int]` — callers
get an immutable view without sacrificing ergonomics. The
`policy_violations` tuple uses the same immutability pattern.

## Cardinal alignment

- **#1 (failures-as-data):** `policy_violations` is a tuple of typed
  records, NOT free-form strings. Each violation carries `rule`,
  `observed`, `threshold` so renderers and downstream tooling can
  filter/aggregate without parsing.
- **#5 (sensitive data wrapped):** the cache holds judge results;
  rationale text (when stored under `full_judge_io`) is wrapped in
  `Sensitive[T]` at the boundary. `CacheSummary` itself records only
  metadata, never raw rationale.
- **#6 (typed boundaries):** every field is hand-written and typed.
  `policy_violations` is `tuple[PolicyViolationRecord, ...]`, not
  `list[dict[str, Any]]`. `models_distribution` is `Mapping[str,
  int]`, not `dict[str, Any]`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from whatifd.types.policy import ScorerCacheMode, ScorerCacheStorageProfile


@dataclass(frozen=True, slots=True)
class CachePolicySnapshot:
    """The cache-policy fields from `DecisionPolicy` at run time.

    Captured into `CacheSummary` so the manifest carries the policy
    that produced the run's cache behavior — no need to cross-reference
    `DecisionPolicy` separately, no risk of policy mutation between
    run and report. Snapshot semantics: frozen dataclass, value-equal,
    safe to hash and serialize.
    """

    mode: ScorerCacheMode
    warn_after_days: int
    block_after_days: int
    storage_profile: ScorerCacheStorageProfile


@dataclass(frozen=True, slots=True)
class PolicyViolationRecord:
    """One cache-policy violation observed during the run.

    Cardinal #6: structured records, not free-form strings. The
    deferred `cache_staleness_guard` (cascade-tracked) emits these
    when `oldest_hit_age_days > warn_after_days` or `> block_after_days`;
    other policy guards may emit additional violation kinds in v0.2+.

    Fields parallel `FloorFailure`'s shape so renderers can treat
    cache violations and floor failures uniformly:

    - `rule`: the policy field name that was violated (e.g.,
      `scorer_cache_warn_after_days`). Stable across renders.
    - `observed`: the runtime measurement (e.g., `45` for
      "oldest hit is 45 days old"). Typed `int | float | str` to
      handle counts, ratios, and string descriptors uniformly.
    - `threshold`: the policy threshold the observation breached.
      Same union as `observed`.
    """

    rule: str
    observed: int | float | str
    threshold: int | float


@dataclass(frozen=True, slots=True)
class CacheSummary:
    """The `cache_summary` field on `ReportV01`. Required, typed,
    cannot be elided.

    Construction at end of run: the cache subsystem aggregates its
    counters (hits/misses/writes/etc.) and the projection layer
    builds this object before assembling `ReportV01`. Schema
    validation in Phase 5 enforces presence + content.

    Field ordering follows `references/contracts.md` §"Cache
    disclosure content spec" for stability across renders. The
    versioned constants (`schema_version`, `key_version`) are
    duplicated here from the cache-package modules deliberately:
    the manifest is the source of truth for "what version was used,"
    and a future cache-version bump must update the manifest field
    too — duplication makes drift visible.
    """

    # ----- Versioning -----
    schema_version: str
    key_version: str

    # ----- Active configuration -----
    mode: ScorerCacheMode
    storage_profile: ScorerCacheStorageProfile
    storage_path: str

    # ----- Counters -----
    hits: int
    misses: int
    writes: int
    stale_hits: int
    corrupted_entries: int

    # ----- Policy snapshot + violations -----
    policy: CachePolicySnapshot
    policy_violations: tuple[PolicyViolationRecord, ...] = ()

    # ----- Optional, populated when meaningful -----
    oldest_hit_age_days: int | None = None
    models_distribution: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
