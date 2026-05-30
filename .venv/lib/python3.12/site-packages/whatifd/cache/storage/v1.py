"""Cache storage — v1.

On-disk file layout for the scorer cache. Phase 3.2 of the v0.1
implementation plan; pairs with `whatifd/cache/keying/v1.py` (Phase 3.1).

## Layout

```
.whatifd/cache/
├── meta.json               (cache_schema_version, cache_key_version, created_at)
├── .lock                   (Phase 3.3 — not written here)
└── entries/
    └── <digest[0:2]>/
        └── <digest>.json   (one entry per cache key)
```

`<digest>` is the 64-char hex portion of the cache key (the part after
the `v1:` prefix from `build_cache_key`). Sharding by the first 2 hex
chars gives 256 directories at saturation, avoiding filesystem-level
slowdowns at scale. The `v1:` prefix is intentionally NOT in the
filename — `:` is invalid on Windows filesystems, and the
`cache_key_version` lives inside the entry JSON anyway.

## Entry shape

Per `references/contracts.md` §"Entry format". `key_components` is
typed as `CacheKeyComponents` (Phase 3.1) — the storage layer round-
trips the typed shape rather than a raw dict, so cardinal #6's "no
`dict[str, Any]` at typed boundaries" holds. A keying-v2 schema
change requires a storage-v2 module (paired version bump).

`rationale` is stored only when the storage profile is `full_judge_io`
(per `references/contracts.md`); the default profile
(`normalized_result_only`) has `rationale: None`. The profile gating
is the CALLER'S responsibility — this storage layer writes whatever
`CacheEntry` the caller hands it. The cardinal #5 boundary
(no `Sensitive[T]` in entry contents) is enforced by the
`canonical_json_bytes` top-level guard plus the `CacheKeyComponents`
hex-validation invariant from Phase 3.1.

## Versioning

`CACHE_SCHEMA_VERSION = "v1"` is written into `meta.json` at cache
init, and into every entry. PRs that change the on-disk file format
MUST introduce a `v2` module rather than mutate `v1`.

## Cardinal #1 split: programmer bugs vs data conditions

- **Programmer-bug paths** raise `InvariantViolationError`: caller
  constructs a `CacheEntry` with the wrong version, or hands a
  wrong-version key to `read_entry`/`write_entry`. These are
  contract violations the caller controls.
- **Data-condition paths** raise `CacheSchemaMismatchError`: an
  on-disk file declares a version this module doesn't know. The
  caller did everything right; the disk surprised us. Callers convert
  this to a `FailureRecord` at the appropriate scope.

Splitting the two avoids the "is this a bug or a data condition?"
ambiguity at the catch site.

## What this module does NOT do

- **Locking** — Phase 3.3 (`whatifd/cache/lock.py`).
- **Mode resolution** — Phase 3.4 (`whatifd/cache/policy.py`).
- **CacheSummary aggregation** — Phase 3.5 (`whatifd/cache/summary.py`).
- **Profile gating on `rationale`** — caller's responsibility.

Tests run against `tmp_path`; no shared state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeAlias

from whatifd.cache.keying import CACHE_KEY_VERSION, CacheKeyComponents
from whatifd.exceptions import InvariantViolationError
from whatifd.serialization import canonical_json_bytes
from whatifd.types.primitives import JsonPrimitive

CACHE_SCHEMA_VERSION = "v1"

_ENTRIES_DIRNAME = "entries"
_META_FILENAME = "meta.json"

# v1 keying emits SHA-256 hex digests (64 lowercase hex chars). v1
# storage validates this exact length on lookup. v2 storage paired
# with a different keying algorithm would loosen or change this
# pattern.
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

# Forward-compat opaque JSON-roundtrippable value. Used for
# `CacheMeta.extra` so unknown keys in `meta.json` preserve their shape
# across read/write without forcing this module to know what they
# mean. Tighter than `Any` (per cardinal #6 typed-boundary discipline);
# inner list/dict values stay `Any` because a recursive alias would
# add type-checker friction without strengthening the contract.
JsonValue: TypeAlias = JsonPrimitive | list[Any] | dict[str, Any]

# Known top-level keys in `meta.json`. Any key NOT in this set lands in
# `CacheMeta.extra` (forward-compat round-trip per `CacheMeta` docstring).
# `_write_meta` enforces that `extra` MUST NOT shadow any of these
# names — a corrupted meta.json or a buggy caller cannot silently
# overwrite version fields by smuggling them through `extra`.
_META_KNOWN_KEYS = frozenset({"cache_schema_version", "cache_key_version", "created_at"})


class CacheSchemaMismatchError(Exception):
    """An on-disk file declares a `cache_schema_version` this module
    cannot read.

    DATA condition (the file says one thing, this module expects
    another), not a programmer bug. Callers convert to a
    `FailureRecord` at the appropriate scope (per cardinal #1,
    expected failures are data). Programmer-side version mismatches
    (entry constructed with the wrong declared version, key passed in
    with the wrong prefix) raise `InvariantViolationError` instead;
    the split keeps cardinal #1 catch-site classification unambiguous.
    """


@dataclass(frozen=True, slots=True)
class CacheResult:
    """The judge result stored alongside a cache key.

    `score_delta`, `confidence` are decimal-formatted strings for
    cross-platform determinism (cardinal #4); `flags` is the list of
    judge-emitted flags as bare strings (no domain typing in v0.1; the
    judge schema treats them opaquely).

    `rationale` is `str | None`. The caller decides whether to populate
    it — `full_judge_io` profile populates; `normalized_result_only`
    sets None. The storage layer does not gate; it writes what it gets.
    """

    score_delta: str
    verdict: str
    confidence: str
    flags: tuple[str, ...] = ()
    rationale: str | None = None


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One cache entry as written to disk.

    `key_components` is the `CacheKeyComponents` instance the cache
    key was derived from, stored for human-readable provenance and
    so a debugger can reconstruct the inputs without re-running the
    adapter. Typed (not `dict[str, Any]`) per cardinal #6 — a v2
    keying schema requires a v2 storage module.

    `created_at` is an ISO-8601 UTC timestamp produced at write time.
    Non-deterministic; not part of the cache key.
    """

    cache_key_version: str
    cache_schema_version: str
    created_at: str
    key_components: CacheKeyComponents
    result: CacheResult


@dataclass(frozen=True, slots=True)
class CacheMeta:
    """Top-level `meta.json` content. One per cache directory.

    Records the versions the cache directory was initialized with.
    Reading code uses this to decide whether to migrate, refuse, or
    proceed. Cache-version-bump tests assert the entries match the
    meta-recorded versions.

    `extra` is the forward-compatibility escape hatch: any keys present
    in `meta.json` that this module does not recognize are collected
    here on read and re-emitted on write. A future minor that adds a
    new informational field to `meta.json` (e.g., `tenant_id`,
    `last_verified_at`) can land that field as a v1 extension without
    breaking existing v1 caches — older code preserves the new field
    via `extra` round-trip rather than dropping it. Breaking changes
    (new required fields, semantic changes to existing fields) still
    require a `v2` schema bump.

    Invariant: `extra` MUST NOT contain any of the known top-level key
    names (`cache_schema_version`, `cache_key_version`, `created_at`).
    `_write_meta` enforces this — a corrupted on-disk meta or a buggy
    caller cannot silently overwrite version fields by smuggling them
    through `extra`.
    """

    cache_schema_version: str
    cache_key_version: str
    created_at: str
    extra: dict[str, JsonValue] = field(default_factory=dict)


def init_cache(root: Path) -> CacheMeta:
    """Create the cache directory layout if it doesn't exist; return
    the existing or newly-written `meta.json` content.

    Idempotent: calling `init_cache` on an already-initialized cache
    returns the recorded meta without overwriting it. Calling on a
    cache whose recorded versions do not match this module's constants
    raises `CacheSchemaMismatchError` — schema migration is not
    automatic. (DATA condition: the disk says one version, we expect
    another. The caller did everything right.)
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / _ENTRIES_DIRNAME).mkdir(exist_ok=True)
    meta_path = root / _META_FILENAME
    if meta_path.exists():
        meta = read_meta(root)
        if meta.cache_schema_version != CACHE_SCHEMA_VERSION:
            raise CacheSchemaMismatchError(
                f"Cache at {root} was initialized with cache_schema_version="
                f"{meta.cache_schema_version!r}; this module expects "
                f"{CACHE_SCHEMA_VERSION!r}. Migration is not automatic in v0.1; "
                "rebuild the cache via `whatifd cache rebuild --force`."
            )
        return meta
    meta = CacheMeta(
        cache_schema_version=CACHE_SCHEMA_VERSION,
        cache_key_version=CACHE_KEY_VERSION,
        created_at=_utc_now_iso(),
    )
    _write_meta(root, meta)
    return meta


def read_meta(root: Path) -> CacheMeta:
    """Read `meta.json` from a cache root.

    Raises `FileNotFoundError` if the cache hasn't been initialized
    (callers wanting idempotence use `init_cache` first). Raises
    `CacheSchemaMismatchError` on a corrupted file (invalid JSON) or
    a file missing required top-level keys — both are DATA conditions
    per cardinal #1: the disk surprised us, not a programmer bug.
    """
    meta_path = root / _META_FILENAME
    text = meta_path.read_text(encoding="utf-8")  # FileNotFoundError surfaces
    try:
        raw = json.loads(text)
        return CacheMeta(
            cache_schema_version=raw["cache_schema_version"],
            cache_key_version=raw["cache_key_version"],
            created_at=raw["created_at"],
            extra={k: v for k, v in raw.items() if k not in _META_KNOWN_KEYS},
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise CacheSchemaMismatchError(
            f"Cache meta file at {meta_path} is corrupted or missing "
            f"required top-level keys: {e}. The file is unreadable as a "
            "v1 CacheMeta. Likely cause: on-disk corruption or partial "
            "write from a crashed run. Recover via "
            "`whatifd cache rebuild --force`."
        ) from e


def write_entry(root: Path, key: str, entry: CacheEntry) -> Path:
    """Write `entry` to its sharded path under `root` and return the
    written path.

    Overwrites any existing entry at the same key (caller is
    responsible for coordinating concurrent writers via the Phase 3.3
    lock). Uses `canonical_json_bytes` for the on-disk encoding so
    entries written by different platforms compare byte-equal — useful
    for cache integrity verification (`whatifd cache verify`).

    The entry's `cache_schema_version` MUST match this module's
    `CACHE_SCHEMA_VERSION`; mismatch is a programmer bug (the caller
    constructed an entry with the wrong declared version), so this
    raises `InvariantViolationError`, NOT `CacheSchemaMismatchError`.
    """
    if entry.cache_schema_version != CACHE_SCHEMA_VERSION:
        raise InvariantViolationError(
            f"CacheEntry.cache_schema_version={entry.cache_schema_version!r} "
            f"does not match storage CACHE_SCHEMA_VERSION={CACHE_SCHEMA_VERSION!r}. "
            "Entries written by this module must declare the matching version. "
            "(Programmer bug; not a runtime data condition.)"
        )
    digest = _digest_from_key(key)
    shard_dir = root / _ENTRIES_DIRNAME / digest[:2]
    shard_dir.mkdir(parents=True, exist_ok=True)
    entry_path = shard_dir / f"{digest}.json"
    entry_path.write_bytes(canonical_json_bytes(_entry_to_dict(entry)))
    return entry_path


def read_entry(root: Path, key: str) -> CacheEntry | None:
    """Read the entry at `key` and return `CacheEntry`, or `None` on
    cache miss (file does not exist).

    Raises `CacheSchemaMismatchError` if the on-disk entry's
    `cache_schema_version` does not match this module's constant —
    DATA condition; callers convert to a `FailureRecord`. A
    wrong-version `key` (e.g., `v2:` against v1 storage) raises
    `InvariantViolationError` from `_digest_from_key` because that's a
    programmer bug, not a data condition.
    """
    digest = _digest_from_key(key)
    entry_path = root / _ENTRIES_DIRNAME / digest[:2] / f"{digest}.json"
    if not entry_path.exists():
        return None
    try:
        raw = json.loads(entry_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CacheSchemaMismatchError(
            f"Cache entry at {entry_path} is corrupted (invalid JSON): {e}. "
            "Likely cause: on-disk corruption or partial write. Recover "
            "via `whatifd cache rebuild --force`."
        ) from e
    if not isinstance(raw, dict) or raw.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
        raise CacheSchemaMismatchError(
            f"Entry {entry_path} has cache_schema_version="
            f"{raw.get('cache_schema_version') if isinstance(raw, dict) else None!r}; "
            f"this module expects {CACHE_SCHEMA_VERSION!r}. "
            "Migration is not automatic in v0.1."
        )
    # Reconstruct typed object. Wrap the kwarg splats in a domain-
    # specific exception: a corrupted or version-skewed entry surfaces
    # as `CacheSchemaMismatchError` (data condition; cardinal #1)
    # rather than a raw `TypeError`/`KeyError` from the constructor.
    try:
        result = raw["result"]
        key_components = CacheKeyComponents(**raw["key_components"])
        return CacheEntry(
            cache_key_version=raw["cache_key_version"],
            cache_schema_version=raw["cache_schema_version"],
            created_at=raw["created_at"],
            key_components=key_components,
            result=CacheResult(
                score_delta=result["score_delta"],
                verdict=result["verdict"],
                confidence=result["confidence"],
                flags=tuple(result.get("flags", ())),
                rationale=result.get("rationale"),
            ),
        )
    except (TypeError, KeyError) as e:
        raise CacheSchemaMismatchError(
            f"Entry {entry_path} has a shape this storage module cannot "
            f"reconstruct: {e}. The on-disk JSON does not match the v1 "
            "CacheEntry/CacheKeyComponents fields. Likely cause: a v2 "
            "schema written into a v1 entry by a future code path, or "
            "on-disk corruption."
        ) from e


def _digest_from_key(key: str) -> str:
    """Strip the `<version>:` prefix and return the digest portion.

    The key MUST be of the form `<version>:<64-char-lowercase-hex>`
    where `<version>` matches `CACHE_KEY_VERSION` (currently `"v1"`).
    Anything else — a bare digest, a `v2:` prefix, an empty string,
    a too-short or non-hex digest — is a programmer bug (the caller
    built the key wrong), so this raises `InvariantViolationError`,
    NOT `CacheSchemaMismatchError`. The digest-shape check defends
    against `"v1:"` (empty digest, would otherwise produce a path of
    `entries//.json`).
    """
    if ":" not in key:
        raise InvariantViolationError(
            f"Cache key {key!r} has no `<version>:` prefix. Keys must be "
            f"constructed via `whatifd.cache.keying.build_cache_key`, which "
            f"emits the prefix; bare digests are not accepted."
        )
    prefix, digest = key.split(":", 1)
    if prefix != CACHE_KEY_VERSION:
        raise InvariantViolationError(
            f"Cache key version mismatch: key prefix={prefix!r}; "
            f"this storage module expects {CACHE_KEY_VERSION!r}. "
            "A v2 key cannot be looked up in v1 storage. "
            "(Programmer bug; the caller passed a key from a different "
            "keying version.)"
        )
    if not _DIGEST_RE.match(digest):
        raise InvariantViolationError(
            f"Cache key digest {digest!r} is not 64 lowercase hex chars. "
            f"v1 keying emits SHA-256 hex digests; this is the only shape "
            f"v1 storage accepts. (Programmer bug; key was malformed before "
            "reaching the cache.)"
        )
    return digest


def _entry_to_dict(entry: CacheEntry) -> dict[str, Any]:
    """Convert a `CacheEntry` to the on-disk dict shape.

    Uses field-by-field copy rather than `asdict()` at the top level
    so the on-disk schema is decoupled from the dataclass shape — a
    future field on `CacheEntry` is opt-in into the wire format, not
    auto-included. `key_components` is asdict'd inside because it
    belongs to a sibling versioned module (`whatifd.cache.keying.v1`):
    any field added there requires a paired keying-version bump under
    the project's "PRs touching this directory bump version" rule, so
    asdict's auto-include behavior is bounded by the version contract,
    not by typing here.

    `dict[str, Any]` return type is the wire-format glue: the function
    produces JSON-roundtrippable structure for `canonical_json_bytes`
    consumption. Per cardinal #6, public schemas are hand-written
    (`ReportV01`, etc.); this helper is private (underscore prefix)
    and never crosses the public-schema boundary. The boundary types
    `CacheEntry`, `CacheResult`, `CacheKeyComponents` ARE typed; the
    JSON glue is intentionally opaque.
    """
    from dataclasses import asdict

    r = entry.result
    return {
        "cache_key_version": entry.cache_key_version,
        "cache_schema_version": entry.cache_schema_version,
        "created_at": entry.created_at,
        "key_components": asdict(entry.key_components),
        "result": {
            "score_delta": r.score_delta,
            "verdict": r.verdict,
            "confidence": r.confidence,
            "flags": list(r.flags),
            "rationale": r.rationale,
        },
    }


def _write_meta(root: Path, meta: CacheMeta) -> None:
    # Refuse to write if `extra` shadows any known top-level key.
    # A `**meta.extra` spread that overrode a version field would
    # silently corrupt the meta — better to surface the bug.
    overlap = _META_KNOWN_KEYS & meta.extra.keys()
    if overlap:
        raise InvariantViolationError(
            f"CacheMeta.extra contains reserved top-level key(s) {sorted(overlap)!r}. "
            "Reserved keys are written from named fields, not from extra; "
            "having them in extra means a corrupted meta.json or a buggy "
            "caller. Refusing to write."
        )
    payload = {
        "cache_schema_version": meta.cache_schema_version,
        "cache_key_version": meta.cache_key_version,
        "created_at": meta.created_at,
        **meta.extra,
    }
    (root / _META_FILENAME).write_bytes(canonical_json_bytes(payload))


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with `Z` suffix.

    Wrapped so tests can monkeypatch this single function rather than
    the broader `datetime.now`. Non-deterministic by construction.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
