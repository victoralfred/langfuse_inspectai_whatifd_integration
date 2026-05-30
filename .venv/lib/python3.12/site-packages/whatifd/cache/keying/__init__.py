"""`whatifd.cache.keying` — cache key construction.

Versioned package: the active version is `v1`. PRs that change keying
semantics (component set, hashing scheme, normalization) MUST bump to
`v2` rather than mutate `v1`. Keys produced by `v1` and `v2` MUST NOT
collide; the version prefix on the emitted key string makes the
distinction grep-able.

The active version is re-exported here so call sites import from
`whatifd.cache.keying` (stable surface) rather than
`whatifd.cache.keying.v1` (versioned, may be deprecated in v0.2).
"""

from whatifd.cache.keying.v1 import (
    CACHE_KEY_VERSION,
    CacheKeyComponents,
    build_cache_key,
)

__all__ = ("CACHE_KEY_VERSION", "CacheKeyComponents", "build_cache_key")
