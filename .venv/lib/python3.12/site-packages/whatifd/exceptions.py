"""Cross-cutting exceptions that mark structural-integrity violations.

Per cardinal rule #1: expected failures are data (`FailureRecord`,
`DecisionFinding`); bugs propagate. The exceptions in this module
mark the boundary — they fire only when an upstream caller has
violated a declared type contract (e.g., a `DecimalString` that
doesn't parse as a decimal, a `Sensitive[T]` reaching the serializer
unwrapped). They are NOT for runtime data conditions the report
should describe.

If you find yourself reaching for `InvariantViolation` to handle a
data condition that could legitimately occur in production, you
likely want a `FailureRecord` or `DecisionFinding` instead — wrap
the condition as data and let the verdict layer decide.
"""

from __future__ import annotations


class InvariantViolationError(Exception):
    """A structural type contract was violated upstream.

    Surfaces upstream bugs (wrong type stuffed into a typed slot,
    pre-condition violated by a caller) instead of silently abstaining
    or emitting a misleading verdict. Subclasses `Exception` directly
    rather than `ValueError`/`RuntimeError` so call-site intent is
    legible: this is a contract violation, not a value-domain error.
    """
