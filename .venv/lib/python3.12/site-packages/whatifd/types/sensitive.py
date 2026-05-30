"""`Sensitive[T]` redaction wrapper — cardinal rule #5.

Adapters wrap user content (trace inputs, judge rationale, raw scorer
outputs) as `Sensitive[T]` at their boundary. Core's serializer refuses
to write unwrapped sensitive values via three layers of defense:

1. **Type-level (mypy strict)** — adapters return `Sensitive[str]`;
   core types accept `Sensitive[str]` for sensitive fields. mypy catches
   misuse at type-check time. Lives in this module.
2. **Pre-serialization graph walk** — `assert_no_unredacted_sensitive(obj)`
   in `whatifd/serialization/graph_walk.py` (Phase 5) walks the full
   object graph before any artifact write and raises on any `Sensitive`
   instance. Catches `dataclasses.asdict()` paths that lose type info.
3. **Encoder fallback** — `WhatifJSONEncoder.default()` in
   `whatifd/serialization/encoder.py` (Phase 5) raises
   `UnredactedSensitiveError` if a `Sensitive` reaches it. Last line.

The discipline inversion: instead of "audit every serialization path,"
audit becomes "grep for `.unwrap(`." Every unwrap is a reviewable
call site with a logged reason that lands in
`manifest.runtime.sensitive_unwraps`.

Phase 1.2 lands the wrapper, the audit-log record type, the in-process
audit collector, and the two exception types. Phases 5 and 6 wire the
serializer-side defenses; Phase 4 wires the adapter-side wrapping.
"""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class SensitiveSerializationError(Exception):
    """Raised when a `Sensitive[T]` instance hits a serialization path
    (pickle, json) without first being unwrapped with an audit reason.

    Distinct from `UnredactedSensitiveError` — this fires at the
    `__reduce__` / encoder default boundary; that one fires at the
    pre-serialization graph walk before encoding starts.
    """


class UnredactedSensitiveError(Exception):
    """Raised by the pre-serialization graph walk and the JSON encoder
    when a `Sensitive[T]` is found in an artifact about to be written.

    The expected pattern: adapter wraps user content as `Sensitive[T]`;
    redaction logic in `whatifd/serialization/redaction.py` (Phase 5)
    transforms it to a `RedactedValue` per the configured profile;
    the serializer sees a `RedactedValue`, not a `Sensitive[T]`, and
    accepts it. If a `Sensitive[T]` reaches the serializer, redaction
    was skipped — that's a bug.
    """


@dataclass(frozen=True, slots=True)
class SensitiveUnwrap:
    """Audit record for a single `.unwrap()` call.

    Persisted in `RunManifest.runtime.sensitive_unwraps` (non-deterministic
    ordering — wall-clock dependent). The schema for that field marks it
    `x-deterministic: false`.

    Defined here, not in `whatifd/types/manifest.py`, to avoid a circular
    import between sensitive ↔ manifest. Manifest imports from here.
    """

    classification: str
    reason: str
    location: str  # call-site, e.g., "whatifd/render/markdown.py:render_evidence:147"


class _AuditLog:
    """In-process collector for `SensitiveUnwrap` records.

    Thread-safe append; `drain()` returns and clears the buffer atomically.
    A single module-level instance (`_audit_log`) is shared across the
    process. Multi-run isolation in v0.1 relies on each run draining the
    log into its own manifest before the next run begins; v0.2 may move
    to a `contextvars.ContextVar` for stronger isolation if concurrent
    runs become a real use case.
    """

    def __init__(self) -> None:
        self._records: list[SensitiveUnwrap] = []
        self._lock = threading.Lock()

    def record(self, event: SensitiveUnwrap) -> None:
        with self._lock:
            self._records.append(event)

    def drain(self) -> list[SensitiveUnwrap]:
        """Return and clear the accumulated audit records."""
        with self._lock:
            records, self._records = self._records, []
        return records

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


# Module-private singleton; tests use drain() to reset between cases.
_audit_log = _AuditLog()


