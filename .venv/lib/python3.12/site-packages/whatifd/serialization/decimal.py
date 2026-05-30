"""DecimalString parse helper — early-shipped subset of Phase 5 serialization.

Phase 5 will land both halves of the determinism boundary:
  - `format_decimal_string(value: float) -> DecimalString` (Phase 5)
  - `parse_decimal_string(value: DecimalString, *, field: str) -> float` (here)

The parse half ships in Phase 2.5 because guards and the verdict layer
need to validate structural integrity on `DecimalString` fields before
the format half is needed (format is for emission; parse is for
ingestion of already-stored deterministic values).

Per cardinal #1, a `DecimalString` that doesn't parse is an upstream
contract violation, not a runtime data condition. The helper raises
`InvariantViolationError` (typed; not stdlib `ValueError`) so call-site
intent is legible.

The current implementation accepts anything `float()` parses, then
emits a `FutureWarning` on inputs that violate the committed canonical
shape (fixed-precision: decimal point with at least one fractional
digit; no scientific notation). Phase 5 flips the warning to a hard
reject and pins exact precision per field. The cascade-catalog entry
"`parse_decimal_string` permissiveness — tighten at Phase 5" tracks
the deferral rationale and the tests that flip from `pytest.warns` to
`pytest.raises` at that point.

`FutureWarning` (not `DeprecationWarning`) is intentional: Python's
default warning filters silence `DeprecationWarning` in library code
(only shown when running as `__main__`). `FutureWarning` is shown by
default to end users — surfacing drift early was the point of the
soft warning, so the more visible category is the right pick.
"""

from __future__ import annotations

import re
import warnings
from typing import NewType

from whatifd.exceptions import InvariantViolationError
from whatifd.types.primitives import DecimalString

# A diagnostic-only label identifying the source field of a parse call
# (e.g., `"CohortResult.median_delta"`). NewType-wrapped to make the
# metadata-only role legible at the type level — call sites must
# explicitly wrap their string with `FieldLabel(...)`, which signals
# "this is not user data; it's an error-message identifier".
FieldLabel = NewType("FieldLabel", str)

# Canonical shape: optional minus, one or more digits, REQUIRED decimal
# point, one or more fractional digits. No leading `+`, no scientific
# notation, no bare integers. Phase 5 will narrow this further (exact
# precision per field), but the shape commitment is firm today.
_CANONICAL_DECIMAL_RE = re.compile(r"^-?\d+\.\d+$")


def parse_decimal_string(value: DecimalString, *, field: FieldLabel) -> float:
    """Parse a `DecimalString` to `float`; raise `InvariantViolationError`
    if the string cannot be parsed.

    `field` is a caller-provided label (e.g., `"CohortResult.median_delta"`,
    `"trace_delta on cohort='failure'"`) that appears in the error
    message for diagnostic clarity. It does NOT affect parse logic — it's
    metadata only.

    Emits a `FutureWarning` for inputs that parse but violate the
    committed canonical shape (no decimal point, scientific notation,
    leading `+`). Phase 5 will tighten the warning into a hard reject;
    the warning gives drift visibility now without pre-committing to
    exact per-field precision. `FutureWarning` is used (not
    `DeprecationWarning`) because the default warning filters silence
    `DeprecationWarning` outside `__main__`, which would defeat the
    drift-surface purpose.

    Returns: the parsed float value.

    Raises: `InvariantViolationError` if `value` is not parseable as a
    decimal. The underlying `ValueError` is chained via `__cause__`.
    """
    try:
        result = float(value)
    except ValueError as e:
        raise InvariantViolationError(
            f"DecimalString must be parseable as a number; got {value!r} (field={field!r})"
        ) from e

    if not _CANONICAL_DECIMAL_RE.match(value):
        warnings.warn(
            f"DecimalString {value!r} (field={field!r}) is non-canonical: "
            f"expected fixed-precision form like '0.310' or '-0.050'. "
            f"Phase 5 will reject scientific notation and bare integers.",
            FutureWarning,
            stacklevel=2,
        )
    return result
