"""Cache lock — Phase 3.3.

Single-writer enforcement on the scorer cache directory. Pairs with
Phase 3.1 keying and Phase 3.2 storage; this is the third sub-phase of
Phase 3 per the v0.1 implementation plan.

## Why a lock at all

The scorer cache lives under `.whatifd/cache/`. A `whatifd fork` run
writes new entries as it scores traces. If two `whatifd` processes
target the same cache concurrently, partial writes can interleave and
produce corrupted entries — the cardinal #1 "failures-as-data"
boundary requires us to fail loud rather than ship a Ship verdict
backed by torn-write evidence. Single-writer enforcement is the
defensive primitive.

## How the lock works

Two layers of defense:

1. **OS-level `fcntl.flock(LOCK_EX | LOCK_NB)`** on `<cache>/.lock`.
   Reliable on Linux/macOS. Releases automatically on process death
   including SIGKILL, kernel panic, or OOM kill. The kernel does not
   leak fcntl locks across exec or process exit.
2. **Stale-lock fallback** for the case where flock thinks the lock
   is held but the recorded process is no longer the one holding it
   (e.g., a hung process the operator killed externally; a container
   restart). Lock file records `{pid, process_start_time, hostname,
   started_at}`; takeover happens when ANY of:
   - The recorded PID is no longer running (`psutil.NoSuchProcess`).
   - The recorded PID is running but its `create_time()` differs from
     the recorded `process_start_time` (PID reused).
   - The lock is older than `stale_after_seconds` (default 24h) AND
     the operator has explicitly opted into time-based takeover via
     `acquire_cache_lock(..., allow_age_takeover=True)`. Default is
     False because age alone is a weak signal — a long-running batch
     might legitimately hold a lock for days.

The double-condition (PID dead OR PID-reused) is what
`references/enforcement.md` calls "the stale-lock-false-positive
defense": if PID 12345 dies and the OS later assigns 12345 to an
unrelated process, the recorded `process_start_time` mismatches and
takeover proceeds. A naive PID-only check would refuse takeover
because the PID is "alive" — it just isn't the same process.

## TOCTOU note on stale takeover

When `flock` initially refuses, we read the recorded lock-file
content, decide it's stale, and re-attempt `flock`. Between those
steps a different process could *also* read the file and *also*
decide stale — but `flock(LOCK_EX | LOCK_NB)` is atomic, so only one
of the contenders can hold it at a time. The losing contender gets
`BlockingIOError` on its re-flock and surfaces `CacheLockedError`
naming us as the new holder; the winner proceeds to truncate and
rewrite the lock file. The window during which the on-disk file
still names the *previous* (stale) holder, while we hold the kernel
flock and are about to overwrite it, is brief and harmless: any
operator-visible inspection of `.lock` during that window resolves on
re-read after our `fsync`. No data is lost; the only observable
artifact is a momentary mismatch between the kernel-truth (us) and
the file-truth (previous holder), which is a diagnostic anomaly, not
a correctness one.

## What this module does NOT do

- **NFS-safe locking.** `fcntl.flock` semantics on NFS are
  implementation-dependent; some clients silently degrade to advisory.
  The module documents this limitation and produces a clear error
  message naming NFS as a likely cause if flock returns an unexpected
  errno; full NFS-safe locking is deferred to v0.3.
- **Cross-host coordination.** The lock is process-local on a single
  filesystem. Multi-tenant cache directories shared across hosts are
  the cascade entry "Multi-tenant cache directories" (v0.3).
- **Read locks.** v0.1 single-writer model only; readers do not need
  to coordinate because writes are atomic-via-canonical-bytes
  (Phase 3.2 already writes whole files via `path.write_bytes`).

## Cardinal alignment

- **#1 (failures-as-data):** lock acquisition failure produces a
  typed `CacheLockedError` with a structured message (PID, hostname,
  started_at, recovery hints). Callers convert to a `FailureRecord`
  at the appropriate scope. This is NOT an `InvariantViolationError`
  — a held lock is a legitimate runtime data condition, not a
  programmer bug.
- **#9 (orchestration not compute):** `fcntl.flock` is the OS doing
  the work. No CPU optimization, no shared-memory tricks. The Python
  layer just records provenance.

## Internal surface (single underscore, importable for tests)

The single-underscore helpers below are part of the package's
internal surface. They have stable names so the unit-test suite can
exercise pure decision logic in isolation from filesystem state:

- `_should_takeover(content, stale_after_seconds, allow_age_takeover) -> bool`
  — pure stale-decision function tested in `TestShouldTakeover`.
- `_process_dead_or_recycled(content) -> bool` — psutil-backed
  helper; tested via monkeypatch for the `AccessDenied` branch.

External callers (outside `whatifd.cache.lock`) should NOT import
these — `acquire_cache_lock` is the only public surface. Internal
imports are fine because the package's test suite is the only other
in-tree consumer, and renames will be coordinated.
"""

