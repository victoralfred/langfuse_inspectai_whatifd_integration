"""`whatifd.cache` — scorer cache subsystem.

Phase 3 (cache subsystem) per the v0.1 implementation plan. The cache
addresses *reproducibility* — the same trace + same scorer config
produces the same cached judge result, so the verdict's evidence is
stable across runs. Per cardinal #10, scorer caching is NOT a claim
about reliability, validity, or calibration; that distinction is
disclosed in `MethodologyDisclosure.judge_state`.

Sub-packages (filled in across Phase 3 sub-phases):

- `keying.v1` — cache key construction (Phase 3.1)
- `storage.v1` — file layout and metadata (Phase 3.2)
- `lock` — single-writer enforcement (Phase 3.3)
- `policy` — mode resolution from config + environment (Phase 3.4)
- `summary` — `CacheSummary` typed object for `ReportV01` (Phase 3.5)

Versioned packages (`keying.v1`, `storage.v1`) carry the
`CACHE_KEY_VERSION` and `CACHE_SCHEMA_VERSION` constants. PRs touching
these directories MUST bump the version — the cache-version-bump test
asserts this, and a stale version is the cache-poisoning footgun.

The most-used surface (`acquire_cache_lock`, `CacheLock`,
`CacheLockedError`) is re-exported here so callers can write
`from whatifd.cache import acquire_cache_lock` instead of reaching
into the submodule. The submodule (`whatifd.cache.lock`) remains
the source of truth and is what tooling will rename if the lock
implementation is ever swapped (e.g., for a network-coordinated v0.3
multi-tenant lock).
"""

from pathlib import Path

from whatifd.cache.lock import (
    LOCK_FAILURE_CODE,
    CacheLock,
    CacheLockedError,
    acquire_cache_lock,
)
from whatifd.cache.policy import CachePolicyResolution, resolve_cache_mode
from whatifd.cache.summary import (
    CachePolicySnapshot,
    CacheSummary,
    PolicyViolationRecord,
)

# Single source of truth for the cache-root default. The storage
# layer's docstring already names `.whatifd/cache/` as the canonical
# layout; this constant is the importable form. CLI subcommands
# (`whatifd fork`, `whatifd cache *`) and any future runtime code
# default to this path; an operator override goes through
# `--cache-root` on the CLI or an explicit argument in code. A
# future change here propagates everywhere; cli.py / recovery.py /
# any new caller read the same value.
DEFAULT_CACHE_ROOT = Path(".whatifd/cache")

__all__ = (
    "DEFAULT_CACHE_ROOT",
    "LOCK_FAILURE_CODE",
    "CacheLock",
    "CacheLockedError",
    "CachePolicyResolution",
    "CachePolicySnapshot",
    "CacheSummary",
    "PolicyViolationRecord",
    "acquire_cache_lock",
    "resolve_cache_mode",
)