def _infer_caller(skip: int = 2) -> str:
    """Identify the call site of `.unwrap()`.

    Walks up the stack to the caller of `.unwrap`, returning a string of
    the form `<module>:<function>:<lineno>`. The default `skip=2` accounts
    for this function plus `Sensitive.unwrap` itself.

    Falls back to `"<unknown>"` if frame inspection fails (e.g., under
    a packer that strips frames). Audit records remain valid; the
    location field is best-effort.
    """
    try:
        frame = inspect.currentframe()
        for _ in range(skip):
            if frame is None:
                return "<unknown>"
            frame = frame.f_back
        if frame is None:
            return "<unknown>"
        module = frame.f_globals.get("__name__", "<unknown>")
        function = frame.f_code.co_name
        lineno = frame.f_lineno
        return f"{module}:{function}:{lineno}"
    except (AttributeError, TypeError, ValueError):
        # Narrow catch: frame attributes can disappear under exotic
        # interpreters (PyPy, frame-stripping packers) or in finalizer
        # contexts. We don't catch broader exceptions because audit-log
        # location is metadata enrichment, not a verdict-affecting path —
        # if something else goes wrong here, it's a bug, not a known
        # tolerable mode.
        return "<unknown>"


class Sensitive(Generic[T]):
    """Wrapper that defaults to redacted serialization.

    Any direct serialization path (`repr`, `str`, f-string `__format__`,
    `pickle.dumps`) produces a redacted form. Unwrapping requires explicit
    `.unwrap(reason=...)` which audit-logs to `_audit_log`.

    `__slots__` is set so attribute assignment is impossible after
    construction — the only state is the wrapped value and the
    classification string.

    Generic over T so the wrapped type is preserved through the type
    system: `Sensitive[str]` for trace text, `Sensitive[bytes]` for
    binary payloads, etc. mypy strict catches type misuse.
    """

    __slots__ = ("_value", "classification")

    # Class-level type annotations so mypy strict sees these attributes
    # as declared. `__slots__` alone doesn't carry types.
    _value: T
    classification: str

    def __init__(self, value: T, classification: str) -> None:
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "classification", classification)

    def __setattr__(self, name: str, value: object) -> None:
        # Block post-construction reassignment. __slots__ alone permits
        # reassigning declared names; we want frozen-dataclass semantics
        # without the dataclass machinery (Generic[T] + __slots__ +
        # frozen=True is fragile in some Python versions). __init__ uses
        # object.__setattr__ to bypass this guard exactly once.
        raise AttributeError(
            f"Sensitive[{getattr(self, 'classification', '?')}] is immutable "
            f"after construction; cannot set {name!r}"
        )

    def __repr__(self) -> str:
        return f"<Sensitive[{self.classification}] redacted>"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, format_spec: str) -> str:
        # f-string `f"{x}"` calls __format__, not __str__. Override here too
        # so f-strings can never accidentally leak a wrapped value.
        return self.__repr__()

    def __reduce__(self) -> Any:
        # Block pickle. The error message points at the right fix.
        raise SensitiveSerializationError(
            f"Cannot pickle Sensitive[{self.classification}]; "
            f"call .unwrap(reason=...) first or transform via "
            f"whatifd.serialization.redaction.redact()"
        )

    def unwrap(self, *, reason: str, location: str | None = None) -> T:
        """Explicit unwrap. The reason argument is recorded and persisted
        in `manifest.runtime.sensitive_unwraps`.

        Pass `location` only when the inferred caller name is wrong
        (rare). The default uses `inspect.currentframe()` to identify
        the call site automatically.
        """
        _audit_log.record(
            SensitiveUnwrap(
                classification=self.classification,
                reason=reason,
                location=location or _infer_caller(),
            )
        )
        # Cast through `object.__getattribute__` to keep strict type
        # checkers happy: __slots__ + frozen-ish access. The runtime is
        # just attribute lookup.
        return self._value