from __future__ import annotations

import contextlib
import errno
import os
import socket
import sys
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

# Windows guard: `fcntl` is POSIX-only. Refuse import with a clear
# typed message rather than letting a bare `ImportError` leak from
# module load — cardinal #1 (failures-as-data: even import-time
# environmental failures should be diagnosable, not opaque).
if sys.platform == "win32":  # pragma: no cover (Linux/macOS-only test matrix)
    raise ImportError(
        "whatifd.cache.lock requires POSIX `fcntl`; Windows is not supported in v0.1. "
        "See pyproject.toml classifiers (Operating System :: POSIX :: Linux, MacOS) "
        "and the module docstring's 'NFS unsupported / cross-host coordination' note. "
        "Multi-platform locking is the cascade entry 'Multi-tenant cache directories' (v0.3)."
    )

import fcntl  # POSIX-only; Windows fails the sys.platform check above

import psutil

from whatifd.cache._types import CacheLock, LockFileContent
from whatifd.serialization import canonical_json_bytes, parse_lock_file_content

# Re-export the shared types so `whatifd.cache.lock` remains the stable
# public surface — external callers should not reach into _types.py.
__all__ = (
    "LOCK_FAILURE_CODE",
    "CacheLock",
    "CacheLockedError",
    "LockFileContent",
    "acquire_cache_lock",
)

# The failure-code registry entry that wraps `CacheLockedError` when
# Phase 2.6 projection layer converts the exception into a structured
# `FailureRecord`. Pinning the constant here makes the exception ↔
# registry link grep-discoverable; the registry already carries the
# cardinal #8 fix-suggestion at fix_suggestions.py:120
# (cache_lock_unavailable → "whatifd cache rebuild --force",
# "whatifd cache unlock", "whatifd cache verify").
LOCK_FAILURE_CODE = "cache_lock_unavailable"

_LOCK_FILENAME = ".lock"
_DEFAULT_STALE_AFTER_SECONDS = 86400  # 24 hours

# How much drift between recorded process_start_time and the live
# psutil.Process.create_time() is acceptable before we declare the PID
# recycled. psutil reports create_time in seconds-since-epoch, but its
# precision and rounding differ across platforms:
#
#   - Linux: read from /proc/<pid>/stat field 22 (start time in jiffies
#     since boot); jiffy precision is typically 0.01s but the conversion
#     to wall-clock can introduce sub-second drift.
#   - macOS: read from kinfo_proc.kp_proc.p_starttime (struct timeval,
#     microsecond precision); generally matches Linux at the second
#     boundary but can drift by a few hundred ms in samples taken
#     across the boundary.
#
# 1.0s is the conservative tolerance: wider than observed cross-platform
# drift, narrower than the smallest legitimate "PID was recycled"
# scenario (the OS typically reuses a PID only after substantial time
# elapses; truly back-to-back PID reuse with sub-second create_time
# match would require a test-bench scenario, not production traffic).
# Tightening below 1.0s risks false-positive PID-reuse detection on
# slow-clock platforms; widening above 1.0s narrows the legitimate
# PID-reuse-detection window without practical benefit.
_CREATE_TIME_TOLERANCE_SECONDS = 1.0


