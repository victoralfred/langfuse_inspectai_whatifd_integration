"""`assert_no_unredacted_sensitive` — pre-serialization redaction guard.

Phase 5.4 of the v0.1 implementation plan; pairs with Phase 5.1
(types), 5.2 (projection), 5.3 (encoder + banned-import). This is
**layer (b) of the cardinal #5 three-layer defense** per
`references/enforcement.md` row 2:

  (a) `Sensitive[T]` type wrapper enforced via mypy strict.
  (b) Pre-serialization graph walk (THIS MODULE) before any artifact
      write.
  (c) `WhatifJSONEncoder.default()` raises `UnredactedSensitiveError`
      as last line (Phase 5.3).

The graph walk is the PRIMARY runtime defense — it catches every
`Sensitive[T]` reachable from the report's object graph before json
serialization runs. The encoder fallback is the safety net for the
rare case where a graph-walk path was missed.

## Why a recursive walk

A `ReportV01` is a tree of frozen dataclasses, lists, tuples,
mappings, and primitives. Each node may carry a `Sensitive[T]` if
the boundary upstream (adapter ingress, Phase 4) failed to wrap or
unwrap correctly. The walk visits every reachable value:

- `Sensitive[T]` → raise immediately, no further traversal.
- Frozen dataclasses → recurse over `dataclasses.fields(obj)` values.
- Mappings → recurse over keys AND values (a Sensitive in a key is
  as bad as one in a value).
- Lists, tuples, sets, frozensets → recurse over elements.
- Primitives (str, int, float, bool, None, bytes, Path, datetime) →
  no recursion needed; not container-shaped.

A `_seen` set tracks visited dataclass / collection identities to
break reference cycles. The wire shape is acyclic by design (it's a
tree), but the walk is robust to a future cycle without crashing.

## Caller contract

```python
from whatifd.serialization import assert_no_unredacted_sensitive

assert_no_unredacted_sensitive(report)  # raises if any Sensitive[T] found
encoded = encode_report_v01(report)     # safe to serialize
write(encoded)
```

The `whatifd fork` artifact-write path runs the walk immediately
before `encode_report_v01` so a Sensitive leak fails the write
loudly with a typed error pointing at the problem path.

## What the walk does NOT cover

- `Sensitive[T]` instances stored as attributes on a class that's
  NOT a dataclass and NOT a known container — the walk has no way
  to discover them. v0.1 ReportV01 has no such types; if a v0.2
  introduces one, extending this walk is the right place.
- Module-level globals or class-level attributes on dataclass types
  themselves (only INSTANCE values are walked).
- `Sensitive[T]` masquerading as a non-Sensitive subclass — the
  `isinstance(obj, Sensitive)` check uses runtime type, so a class
  hierarchy that inherits from Sensitive will be detected.

## Cardinal alignment

- **#1 (failures-as-data):** `UnredactedSensitiveError` is a typed
  exception; callers convert to `FailureRecord` if needed. Not a
  silent log.
- **#5 (sensitive data wrapped):** the load-bearing cardinal-#5
  enforcement at the artifact-write boundary. Combined with
  type-level (a) and encoder fallback (c), the three layers ensure
  no Sensitive[T] leaves the process unredacted.
- **#9 (orchestration not compute):** pure-Python recursion, no
  CPU optimization, no shared-memory tricks. The walk is bounded by
  the report's object graph (small, hundreds of nodes).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from whatifd.types.sensitive import Sensitive, UnredactedSensitiveError

__all__ = ["assert_no_unredacted_sensitive"]


def assert_no_unredacted_sensitive(obj: Any, *, path: str = "<root>") -> None:
    """Walk `obj` recursively; raise `UnredactedSensitiveError` if any
    `Sensitive[T]` instance is reachable.

    The optional `path` parameter is a breadcrumb string the walk
    extends as it descends — when an error fires, the message names
    the path to the offending value (e.g.,
    `<root>.runtime.sensitive_unwraps[3].location`). Operators
    debugging a leak can locate the unredacted call site
    immediately.

    Returns `None` on success (the report is safe to serialize). The
    walk is read-only — it never mutates the input.
    """
    seen: set[int] = set()
    _walk(obj, path, seen)


def _walk(obj: Any, path: str, seen: set[int]) -> None:
    """Recursive workhorse. Separated from the public entry point so
    the `seen` cycle-guard is initialized once per top-level call.
    """
    # Cardinal #5: the load-bearing check. Any Sensitive[T] instance
    # reachable from the graph is a leak — fail loud BEFORE the
    # encoder gets a chance to fall back.
    #
    # ORDERING: this isinstance check MUST stay before the cycle-guard
    # `seen.add(id(obj))` below. A future refactor that swaps the
    # blocks would cause a Sensitive that's been visited once (e.g.,
    # via two paths in a shared subtree) to be silently skipped on the
    # second encounter — defeating the load-bearing defense.
    if isinstance(obj, Sensitive):
        raise UnredactedSensitiveError(
            f"unredacted Sensitive[{obj.classification}] found at {path}. "
            "Cardinal #5: every Sensitive[T] must be unwrapped via "
            "`.unwrap(reason=...)` (audited) or transformed via "
            "`whatifd.serialization.redaction.redact()` (Phase 5+ when it "
            "lands) before reaching the artifact-write boundary. The "
            "encoder fallback (`WhatifJSONEncoder.default()`) would also "
            "catch this, but the graph walk is the primary defense."
        )

    # Cycle guard: track visited container identities. Primitives
    # don't need cycle tracking (they can't reference back).
    if isinstance(obj, str | bytes | int | float | bool) or obj is None:
        return

    obj_id = id(obj)
    if obj_id in seen:
        return
    seen.add(obj_id)

    # Frozen dataclasses: recurse over field values via getattr.
    # Note: `dataclasses.is_dataclass` is True for both classes AND
    # instances; the `not isinstance(obj, type)` guard filters out
    # the class object itself.
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for field in dataclasses.fields(obj):
            value = getattr(obj, field.name)
            _walk(value, f"{path}.{field.name}", seen)
        return

    # Mappings: walk keys AND values (a Sensitive in a key is just
    # as bad as in a value).
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            _walk(key, f"{path}.<key:{key!r}>", seen)
            _walk(value, f"{path}[{key!r}]", seen)
        return

    # Sequences and sets: walk elements.
    if isinstance(obj, list | tuple):
        for i, value in enumerate(obj):
            _walk(value, f"{path}[{i}]", seen)
        return

    if isinstance(obj, set | frozenset):
        # Sets aren't ordered; index meaningless, use repr for path.
        for value in obj:
            _walk(value, f"{path}[<set:{value!r}>]", seen)
        return

    # Anything else (Path, datetime, custom non-dataclass classes
    # with no container shape): no recursion, not a Sensitive[T].
    # The encoder will either handle these via its own dispatch or
    # fail loud with TypeError per cardinal #1.
    return
