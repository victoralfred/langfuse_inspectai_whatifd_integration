"""`whatifd.report` — public-schema-versioned report types.

Phase 5 of the v0.1 implementation plan. The full Phase 5 surface
includes:

- `models_v01.py` — `ReportV01` and its dependent typed sub-shapes
  (Phase 5.1, this PR). Hand-written per cardinal #6 — public schema
  is hand-written; internal types refactor freely.
- `projection.py` — `project_to_report_v01(internal_state) -> ReportV01`
  (later sub-phase). Flattens internal types (Verdict sealed union,
  etc.) into the wire shape.
- `schema/v0.1.schema.json` — generated, byte-stable, committed (later
  sub-phase). The schema match test asserts zero drift between code
  and committed schema.

The split between INTERNAL types (`whatifd/types/`) and PUBLIC types
(`whatifd/report/models_v01.py`) is the cardinal #6 boundary. Internal
types may refactor freely between minor versions; public types are
the wire contract and require schema-version bumps to change.
"""

from whatifd.report.models_v01 import (
    REPORT_SCHEMA_URI,
    REPORT_SCHEMA_VERSION,
    ReportV01,
    VerdictState,
)
from whatifd.report.projection import project_to_report_v01

__all__ = (
    "REPORT_SCHEMA_URI",
    "REPORT_SCHEMA_VERSION",
    "ReportV01",
    "VerdictState",
    "project_to_report_v01",
)