class CacheLockedError(Exception):
    """The cache lock could not be acquired and is not stale.

    DATA condition (a legitimate runtime state — another process holds
    the lock), not a programmer bug. The message includes the
    structured lock-file contents so operators can decide whether to
    wait, run `whatifd cache unlock`, or run `whatifd cache rebuild`.

    ## Structured-failure mapping (cardinal #8)

    Callers at the verdict-projection boundary convert this exception
    into a `FailureRecord(code=LOCK_FAILURE_CODE, ...)`. The registry
    entries already exist for the projected code:

    - `FAILURE_CODE_REGISTRY["cache_lock_unavailable"]` — the failure
      side (operational fact).
    - `FINDING_CODE_REGISTRY["cache_lock_unavailable"]` — the policy
      conclusion (`blocks_all` severity → Inconclusive).
    - `FIX_SUGGESTION_REGISTRY["cache_lock_unavailable"]` — the
      cardinal #8 actionability template, with the three
      `whatifd cache rebuild`/`unlock`/`verify` recovery paths.

    The free-text strings in this exception's message are
    *operator-facing diagnostics* (PID/hostname/started_at provenance
    that the registry templates can't carry without per-instance
    data); they do NOT replace the registry entries. The structural
    actionability promise is satisfied by the registry; the message
    is enrichment.
    """


@contextmanager
def acquire_cache_lock(
    cache_root: Path,
    *,
    stale_after_seconds: int = _DEFAULT_STALE_AFTER_SECONDS,
    allow_age_takeover: bool = False,
) -> Iterator[CacheLock]:
    """Acquire the single-writer lock on `cache_root`.

    Yields a `CacheLock` describing the held lock. On exit (normal or
    exceptional), releases `fcntl.flock` and unlinks the lock file.

    Raises `CacheLockedError` when the lock is held by another live
    process and is not stale. Operators can resolve via
    `whatifd cache unlock` (Phase 8).

    `allow_age_takeover=True` enables time-based takeover (lock older
    than `stale_after_seconds`) — opt-in only because age alone is a
    weak signal. Default behavior takes over only on dead-process or
    PID-reuse evidence (both surface via the
    `psutil.NoSuchProcess`/`create_time` mismatch path).
    """
    lock_path = cache_root / _LOCK_FILENAME
    cache_root.mkdir(parents=True, exist_ok=True)

    # Open via os.open + os.fdopen to get O_RDWR | O_CREAT semantics
    # WITHOUT O_APPEND. The earlier "a+" mode worked but had subtle
    # cross-platform read-back behavior — POSIX O_APPEND forces every
    # write to EOF atomically, which is correct for our truncate-then-
    # write takeover path (truncate sets EOF to 0, so the forced-EOF
    # write lands at byte 0) but easy to misunderstand. Using O_RDWR
    # explicitly avoids the append-mode mental model entirely:
    #   - position-0 reads: explicit fp.seek(0); fp.read()
    #   - takeover writes: explicit fp.seek(0); fp.truncate(); fp.write()
    # No O_APPEND means no platform-dependent write-positioning.
    #
    # SIM115 (use `with open(...)`) is suppressed: the file's lifetime
    # spans the entire context manager — we acquire the fcntl lock on
    # this fd, yield the CacheLock to the caller, and only release +
    # close in the conditional-acquired finally path below. A `with`
    # block would close at the wrong scope.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fp = os.fdopen(fd, "r+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as e:
            # flock refused. Inspect the existing lock file for stale
            # evidence; if stale, attempt takeover. Otherwise raise.
            if _try_takeover_if_stale(fp, lock_path, stale_after_seconds, allow_age_takeover):
                # Re-attempt flock. If the OS still refuses, the
                # original process is alive and holding the kernel
                # lock — file-level stale evidence is overridden by
                # the OS truth. The lock is NOT actually stale.
                try:
                    fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError as inner:
                    raise _build_locked_error(lock_path) from inner
            else:
                raise _build_locked_error(lock_path) from e
        except OSError as e:
            if e.errno in (errno.ENOLCK, errno.EOPNOTSUPP):
                raise CacheLockedError(
                    f"fcntl.flock returned errno={e.errno} ({errno.errorcode.get(e.errno, '?')}) "
                    f"on {lock_path}. The filesystem may not support advisory "
                    "locking — NFS clients commonly degrade silently. v0.1 "
                    "does not support NFS-mounted cache directories; use a "
                    "local filesystem or the cascade entry 'Multi-tenant "
                    "cache directories' (v0.3) for shared-host caches."
                ) from e
            raise

        content = _build_lock_content()
        fp.seek(0)
        fp.truncate()
        fp.write(canonical_json_bytes(_content_to_dict(content)).decode("ascii"))
        fp.flush()
        os.fsync(fp.fileno())

        yield CacheLock(lock_path=lock_path, content=content)
    finally:
        # Cleanup is conditional on acquisition: if we never acquired
        # (CacheLockedError path), we must NOT unlock/unlink — the
        # other process still holds it. Closing the fd is always safe.
        #
        # Cardinal #1 + cleanup-path discipline: cleanup errors must
        # NOT mask the original exception (Python re-raises from
        # finally would override the in-flight error), but they also
        # must not be silently dropped. We surface them as
        # `ResourceWarning` — visible to operators by default in CPython
        # (`-W default::ResourceWarning`) and routable through
        # `warnings.filterwarnings(...)` for CI/test discipline.
        # Matches the precedent in `whatifd/serialization/decimal.py`.
        if acquired:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            except OSError as e:
                warnings.warn(
                    f"flock(LOCK_UN) failed during cache-lock cleanup: {e!r}",
                    ResourceWarning,
                    stacklevel=2,
                )
        try:
            fp.close()
        except OSError as e:
            warnings.warn(
                f"close() failed during cache-lock cleanup: {e!r}",
                ResourceWarning,
                stacklevel=2,
            )
        if acquired:
            with contextlib.suppress(FileNotFoundError):
                # FileNotFoundError on unlink is the expected "another
                # process took over via stale-detection and unlinked
                # before us" race; benign and not warning-worthy.
                lock_path.unlink()


