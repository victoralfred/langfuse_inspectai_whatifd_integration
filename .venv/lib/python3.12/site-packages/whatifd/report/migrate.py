"""Report migration — v0.1 → v0.2.

Phase A of the v0.2 roadmap. The v0.1 schema bump introduced a single
required top-level field on `ReportV01`: `experiment_shape`. v0.1
reports are upgraded by injecting `experiment_shape: "failure_rescue"`
(the only shape v0.1 supported) and bumping `schema_version` + `schema_uri`.

## Doctrine

- **Cardinal #1 (failure-as-data):** malformed input → typed
  `MigrationError`, never an unhandled exception. The CLI catches
  the typed error and emits a structured stderr message.
- **Cardinal #6 (public schema hand-written):** the migrator works on
  raw dict[str, Any] (the wire shape) — it does NOT instantiate
  `ReportV01` from the v0.1 input, because the v0.1 dict lacks the
  v0.2-required `experiment_shape` field. Construction of the typed
  `ReportV01` would fail before the migration could inject it. Working
  on the dict keeps the migration boundary clean.
- **Idempotence:** a v0.2 report passed through the migrator is
  returned unchanged with `changed=False`.

## Future migrations

When v0.3 schema lands, extend `_MIGRATIONS` with `"0.2" -> _migrate_v0_2_to_v0_3`.
The dispatcher walks the chain: v0.1 → v0.2 → v0.3 → ... → current.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeAlias

from whatifd.report.models_v01 import REPORT_SCHEMA_URI, REPORT_SCHEMA_VERSION

RawReport: TypeAlias = dict[str, Any]
"""Wire-shape report dict pre-typed-instantiation. Named alias (vs bare
`dict[str, Any]`) signals the cardinal #6 boundary: the migrator
operates here precisely because v0.X dicts may legitimately lack v0.Y-
required fields, and typed `ReportV01` instantiation would fail before
the migration could inject them."""


class MigrationError(Exception):
    """Structural error in the input report (cardinal #1).

    Raised on: missing schema_version, unknown source version, malformed
    top-level shape. Caller (CLI) translates to a stderr message + exit 2.
    """


def _migrate_v0_1_to_v0_2(report: RawReport) -> RawReport:
    """Inject `experiment_shape` (the only v0.2 schema addition) and
    bump schema_version / schema_uri. v0.1 was failure-rescue only,
    so the inject value is structurally determined.

    Reads `REPORT_SCHEMA_URI` rather than hardcoding the URI string —
    if the constant ever moves the migrator follows. Already covered
    by the post-chain integrity check, but reading the constant kills
    the drift risk at the source.
    """
    upgraded = dict(report)
    upgraded["experiment_shape"] = "failure_rescue"
    upgraded["schema_version"] = "0.2"
    upgraded["schema_uri"] = REPORT_SCHEMA_URI
    return upgraded


_MIGRATIONS: dict[str, Callable[[RawReport], RawReport]] = {
    "0.1": _migrate_v0_1_to_v0_2,
}
"""Mutable at the type level (not `Mapping`) because tests mutate it via
`monkeypatch.setitem` to inject buggy migrators for chain-integrity
coverage. Annotating as `dict` keeps the type-narrowing honest."""


def migrate_report(report: RawReport) -> tuple[RawReport, bool]:
    """Walk a report from its declared schema version to the current one.

    Returns `(migrated_report, changed)`. `changed=False` means the input
    was already at the current schema version (idempotent no-op).

    Raises `MigrationError` on structural problems.
    """
    if not isinstance(report, dict):
        raise MigrationError(f"report must be a JSON object, got {type(report).__name__}")

    version = report.get("schema_version")
    if version is None:
        raise MigrationError("report missing required `schema_version` field")
    if not isinstance(version, str):
        raise MigrationError(f"`schema_version` must be a string, got {type(version).__name__}")

    if version == REPORT_SCHEMA_VERSION:
        return report, False

    current = report
    walked = False
    while True:
        v = current.get("schema_version")
        if v == REPORT_SCHEMA_VERSION:
            break
        if not isinstance(v, str) or v not in _MIGRATIONS:
            raise MigrationError(
                f"no migration path from schema_version={v!r} to "
                f"v{REPORT_SCHEMA_VERSION}. Known sources: "
                f"{sorted(_MIGRATIONS.keys())}"
            )
        before = v
        current = _MIGRATIONS[v](current)
        after = current.get("schema_version")
        # Step-level integrity: a migration step MUST advance the
        # version. Same-version or non-string output indicates a buggy
        # migrator; fail with a chain-corruption message rather than
        # letting the loop terminate with a misleading "no migration
        # path" pointing at the corrupted output.
        if not isinstance(after, str) or after == before:
            raise MigrationError(
                f"migration chain corruption: step from {before!r} "
                f"produced schema_version={after!r}. The migration "
                f"function must advance the version."
            )
        walked = True

    if current.get("schema_uri") != REPORT_SCHEMA_URI:
        raise MigrationError(
            f"migration produced schema_uri={current.get('schema_uri')!r}, "
            f"expected {REPORT_SCHEMA_URI!r}"
        )

    return current, walked
