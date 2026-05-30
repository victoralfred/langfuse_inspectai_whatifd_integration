"""Report-JSON load helper — keeps `json.loads` inside the serialization
boundary (cardinal #5 module-discipline).

`canonical_json_bytes` (write side) already lives here; this module is
the matching read-side helper. Callers outside `whatifd.serialization.*`
should NOT call `json.loads` on report-shaped JSON directly.

The helper is intentionally thin: it returns the raw `dict[str, Any]`
wire shape, NOT a typed `ReportV01`. Typed instantiation belongs to the
projection / migration layer because v0.X dicts may legitimately lack
fields required at v0.Y.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeAlias

# Defined locally (not re-exported from `whatifd.report.migrate`) to
# avoid a circular import: `serialization` is initialized eagerly via
# `whatifd/serialization/__init__.py`, while `report.migrate` imports
# from `report.models_v01`, which transitively pulls serialization.
# The TypeAlias is a zero-cost duplicate signaling the same cardinal #6
# wire-shape boundary.
RawReport: TypeAlias = dict[str, Any]


class ReportLoadError(Exception):
    """Structured I/O or parse error reading a report file (cardinal #1)."""


def load_report_json(path: Path) -> RawReport:
    """Read and JSON-decode a report file.

    Returns the raw wire shape; callers (migrator, validator) are
    responsible for any further structural checks.

    Raises `ReportLoadError` on missing file, permission error, or
    JSON-decode failure. Cardinal #1: never lets a stdlib exception
    escape unwrapped.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportLoadError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReportLoadError(f"cannot parse {path} as JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReportLoadError(f"{path}: report must be a JSON object, got {type(data).__name__}")
    return data
