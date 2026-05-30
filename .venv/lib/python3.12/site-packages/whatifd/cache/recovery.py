"""`whatifd.cache.recovery` — operator-facing cache repair operations.

Phase 8.3 of the v0.1 implementation plan. Surfaces the three
cache-recovery primitives the walkthroughs reference (scenario 5
in particular):

  - `rebuild(cache_root, *, force) -> RebuildResult` — wipes
    `<cache_root>/entries/`. Operators run this when scoring is
    structurally broken (corrupted entries, mid-write crash) and
    cache continuity is less valuable than a clean slate.
  - `unlock(cache_root, *, allow_alive) -> UnlockResult` — removes
    `<cache_root>/.lock` after a PID-alive safety check. Default
    refuses to clobber a live lock; `allow_alive=True` overrides.
  - `verify(cache_root) -> VerifyResult` — walks every entry in
    `<cache_root>/entries/` and reports any that fail to parse as
    valid `CacheEntry` JSON. v0.1 verifies structural integrity
    only; cryptographic content-hash verification is deferred to
    v0.2 when entries carry a stored hash field.

## Why a separate module from `whatifd/cache/storage/v1.py`

Storage owns the read/write contract that the runtime consumes.
Recovery owns the destructive operator-facing repairs that should
NOT be reachable from the runtime path. Separating them keeps the
runtime import graph clean (storage doesn't import recovery), and
the banned-import lint can prevent runtime modules from accidentally
calling `rebuild` or `unlock` (Phase 9 follow-up if motivated).

## Cardinal alignment

- **#1 failures-as-data:** each function returns a typed
  `*Result` dataclass; CLI consumers branch on the result rather
  than on exception types. Genuinely unexpected I/O errors
  (permission denied, disk full) propagate as OSError — not in
  v0.1's failure-as-data scope.
- **#2 floor cannot be bypassed:** rebuild / unlock / verify are
  cache-subsystem operations; they don't touch verdict logic. No
  cardinal-#2 surface here.
- **#7 two-affirmation:** unlock is cache-recovery, NOT a
  structurally-dangerous capability that needs two-affirmation.
  Per the cascade catalog "CLI cache subcommands for v0.1": a
  CLI flag (`--allow-alive`) is sufficient because unlock is a
  recovery path, not an opt-in to a sensitive capability.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psutil

# Closed set of unlock error sentinels. Distinct from the freeform
# `unlink_error` string (which carries the OS-level message); the
# CLI exhaustively branches on this sentinel so a future variant
# addition surfaces at every consumer call site.
UnlockErrorCode = Literal["no_lock_file", "lock_holder_alive", "unlink_failed"]

_ENTRIES_SUBDIR = "entries"
_LOCK_FILENAME = ".lock"


@dataclass(frozen=True, slots=True)
class RebuildResult:
    """Outcome of `rebuild`.

    - `entries_removed`: JSON files deleted from bucket dirs.
    - `bucket_dirs_removed`: two-char digest-prefix directories
      removed (one per bucket the storage layer created).
    - `non_bucket_skipped`: non-directory paths directly under
      `entries/` (stray files; shouldn't normally exist).
    - `non_file_skipped_in_bucket`: non-file paths INSIDE bucket
      directories (e.g., nested subdirs). Storage layout is
      bucket/<file>; anything else is structurally unexpected.
      Surfaced separately from `non_bucket_skipped` because the
      anomaly is at a different layer (inside vs above buckets).
    - `error`: set when the cache root or entries dir didn't
      exist (a no-op rebuild).
    """

    entries_removed: int
    bucket_dirs_removed: int
    non_bucket_skipped: int = 0
    non_file_skipped_in_bucket: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class UnlockResult:
    """Outcome of `unlock`.

    - `removed`: True iff the lock file was deleted.
    - `pid_was_alive`: whether the recorded PID was a live process
      at the time of the check (informational).
    - `error`: closed-set sentinel from `UnlockErrorCode` or
      `None` for clean success. CLI branches exhaustively on this.
    - `unlink_error`: OS-level error message when `error="unlink_failed"`
      (the freeform string from `OSError.__str__`). `None` for any
      other error code.

    Splitting the sentinel from the freeform message: the previous
    `error="unlink_failed: <exc>"` pattern mixed two concepts in
    one field. Now the CLI matches on the closed `error` literal
    and reads `unlink_error` separately when needed.
    """

    removed: bool
    pid_was_alive: bool
    error: UnlockErrorCode | None = None
    unlink_error: str | None = None


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of `verify`. `total` is every file under entries/;
    `valid` parses cleanly as a CacheEntry; `corrupted` is the
    tuple of paths that failed to parse.

    Three coherent states:

      - **Clean:** `error=None`, `corrupted=()`, `total>=0`. CLI
        exits 0.
      - **Vacuously clean:** `vacuous=True`, `total=0`. The
        entries directory doesn't exist; nothing to verify. CLI
        also exits 0. Distinct from "Clean" so consumers can
        report the difference operationally.
      - **Corrupted:** `corrupted` non-empty. CLI exits 2.

    `vacuous` is a dedicated bool rather than reusing `error`
    because the entries-dir-missing case is NOT an error — it's
    the absence of a verify subject. The previous `error=
    "entries_dir_missing"` overload conflated "no work to do"
    with "verify failed", which is a different semantic and
    would confuse future contributors.

    `corrupted` is a tuple (not a list) for immutability — the
    `frozen=True` decorator alone doesn't prevent mutation
    through a list-typed field.

    `non_bucket_skipped` mirrors `RebuildResult.non_bucket_skipped`:
    counts paths directly under `entries/` that aren't bucket
    directories (stray files; shouldn't normally exist). Without
    this counter, verify would silently ignore them while rebuild
    reports them — closing that operational gap so the two
    operations describe the same anomalies.

    `non_file_skipped_in_bucket` mirrors
    `RebuildResult.non_file_skipped_in_bucket`: counts non-file
    children INSIDE bucket directories (e.g., nested subdirs
    that shouldn't exist with the v0.1 storage layout). Same
    gap-closing rationale as `non_bucket_skipped`.
    """

    total: int
    valid: int
    corrupted: tuple[Path, ...]
    vacuous: bool = False
    non_bucket_skipped: int = 0
    non_file_skipped_in_bucket: int = 0


def rebuild(cache_root: Path, *, force: bool) -> RebuildResult:
    """Delete every entry under `<cache_root>/entries/`. The lock
    file and `meta.json` are preserved — the storage layer's
    schema-version contract stays intact, only the cached values
    are wiped.

    `force` is required to actually delete; without it the function
    returns a no-op result with `error="force_required"`. This is
    the safety belt against a CLI-typo deletion of cache state.
    """
    if not force:
        return RebuildResult(
            entries_removed=0,
            bucket_dirs_removed=0,
            error="force_required",
        )

    entries_dir = cache_root / _ENTRIES_SUBDIR
    if not entries_dir.exists():
        return RebuildResult(
            entries_removed=0,
            bucket_dirs_removed=0,
            error="entries_dir_missing",
        )

    entries_removed = 0
    bucket_dirs_removed = 0
    non_bucket_skipped = 0
    non_file_skipped_in_bucket = 0
    for bucket in entries_dir.iterdir():
        if not bucket.is_dir():
            # Stray file directly under entries/ — shouldn't
            # normally exist; the storage layer only writes inside
            # bucket subdirectories. Count + skip rather than
            # silently ignore so an operator running rebuild can
            # see the anomaly in the result.
            #
            # TODO(future): a `--strict` flag that errors on
            # `non_bucket_skipped > 0` would surface anomalies
            # more loudly. Deferred from v0.1 because the count
            # in `RebuildResult` is sufficient feedback for the
            # current operator surface (`whatifd cache rebuild`
            # prints it). If a real user encounters stray files
            # often enough to want the hard error, that's the
            # trigger.
            non_bucket_skipped += 1
            continue
        bucket_had_only_files = True
        for entry_file in bucket.iterdir():
            if entry_file.is_file():
                entry_file.unlink()
                entries_removed += 1
            else:
                # Non-file inside a bucket (e.g., a nested subdir
                # — should never exist with the v0.1 storage
                # layout). Count + skip; an operator will see the
                # number and can investigate. Don't recurse blindly
                # to avoid silent data loss in case the structure
                # is something we don't understand yet.
                non_file_skipped_in_bucket += 1
                bucket_had_only_files = False
        if bucket_had_only_files:
            bucket.rmdir()
            bucket_dirs_removed += 1
        # else: leave the bucket dir behind so the operator can
        # inspect the unexpected non-file children.
    return RebuildResult(
        entries_removed=entries_removed,
        bucket_dirs_removed=bucket_dirs_removed,
        non_bucket_skipped=non_bucket_skipped,
        non_file_skipped_in_bucket=non_file_skipped_in_bucket,
    )


def unlock(cache_root: Path, *, allow_alive: bool) -> UnlockResult:
    """Remove `<cache_root>/.lock` after a PID-alive safety check.

    Reads the lock file's recorded PID and checks via `psutil`
    whether the process is still alive. If alive AND
    `allow_alive=False`, refuses to act and returns
    `error="lock_holder_alive"`. If alive AND `allow_alive=True`,
    removes the lock anyway (operator override).
    """
    lock_path = cache_root / _LOCK_FILENAME
    if not lock_path.exists():
        return UnlockResult(removed=False, pid_was_alive=False, error="no_lock_file")

    pid_was_alive = False
    try:
        raw = lock_path.read_text(encoding="utf-8")
        recorded = json.loads(raw)
        recorded_pid = int(recorded.get("pid", -1))
        if recorded_pid > 0:
            try:
                proc = psutil.Process(recorded_pid)
                # `is_running()` returns True for zombies too;
                # `status()` returning STATUS_ZOMBIE means the
                # process is effectively dead. Lock holder must be
                # ALIVE to refuse.
                if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    pid_was_alive = True
            except psutil.NoSuchProcess:
                pid_was_alive = False
    except (json.JSONDecodeError, OSError, ValueError):
        # Corrupted lock file: treat as not-alive (safe to remove);
        # the operator's intent in running `unlock` is recovery.
        pid_was_alive = False

    if pid_was_alive and not allow_alive:
        return UnlockResult(
            removed=False,
            pid_was_alive=True,
            error="lock_holder_alive",
        )

    try:
        lock_path.unlink()
    except OSError as exc:
        return UnlockResult(
            removed=False,
            pid_was_alive=pid_was_alive,
            error="unlink_failed",
            unlink_error=str(exc),
        )
    return UnlockResult(removed=True, pid_was_alive=pid_was_alive)


def verify(cache_root: Path) -> VerifyResult:
    """Walk every JSON file under `<cache_root>/entries/` and
    confirm it parses as a valid `CacheEntry`.

    v0.1 checks STRUCTURAL integrity (parse + required fields).
    Cryptographic content-hash verification (catching disk-bit-
    flip corruption that produces still-parseable but wrong data)
    requires `CacheEntry` to carry a stored content hash; that's
    a v0.2 schema bump.
    """
    entries_dir = cache_root / _ENTRIES_SUBDIR
    if not entries_dir.exists():
        return VerifyResult(total=0, valid=0, corrupted=(), vacuous=True)

    total = 0
    valid = 0
    corrupted: list[Path] = []
    non_bucket_skipped = 0
    non_file_skipped_in_bucket = 0
    for bucket in entries_dir.iterdir():
        if not bucket.is_dir():
            # Stray file directly under entries/ — same anomaly
            # rebuild reports via non_bucket_skipped. Verify
            # surfaces it too so the two operations describe the
            # same shape of unexpected state.
            non_bucket_skipped += 1
            continue
        for entry_file in bucket.iterdir():
            if not entry_file.is_file():
                # Non-file inside a bucket (e.g., nested subdir).
                # Same rationale as rebuild's
                # non_file_skipped_in_bucket: surface the count
                # so verify and rebuild describe the same shape
                # of unexpected state.
                non_file_skipped_in_bucket += 1
                continue
            total += 1
            if _is_valid_entry(entry_file):
                valid += 1
            else:
                corrupted.append(entry_file)
    return VerifyResult(
        total=total,
        valid=valid,
        corrupted=tuple(corrupted),
        non_bucket_skipped=non_bucket_skipped,
        non_file_skipped_in_bucket=non_file_skipped_in_bucket,
    )


def _is_valid_entry(path: Path) -> bool:
    """Return True if `path` parses to a `CacheEntry`-shaped JSON
    with the expected fields. Used by `verify`.

    TODO(v0.2): when `CacheEntry` carries a stored content hash,
    extend this function to (a) reconstruct via Pydantic for full
    field validation and (b) recompute the content hash and
    compare against the stored value. The current structural
    check catches corruption that breaks JSON parse or drops
    fields; it does NOT catch disk-bit-flip corruption that
    leaves the file syntactically valid but semantically wrong.
    The v0.2 schema bump that adds the hash field is the trigger
    for that extension.
    """
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return False

    if not isinstance(data, dict):
        return False
    # Required CacheEntry fields per storage/v1.py plus minimal
    # type guards. Tightening beyond key-presence catches a class
    # of corruption (e.g., key truncated to None, metadata
    # overwritten with a list) that bare key-membership misses,
    # at negligible cost.
    required = {"key", "value", "metadata"}
    if not required.issubset(data.keys()):
        return False
    if not isinstance(data["key"], str) or not data["key"]:
        return False
    if not isinstance(data["metadata"], dict):  # noqa: SIM103
        return False
    # `value` is intentionally not type-checked — adapters return
    # arbitrary JSON-serializable scorer outputs (string, dict,
    # list); verify shouldn't enforce a shape the storage layer
    # itself doesn't enforce.
    #
    # Why not Pydantic here despite the project's "Pydantic at
    # boundaries" rule: this is NOT a boundary in the Pydantic-
    # at-boundaries sense. Verify is a recovery operation that
    # walks bytes-on-disk; a malformed entry that fails full
    # Pydantic validation but passes structural parse should still
    # be flagged as "structurally valid, semantically suspect"
    # rather than crash mid-walk. The structural checks above
    # catch truncation / truly missing fields without rejecting
    # v0.1-shape entries that have extra fields the future v0.2
    # reader will populate. Full Pydantic reconstruction lands
    # with the v0.2 cryptographic-hash extension — see TODO at
    # the function docstring.
    return True


__all__ = [
    "RebuildResult",
    "UnlockResult",
    "VerifyResult",
    "rebuild",
    "unlock",
    "verify",
]
