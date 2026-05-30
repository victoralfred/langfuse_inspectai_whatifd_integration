"""Wire-boundary helpers for `whatifd.statistical` outputs.

Cardinal #4 (determinism opt-in per field): in-memory floats are
not byte-stable across platforms; the wire-canonical view is
`DecimalString`. This module's `to_decimal_string` wraps the
common `f"{value:.{precision}f}"` boilerplate so callers wiring
`BootstrapResult` (or any other statistical output) into a wire
shape don't have to repeat the format string at every site.

Lives in its own module (not on the bootstrap helper itself)
because it's a generic boundary helper — Holm-correction outputs
in Phase E.3 will use the same helper.
"""

from __future__ import annotations

from whatifd.types.primitives import DecimalString


def to_decimal_string(value: float, precision: int = 3) -> DecimalString:
    """Format a float as a `DecimalString` for the wire boundary.

    `precision` defaults to 3 — the convergent display precision
    for v0.1 / v0.2 cohort medians and CI bounds. Callers wanting
    different precision (e.g., a probability surface that needs
    4-5 decimal places) override explicitly.

    Cardinal #1: a negative `precision` raises `ValueError`. The
    underlying `f"{x:.{n}f}"` would itself raise on negative `n`,
    but the message is unstructured (`ValueError: Precision not
    allowed in integer format specifier` or similar); converting
    to a named guard at the helper boundary gives an actionable
    error.

    Cardinal #4: the wire shape is the deterministic surface.
    Floats produced by the bootstrap or any other statistical
    function MUST round-trip through this helper before crossing
    into a `CohortResult` / `ReportV01` field.
    """
    if precision < 0:
        raise ValueError(
            f"to_decimal_string: precision must be >= 0, got {precision}. "
            "Use precision=0 for integer-precision wire output."
        )
    return DecimalString(f"{value:.{precision}f}")
