"""Lock-file deserialization helpers.

Phase 3.3 (cache lock) reads `.whatifd/cache/.lock` JSON content to
inspect the recorded holder for stale-detection AND for diagnostic
message enrichment when the lock is held by another process. Both
paths use the same typed helper:

`parse_lock_file_content(raw) -> LockFileContent | None`

The two-valued contract gives callers a clean either-typed-or-stale
boundary. Cardinal #6: no `dict[str, Any]` crosses module
boundaries — the dataclass constructor IS the boundary. Stale-
detection treats `None` as "stale by definition; no provenance to
respect"; diagnostic-message construction treats `None` as "lock
content is unparseable, fall back to a degraded message."

Centralizing here (rather than inline in `whatifd/cache/lock.py`) keeps
the symmetry with `canonical_json_bytes`: writing canonical bytes
lives in this package; reading them back lives next to it. A future
broadening of the banned-import lint to cover all `json` usage outside
serialization will find every `json` call already inside this package.

The `LockFileContent` type lives in `whatifd.cache._types` (extracted
to break the runtime circular dependency between `whatifd.cache.lock`
and this module). External callers should still import the type from
`whatifd.cache.lock`, which re-exports it.
"""

from __future__ import annotations

import json

from whatifd.cache._types import LockFileContent


def parse_lock_file_content(raw: str | bytes) -> LockFileContent | None:
    """Parse a lock-file JSON payload into a typed `LockFileContent`.

    Returns `None` when the payload is empty (zero-byte file from a
    crashed-during-write residue) or unparseable (corrupted JSON,
    missing required fields, wrong field types). Callers map `None`
    to their domain-specific stale or unparseable handling.

    A successful return guarantees all four `LockFileContent` fields
    are present and of the correct type. The cardinal #6 boundary
    is the dataclass constructor; this helper raises nothing back to
    the caller — invalid input maps to `None`.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return LockFileContent(
            pid=int(parsed["pid"]),
            process_start_time=float(parsed["process_start_time"]),
            hostname=str(parsed["hostname"]),
            started_at=str(parsed["started_at"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
