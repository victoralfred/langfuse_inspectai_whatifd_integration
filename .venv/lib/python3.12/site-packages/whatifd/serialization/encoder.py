"""`WhatifJSONEncoder` — artifact-write JSON encoder for `ReportV01`.

Phase 5.3 of the v0.1 implementation plan; pairs with Phase 5.1
(`models_v01.py`) and Phase 5.2 (`projection.py`). The full Phase 5
artifact-write chain:

  internal types
    → `project_to_report_v01(...)` → `ReportV01` (5.2)
    → `assert_no_unredacted_sensitive(report)` (5.4, deferred)
    → `WhatifJSONEncoder().encode(report)` (this module)
    → bytes on disk

## What this encoder does

Extends `json.JSONEncoder` with a `default(obj)` override that
handles the project's typed shapes:

- **`Sensitive[T]`** → raises `UnredactedSensitiveError`. This is the
  CARDINAL #5 LAST LINE OF DEFENSE. The graph walk (5.4) is the
  primary defense; this encoder catches anything that slipped past.
  No silent redaction at this layer — the artifact write fails
  loud, never produces a leaked-content report.
- **Frozen dataclasses** → shallow field projection
  (`{f.name: getattr(obj, f.name)}`). Each field's value flows back
  through `default()` via json's recursive walk; nested dataclasses,
  Mappings, and sets resolve through the same dispatch.
  Deliberately NOT `dataclasses.asdict` — that helper deep-copies
  values via `copy.copy`, which chokes on `MappingProxyType` (used
  in `CacheSummary.models_distribution` and `CacheMeta.extra`).
- **`Mapping`** (incl. `MappingProxyType`) → cast to `dict`. The
  forward-compat `extra` field on `CacheMeta` and
  `models_distribution` on `CacheSummary` use these.
- **`tuple` / `frozenset`** → cast to `list`. JSON has no native
  tuple/set; lists preserve order. Sets are sorted to make the
  output deterministic.

## Determinism (cardinal #4)

The `encode_report_v01(report) -> bytes` helper wraps
`WhatifJSONEncoder` with the same canonical kwargs as
`canonical_json_bytes`:

- `sort_keys=True` — deterministic across dict-insertion-order changes.
- `separators=(",", ":")` — no whitespace.
- `ensure_ascii=True` — escape non-ASCII for byte-identical output
  regardless of host locale.

Same input → byte-identical bytes. Phase 5.5 schema-match test runs
the same encode twice on identical input and asserts byte equality.

## Banned-import lint scope

Per `references/enforcement.md` row 2, `json.dumps` is banned
outside `whatifd/serialization/`. This module IS in the serialization
package, so the import is sanctioned. Test
`tests/unit/whatifd/serialization/test_banned_imports.py` walks
`src/whatifd/` AST and asserts zero `json.dumps` calls outside the
serialization package.

## Cardinal alignment

- **#1 (failures-as-data):** the encoder never silently swallows; an
  un-encodable type raises `TypeError` from stdlib's `default()`
  fallback if `WhatifJSONEncoder.default()` doesn't recognize it.
  Adding new types means extending `default()`, not papering over.
- **#5 (sensitive data wrapped):** `Sensitive[T]` raises rather than
  emits. Three layers per `enforcement.md`:
  - Type-level: mypy strict on `Sensitive[T]` fields.
  - Pre-write graph walk (5.4): `assert_no_unredacted_sensitive`.
  - Encoder default (this module): last line.
- **#6 (typed boundaries):** `encode_report_v01(report: ReportV01) ->
  bytes` takes the typed wire shape. No `dict[str, Any]` boundary.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from whatifd.types.sensitive import Sensitive, UnredactedSensitiveError

if TYPE_CHECKING:
    # Type-only import: ReportV01 lives in `whatifd.report.models_v01`,
    # which transitively imports `whatifd.serialization` (via
    # `whatifd.cache`). A runtime import here would cycle. The
    # function body uses `json.dumps` which is duck-typed on any
    # object — the class is only needed for the type annotation,
    # which `from __future__ import annotations` keeps as a string
    # at module load.
    from whatifd.report.models_v01 import ReportV01


class WhatifJSONEncoder(json.JSONEncoder):
    """Artifact-write encoder for the whatifd report shape.

    Use via `encode_report_v01(report)` for the canonical-encode path
    that stamps determinism kwargs (sort_keys, separators,
    ensure_ascii). Direct `WhatifJSONEncoder()` instantiation is
    supported for tests and advanced callers but loses the
    determinism wrapper unless the caller passes the same kwargs to
    `encode()`.
    """

    def default(self, obj: Any) -> Any:
        # Cardinal #5 last line: any Sensitive[T] reaching the
        # encoder means the graph walk (5.4) missed it OR was bypassed.
        # Raise rather than emit — the artifact write fails loud.
        if isinstance(obj, Sensitive):
            raise UnredactedSensitiveError(
                f"WhatifJSONEncoder received a Sensitive[{obj.classification}] "
                "instance during artifact serialization. The pre-serialization "
                "graph walk (assert_no_unredacted_sensitive, Phase 5.4) is the "
                "primary defense; this fail-loud is the last line. Either the "
                "value should be redacted at the boundary (Sensitive.unwrap "
                "with audited reason) or the artifact-bundle profile must "
                "exclude it. Cardinal #5."
            )

        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            # Shallow field-by-field projection. `dataclasses.asdict`
            # would also work, BUT it deep-copies values via copy.copy,
            # which chokes on `MappingProxyType` (used by
            # `CacheSummary.models_distribution` and `CacheMeta.extra`).
            # The shallow projection lets json's recursive walk call
            # `default()` again on each value — Mappings, sets, nested
            # dataclasses all flow through this same dispatch.
            return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}

        if isinstance(obj, Mapping):
            # MappingProxyType, dict subclasses, etc. JSON only allows
            # string keys, so coerce non-str keys via str().
            #
            # Caller contract: every Mapping field on the wire shape
            # is typed `Mapping[str, ...]` (cardinal #6 — typed
            # boundaries). Non-str keys reaching this branch indicate
            # a contract violation upstream; the silent str() coercion
            # is best-effort emergency dispatch, NOT a sanctioned
            # feature. A future strict-mode flag could raise on
            # non-str keys; for v0.1 the type system catches the
            # contract violation at compile time and the silent
            # coercion is acceptable for the rare runtime escape.
            return {str(k): v for k, v in obj.items()}

        if isinstance(obj, frozenset | set):
            # Sorted by str(item) for determinism — lexicographic
            # order applied via str repr. Sets in the wire shape are
            # rare (we prefer tuples for ordered immutability), and
            # the elements are typically string-typed already
            # (cohort names, model ids, etc.), so str-keyed sort
            # produces the natural lexicographic order.
            #
            # Why str() and not the natural < comparator: heterogeneous
            # sets (e.g., a future {1, "a", None}) would raise on
            # natural sort; str() keeps the dispatch total. If a future
            # caller passes a set with naturally-comparable elements
            # AND wants natural-order output, they should pre-convert
            # to a sorted tuple at the boundary rather than rely on
            # set serialization.
            return sorted(obj, key=str)

        # tuple is handled by stdlib (becomes list); no override needed.

        # Fall through to stdlib's default which raises TypeError —
        # cardinal #1: an unknown type is failures-as-data, not silent.
        return super().default(obj)


def encode_report_v01(report: ReportV01) -> bytes:
    """Encode a `ReportV01` to canonical-form JSON bytes.

    Determinism kwargs match `canonical_json_bytes` (the hash-input
    encoder): `sort_keys=True`, `separators=(",", ":")`,
    `ensure_ascii=True`. Same input → byte-identical output across
    platforms and Python versions.

    The function takes `ReportV01` specifically (not `Any`) because
    cardinal #6 says public-schema types are hand-written; the
    artifact-write path consumes the typed wire shape, not arbitrary
    objects. mypy strict catches wrong-type calls at type-check
    time; the `isinstance` guard below is defense-in-depth for
    runtime callers that bypass type-checking (e.g., dynamic CLI
    paths, REPL usage, tests that intentionally pass garbage).
    Tests that need to encode partial structures use
    `WhatifJSONEncoder()` directly with their own kwargs.
    """
    # TODO(phase8): every caller of `encode_report_v01` must first run
    # `assert_no_unredacted_sensitive(report)` (graph_walk.py — cardinal
    # #5 layer (b)). The encoder's `default()` raise is the last-line
    # fallback, NOT the primary defense. The CLI artifact-write path
    # (`whatifd fork`) wires the sequence in Phase 8; the Phase 9
    # integration test pins it. See cascade-catalog entry
    # "Artifact-write call-site sequencing for graph walk".

    # Lazy import to avoid the encoder/report/cache circular load.
    # Documented at the module top where the TYPE_CHECKING import lives.
    from whatifd.report.models_v01 import ReportV01 as _ReportV01

    if not isinstance(report, _ReportV01):
        raise TypeError(
            f"encode_report_v01 expects a ReportV01 instance; got "
            f"{type(report).__name__!r}. Cardinal #6: the artifact-write "
            "boundary is typed. mypy strict catches this at type-check "
            "time; the runtime guard exists for callers that bypass "
            "type-checking."
        )
    return json.dumps(
        report,
        cls=WhatifJSONEncoder,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