def _try_takeover_if_stale(
    fp: IO[str],
    lock_path: Path,
    stale_after_seconds: int,
    allow_age_takeover: bool,
) -> bool:
    """Inspect the existing lock file; return True if takeover is
    justified by stale evidence.

    Wraps the pure decision function `_should_takeover` with the file
    I/O concerns. The split keeps the decision logic unit-testable
    without filesystem state — see `tests/.../test_lock.py::TestShouldTakeover`.
    Deserialization is delegated to
    `whatifd.serialization.parse_lock_file_content`, which returns
    `None` for empty/corrupted/wrong-shape input — both surface as
    "stale by definition" here.
    """
    fp.seek(0)
    recorded = parse_lock_file_content(fp.read())
    if recorded is None:
        return True
    return _should_takeover(recorded, stale_after_seconds, allow_age_takeover)


def _should_takeover(
    recorded: LockFileContent,
    stale_after_seconds: int,
    allow_age_takeover: bool,
) -> bool:
    """Pure decision: is the recorded lock stale?

    Extracted from `_try_takeover_if_stale` so unit tests can cover
    every branch (process dead, PID recycled, age exceeded with opt-in,
    age exceeded without opt-in, all-clear) without faking file I/O or
    `fcntl` state. The integration test (real subprocess) covers the
    file-I/O wrapper; this function covers the decision matrix.
    """
    if _process_dead_or_recycled(recorded):
        return True
    return allow_age_takeover and _lock_age_exceeded(recorded, stale_after_seconds)


def _process_dead_or_recycled(recorded: LockFileContent) -> bool:
    """True if the recorded process is no longer alive OR a different
    process has been assigned the same PID since the lock was written.
    """
    try:
        proc = psutil.Process(recorded.pid)
        actual_create_time = proc.create_time()
    except psutil.NoSuchProcess:
        # Process is gone. Lock is stale.
        return True
    except psutil.AccessDenied:
        # We can't tell. Conservative: do NOT take over — better to
        # surface a CacheLockedError than to clobber a legitimate
        # process whose creation time we can't read.
        return False
    drift = abs(float(actual_create_time) - recorded.process_start_time)
    return drift > _CREATE_TIME_TOLERANCE_SECONDS


