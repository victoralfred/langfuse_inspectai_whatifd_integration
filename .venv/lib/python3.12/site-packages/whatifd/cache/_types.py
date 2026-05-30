"""Shared types for `whatifd.cache.*` — extracted to break the
serialization/lock circular import.

`whatifd.cache.lock` and `whatifd.serialization.lock_io` both need
`LockFileContent`. Defining it in either of those modules creates a
runtime circular dependency: lock.py imports the parser from
serialization/, which needs the dataclass type from lock.py. Putting
the shared types here lets both modules import at module-load time
with no late-binding workarounds.

Underscore-prefixed module name signals "package-internal surface" —
external callers should import `LockFileContent`/`CacheLock` from
`whatifd.cache.lock` (the stable public surface), not from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LockFileContent:
    """The structured payload of `.lock`.

    `pid` and `process_start_time` together identify the holding
    process unambiguously across PID reuse — `os.getpid()` alone can
    collide with a recycled PID after process death.

    `hostname` is informational; cross-host locks are not supported in
    v0.1 (the lock primitive is filesystem-local).

    `started_at` is the wall-clock time the lock was acquired
    (ISO-8601 UTC). Used for age-based stale detection (opt-in) and
    for the recovery message in `CacheLockedError`.
    """

    pid: int
    process_start_time: float
    hostname: str
    started_at: str


@dataclass(frozen=True, slots=True)
class CacheLock:
    """Handle returned by `acquire_cache_lock`.

    Carries the path of the lock file and the recorded `LockFileContent`
    so callers can log/manifest who acquired it. The actual `fcntl`
    handle is held internally by the context manager and released on
    `__exit__`.
    """

    lock_path: Path
    content: LockFileContent
