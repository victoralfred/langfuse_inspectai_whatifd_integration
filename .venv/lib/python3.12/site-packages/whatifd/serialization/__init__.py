"""Serialization layer — Phase 5 in full; some helpers ship earlier.

The full serialization module lands in Phase 5 and includes:
  - `WhatifJSONEncoder` (banned outside this package; cardinal #5)
  - `assert_no_unredacted_sensitive` (graph walk; cardinal #5)
  - `format_decimal_string` (the format half of the determinism budget)
  - `parse_decimal_string` (the parse half — already here)
  - `canonical_json_bytes` (hash-input canonical form — already here)
  - `extract_deterministic_subset` (reads `x-deterministic`; cardinal #4)

`parse_decimal_string` ships ahead of the rest because Phase 2.5+ guards
need it to enforce structural integrity on `CohortResult.median_delta`.
`canonical_json_bytes` ships ahead because Phase 3.1 cache keying needs
it for deterministic hash inputs. The early deliveries are intentional;
Phase 5 fills in the surrounding helpers without disturbing these
contracts.
"""

from whatifd.serialization.canonical import canonical_json_bytes
from whatifd.serialization.decimal import FieldLabel, parse_decimal_string
from whatifd.serialization.encoder import WhatifJSONEncoder, encode_report_v01
from whatifd.serialization.graph_walk import assert_no_unredacted_sensitive
from whatifd.serialization.load import ReportLoadError, load_report_json
from whatifd.serialization.lock_io import parse_lock_file_content

__all__ = [
    "FieldLabel",
    "ReportLoadError",
    "WhatifJSONEncoder",
    "assert_no_unredacted_sensitive",
    "canonical_json_bytes",
    "encode_report_v01",
    "load_report_json",
    "parse_decimal_string",
    "parse_lock_file_content",
]