def _lock_age_exceeded(recorded: LockFileContent, stale_after_seconds: int) -> bool:
    # Python 3.11+ (project minimum) parses the `Z` suffix natively;
    # no `.replace("Z", "+00:00")` workaround needed.
    try:
        started = datetime.fromisoformat(recorded.started_at)
    except ValueError:
        # Malformed timestamp — treat as not-aged-out so we surface the
        # CacheLockedError rather than silently taking over.
        return False
    age = (datetime.now(UTC) - started).total_seconds()
    return age > stale_after_seconds


def _build_lock_content() -> LockFileContent:
    """Capture the current process's identity for the lock file.

    `psutil.Process(self).create_time()` can raise `AccessDenied` in
    hardened containers (no `CAP_SYS_PTRACE`, namespaced /proc, or
    AppArmor/SELinux profiles that block self-introspection). Without
    a readable create_time we cannot establish PID-reuse defense for
    our own lock, so we surface this as a typed `CacheLockedError`
    (DATA condition; environmental, recoverable by changing the
    container profile) rather than letting an untyped `psutil` error
    leak out.
    """
    pid = os.getpid()
    try:
        process_start_time = psutil.Process(pid).create_time()
    except psutil.AccessDenied as e:
        raise CacheLockedError(
            f"Cannot acquire cache lock: psutil.Process({pid}).create_time() "
            f"raised AccessDenied. The current container/sandbox does not "
            "permit reading the process's own create_time, which the lock's "
            "PID-reuse defense requires. Run with CAP_SYS_PTRACE, an "
            "unrestricted /proc, or a less-restrictive AppArmor/SELinux "
            "profile. (DATA condition: environmental, not a programmer bug.)"
        ) from e
    return LockFileContent(
        pid=pid,
        process_start_time=process_start_time,
        hostname=socket.gethostname(),
        started_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _content_to_dict(content: LockFileContent) -> dict[str, object]:
    return {
        "pid": content.pid,
        "process_start_time": content.process_start_time,
        "hostname": content.hostname,
        "started_at": content.started_at,
    }


def _build_locked_error(lock_path: Path) -> CacheLockedError:
    """Read the held lock for diagnostics and return a typed error
    naming the holder, hostname, and when it was acquired.

    The blocking condition (lock held) is already established by the
    caller; this function ENRICHES the error message with provenance.
    Diagnostic-read failures (file rotated, permission denied) chain
    via `__cause__` so nothing is silently swallowed. Deserialization
    goes through the same typed `parse_lock_file_content` helper used
    for stale-detection — `None` here means "file readable but content
    unparseable," and we fall back to a degraded message rather than
    fabricating provenance.
    """
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except OSError as diag_err:
        err = CacheLockedError(
            f"Cache lock at {lock_path} is held but its content is "
            f"unreadable ({type(diag_err).__name__}: {diag_err}). Run "
            "`whatifd cache unlock` if the holding process is no longer "
            "active, or `whatifd cache rebuild --force` to reset."
        )
        # `raise ... from diag_err` isn't usable here because this
        # function RETURNS the exception (the caller raises). Setting
        # __cause__ manually is the only way to chain a `raise ... from`
        # equivalent on a returned-but-not-yet-raised exception object.
        err.__cause__ = diag_err
        return err

    content = parse_lock_file_content(raw)
    if content is not None:
        return CacheLockedError(
            f"Cache lock at {lock_path} is held by another live process. "
            f"PID={content.pid}, hostname={content.hostname!r}, "
            f"started_at={content.started_at!r}. If you know the "
            "holding process is no longer running, run `whatifd cache unlock`. "
            "If the cache may be corrupted, run `whatifd cache rebuild --force`."
        )
    # File read but unparseable — degraded diagnostic, still typed.
    return CacheLockedError(
        f"Cache lock at {lock_path} is held but its content is unparseable. "
        "Run `whatifd cache unlock` if the holding process is no longer active, "
        "or `whatifd cache rebuild --force` to reset."
    )
