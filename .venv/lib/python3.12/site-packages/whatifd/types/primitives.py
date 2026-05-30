"""Primitive type aliases used across the internal type model.

`DecimalString` and `JsonPrimitive` are the smallest building blocks.
Everything else in `whatifd/types/` builds on these.

`DecimalString` is a `NewType` over `str` that represents a numeric value
already serialized to a fixed-precision decimal string. Float arithmetic
happens internally; emission of values that cross the determinism boundary
uses `format(value, '.3f')` which is platform-stable. Cardinal rule #4
(determinism opt-in per field) is enforced at the type level: any field
in the determinism budget that carries a number is typed `DecimalString`,
not `float` — that way, the type checker catches an accidental float
landing in a deterministic field before it can produce platform-dependent
output.

The format/parse helpers live in `whatifd/serialization/decimal.py`
(Phase 5). This module deliberately holds only the type alias so that
`import whatifd.types.primitives` is essentially zero-cost (no dependency
on serialization machinery).

`JsonPrimitive` is a union of the JSON-compatible scalar types, used
in extension-point `details` mappings (`FailureRecord.details`,
`DecisionFinding.details`, `RunManifest.environment`). Per cardinal rule
#6 (public schema hand-written), these mappings are the only place
arbitrary values are accepted across the public schema, and even then
only as JSON primitives — never `Any`.
"""

from __future__ import annotations

from typing import NewType

# Numeric values in the determinism budget are emitted as fixed-precision
# decimal strings to escape platform-dependent float serialization. See
# `whatifd/serialization/decimal.py` (Phase 5) for format/parse helpers.
DecimalString = NewType("DecimalString", str)

# Union of JSON-compatible scalar types. Used in extension-point `details`
# mappings to bound what schema consumers must accept without falling back
# to `Any`.
JsonPrimitive = str | int | float | bool | None
