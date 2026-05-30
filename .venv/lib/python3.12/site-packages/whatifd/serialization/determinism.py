"""Deterministic-subset extraction — Phase 9A.3.

Reads the committed JSON Schema (`v0.1.schema.json`) to discover
which top-level `ReportV01` fields are tagged `x-deterministic: true`
and projects a serialized report down to that subset. The
deterministic subset is what the determinism CI test compares
byte-for-byte across re-runs of the same fixture; non-deterministic
fields (timestamps, environment fingerprints, sensitive-unwrap
audit logs) live under `runtime` and are excluded.

## Why schema-driven, not a hardcoded list

The `x-deterministic` annotations live on the schema; the schema
is the source of truth (cardinal #4: determinism is opt-in per
field). Hardcoding a list here would drift the moment the schema
adds or removes a tagged field — the extractor must follow the
schema, not the other way around.

## Cardinal #4 alignment

Determinism is opt-in **per field**, not per object. v0.1 ships
top-level scalar/list fields tagged `x-deterministic: true` and
the entire `runtime` subtree tagged `false`. The extractor
operates at the top level only — nested-field determinism (e.g.,
`runtime.config_hash` is deterministic even though `runtime` is
not) is the determinism *claim*, not the comparison surface. The
report's deterministic subset is the part that should be byte-
equal across re-runs; for v0.1 that's everything except `runtime`.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files
from typing import TYPE_CHECKING, Any, TypedDict, Union

if TYPE_CHECKING:
    from whatifd.report.models_v01 import ReportV01

# Functional TypedDict form because `x-deterministic` carries a
# hyphen and isn't a valid Python identifier. `total=False` because
# the schema's per-property keys are optional — a property may
# carry `type` OR `$ref`, `x-deterministic` may be absent on legacy
# fields, etc. The extractor uses `.get("x-deterministic")` so
# absence is safe; the typed shape exists so callers don't see
# `dict[str, Any]` bleed across the internal/boundary line.
# Self-recursive: a schema property's `items` is itself a property
# (the element schema for an array), and `properties` maps names to
# nested property definitions. Forward-referencing via the string
# `"SchemaProperty"` closes the loop. `additionalProperties` accepts
# either a bool (true/false) or another SchemaProperty per JSON
# Schema. The recursive shape eliminates the `dict[str, Any]` surface
# the prior version carried; only the JSON-loaded value type from
# `json.loads` (which is fundamentally `Any`) remains, and that's
# bounded to the `_schema_properties` cast.
# `Union[bool, "SchemaProperty"]` rather than `"bool | SchemaProperty"`:
# the project's minimum supported Python is 3.11 (where `bool | X`
# works at runtime), but the functional `TypedDict` form evaluates
# its second argument at module-load time. The string-form pipe
# union `"bool | SchemaProperty"` is a forward reference under
# `from __future__ import annotations`, but TypedDict's runtime
# resolution path historically had subtle interactions with pipe
# unions in string form. `Union[bool, "SchemaProperty"]` works
# uniformly across mypy and runtime introspection on every
# supported Python.
SchemaProperty = TypedDict(
    "SchemaProperty",
    {
        "type": str,
        "$ref": str,
        "items": "SchemaProperty",
        "properties": dict[str, "SchemaProperty"],
        "additionalProperties": Union[bool, "SchemaProperty"],
        "description": str,
        "x-deterministic": bool,
    },
    total=False,
)


class DeterministicSubsetWarning(UserWarning):
    """Emitted by `extract_deterministic_subset` when the input dict
    carries keys not present in the schema's top-level properties.

    Triggered by schema drift — typically a producer running a newer
    `ReportV01` shape than the consumer's bundled schema. Warning
    (not raise) because dropping the extra key is the safer
    extraction default; raising would block byte-equality comparisons
    on otherwise-compatible reports. Future schema-bump migrations
    can promote this to an error if drift becomes a load-bearing
    failure mode.
    """


@lru_cache(maxsize=1)
def _schema_document() -> dict[str, Any]:
    """Load the bundled report schema once. Sole entry point for
    schema reads in this module; downstream helpers project from
    this dict so the JSON file is never opened twice (eliminates
    the dual-load divergence risk that iter-1 carried).
    """
    from whatifd.report.models_v01 import REPORT_SCHEMA_VERSION

    schema_resource = files("whatifd.report.schema").joinpath(
        f"v{REPORT_SCHEMA_VERSION}.schema.json"
    )
    doc: dict[str, Any] = json.loads(schema_resource.read_text(encoding="utf-8"))
    return doc


def _schema_properties() -> dict[str, SchemaProperty]:
    """The schema's top-level `properties` map. Projects from the
    cached schema document — no separate file read."""
    properties: dict[str, SchemaProperty] = _schema_document().get("properties", {})
    return properties


@lru_cache(maxsize=1)
def _deterministic_field_names() -> frozenset[str]:
    """Cache the set of top-level field names tagged
    `x-deterministic: true`. Schema is committed; doesn't change at
    runtime."""
    return frozenset(
        name for name, prop in _schema_properties().items() if prop.get("x-deterministic") is True
    )


def deterministic_field_names() -> frozenset[str]:
    """Public accessor for the cached set. Tests use this to assert
    coverage against the schema (e.g., \"the determinism test must
    cover every field tagged True\")."""
    return _deterministic_field_names()


def _runtime_deterministic_subfields() -> frozenset[str]:
    """Phase J: read RunManifest's per-field `x-deterministic`
    annotations from the v0.2 schema's `$def` for `RunManifest`.
    The runtime top-level field has `x-deterministic: false` (the
    whole subtree is non-deterministic by default), but the schema
    annotates each sub-field individually. The extractor descends
    when a top-level field is excluded-by-blanket but its nested
    `$ref`'s `$def` carries per-field annotations — those tagged
    `true` are the deterministic projection.

    Returns an empty frozenset if the schema doesn't carry the
    annotations AND emits a `DeterministicSubsetWarning` so the
    silent-degrade path is observable. Cardinal #1: missing
    annotations are structured data, not silent fallback. The
    empty frozenset still degrades gracefully (extractor drops
    runtime entirely, matching pre-Phase-J behavior) but the
    warning makes the degradation visible.
    """
    properties = _schema_properties()
    runtime_prop = properties.get("runtime", {})
    ref = runtime_prop.get("$ref", "")
    if not ref.startswith("#/$defs/"):
        warnings.warn(
            "_runtime_deterministic_subfields: runtime property has no $ref to a "
            "$def — schema doesn't carry Phase J per-field annotations. "
            "Falling back to pre-Phase-J behavior (runtime excluded as a whole). "
            "Likely cause: bundled schema pre-dates Phase J or has been "
            "regenerated against an older RunManifest dataclass.",
            DeterministicSubsetWarning,
            stacklevel=2,
        )
        return frozenset()
    def_name = ref[len("#/$defs/") :]
    runtime_def = _schema_document().get("$defs", {}).get(def_name, {})
    annotated = frozenset(
        name
        for name, prop in runtime_def.get("properties", {}).items()
        if prop.get("x-deterministic") is True
    )
    if not annotated:
        warnings.warn(
            f"_runtime_deterministic_subfields: $def {def_name!r} has no properties "
            "tagged `x-deterministic: true`. Falling back to pre-Phase-J behavior "
            "(runtime excluded as a whole). Likely cause: dataclass's "
            "_DETERMINISTIC_FIELDS opt-in was emptied without intention.",
            DeterministicSubsetWarning,
            stacklevel=2,
        )
    return annotated


def extract_deterministic_subset(report_dict: Mapping[str, Any]) -> dict[str, Any]:
    """Project `report_dict` down to the deterministic subset.

    `report_dict` is the JSON-serialized form (a `dict[str, Any]`) of
    a `ReportV01`. Returns a new dict containing only the keys whose
    schema property carries `x-deterministic: true`.

    Phase J (v0.2 widening): nested dataclasses that opt into per-
    field determinism (currently `RunManifest` via its
    `_DETERMINISTIC_FIELDS` class attribute) get partial-subtree
    projection. Top-level `runtime` is excluded by blanket, but the
    extractor descends into it and includes only the sub-fields the
    schema's `$def` for `RunManifest` tags `x-deterministic: true`.

    Other top-level fields (cohort_results, failures, decision_findings,
    cache_summary, etc.) keep their pre-Phase-J behavior: included as
    a whole subtree because their top-level annotation is `true`.

    Emits `DeterministicSubsetWarning` for any input key not present
    in the schema's top-level properties — typically schema drift
    (producer is newer than the consumer's bundled schema).

    This function does NOT serialize — pass it the result of
    `json.loads(WhatifJSONEncoder.encode(report))` (or equivalent).
    """
    schema_properties = _schema_properties()
    unknown = sorted(k for k in report_dict if k not in schema_properties)
    if unknown:
        warnings.warn(
            f"extract_deterministic_subset: keys not in schema (dropped): {unknown!r}. "
            "Likely schema drift — producer ahead of consumer's bundled v0.1 schema.",
            DeterministicSubsetWarning,
            stacklevel=2,
        )
    keep_top = _deterministic_field_names()
    result: dict[str, Any] = {k: v for k, v in report_dict.items() if k in keep_top}

    # Phase J: descend into `runtime` for per-field determinism.
    runtime_value = report_dict.get("runtime")
    if isinstance(runtime_value, Mapping):
        keep_runtime = _runtime_deterministic_subfields()
        if keep_runtime:
            result["runtime"] = {k: v for k, v in runtime_value.items() if k in keep_runtime}
    return result


def extract_deterministic_subset_from_report(
    report: ReportV01,
) -> dict[str, Any]:
    """Project a `ReportV01` directly into the deterministic subset.

    Convenience wrapper for non-test callers (CI diff gates,
    determinism property tests, future cross-run comparison
    tooling) that have a domain object in hand and don't want to
    round-trip through `encode_report_v01` → `json.loads` →
    `extract_deterministic_subset`. The round-trip path stays
    available for tests that explicitly want to exercise the
    serialization seam (`test_determinism.py` does this on
    purpose).

    **Equivalence contract:** the result is exactly equal (`==`) to
    `extract_deterministic_subset(json.loads(encode_report_v01(report)))`.
    Tests that don't want to exercise the serialization seam can
    swap freely between the two surfaces. The equivalence is pinned
    by `test_extract_from_report_matches_round_trip` in
    `tests/integration/test_determinism.py`; that test is the
    canonical contract surface. A future divergence (e.g., the
    typed helper short-circuits the encoder) MUST update both this
    docstring and that test in the same PR.

    Imports `encode_report_v01` lazily to avoid pulling the
    encoder module into `whatifd.serialization.determinism`'s import
    graph at package load — keeps the determinism module light for
    consumers that only call `deterministic_field_names()`.

    **Performance note:** this helper internally round-trips through
    `encode_report_v01` → `json.loads` → `extract_deterministic_subset`.
    For one-shot extraction (CI diff gate, single comparison) the
    cost is invisible. For performance-sensitive **repeated**
    extraction over many reports (a hypothetical batch determinism
    audit) — call `encode_report_v01(report)` once, parse to a dict
    once, then call `extract_deterministic_subset(dict_)` many
    times against in-memory variants. The round-trip dominates the
    extraction cost; doing it once amortizes over the batch.
    """
    from whatifd.serialization.encoder import encode_report_v01

    serialized = json.loads(encode_report_v01(report))
    return extract_deterministic_subset(serialized)


__all__ = [
    "DeterministicSubsetWarning",
    "deterministic_field_names",
    "extract_deterministic_subset",
    "extract_deterministic_subset_from_report",
]
