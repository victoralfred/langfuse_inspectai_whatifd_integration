"""`whatifd.cache.storage` — cache file layout and entry I/O.

Versioned package: the active version is `v1`. PRs that change the
on-disk file format (entry shape, directory layout, `meta.json`
schema) MUST bump to `v2` rather than mutate `v1`. Entries written by
`v1` and read by `v2` go through migration; entries written by `v2`
and read by `v1` are an error (a forward-compatibility break is a
rollback hazard the cache is not designed to absorb).

The active version is re-exported here so call sites import from
`whatifd.cache.storage` (stable surface) rather than
`whatifd.cache.storage.v1` (versioned).
"""

from whatifd.cache.storage.v1 import (
    CACHE_SCHEMA_VERSION,
    CacheEntry,
    CacheMeta,
    CacheResult,
    CacheSchemaMismatchError,
    init_cache,
    read_entry,
    read_meta,
    write_entry,
)

__all__ = (
    "CACHE_SCHEMA_VERSION",
    "CacheEntry",
    "CacheMeta",
    "CacheResult",
    "CacheSchemaMismatchError",
    "init_cache",
    "read_entry",
    "read_meta",
    "write_entry",
)
