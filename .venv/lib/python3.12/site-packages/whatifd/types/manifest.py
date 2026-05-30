"""`EnvironmentFingerprint` and `RunManifest` — Phase 1.6 audit anchor.

The manifest is the audit anchor — it captures everything needed to
reproduce a run: floor version, policy, environment, redaction state,
sensitive-unwrap log. Every artifact bundle includes a manifest.

Most fields are non-deterministic (timestamps, host info, dependency
versions, sensitive-unwrap ordering). Per cardinal rule #4 (determinism
opt-in per field), the schema explicitly tags deterministic sub-fields
(`trust_floor`, `decision_policy`, `selection_seed`, `config_hash`,
`whatif_version`) with `x-deterministic: true`. Everything else defaults
to `x-deterministic: false`. The CI determinism test diffs only the
tagged subset.

`SensitiveUnwrap` lives in `whatifd/types/sensitive.py` (Phase 1.2)
because it's intimately tied to the `Sensitive[T]` wrapper. Manifest
imports it from there.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar, Literal

from whatifd.types.policy import DecisionPolicy, TrustFloor
from whatifd.types.sensitive import SensitiveUnwrap

ExperimentShape = Literal["failure_rescue", "regression_check"]
"""Which experiment shape this run executes (cardinal #10 — methodology
must match the design). v0.1 was failure-rescue only; v0.2 adds
regression-check. The verdict-policy branch on shape lands with Phase C;
Phase A introduces the field structurally so the schema is ready.
"""


@dataclass(frozen=True, slots=True)
class EnvironmentFingerprint:
    """Run-time environment description.

    All fields are non-deterministic (vary per host / Python install /
    pip resolution). Captured for audit, not for byte-equality testing.

    `dependencies` is a `Mapping[str, str]` of package name → installed
    version. Includes the project's own optional extras (e.g.,
    `whatifd-langfuse`, `whatifd-inspect-ai`) plus their transitive deps.
    The shape is a Mapping for read-only intent; the default is `dict`
    (the inner mapping is technically mutable in v0.1; Phase 5
    serialization may switch to MappingProxyType for stronger immutability
    if a real bug surfaces).
    """

    python: str
    platform: str
    whatif_version: str
    dependencies: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunManifest:
    """The audit anchor for a single whatifd run.

    The whole manifest is non-deterministic by default; the schema
    explicitly tags deterministic sub-fields (`trust_floor`,
    `decision_policy`, `selection_seed`, `config_hash`, `whatif_version`)
    with `x-deterministic: true`. Phase 5 serialization layer wires the
    tags; the determinism CI test diffs only the tagged subset.

    Fields:
    - `experiment_id` — caller-supplied identifier; used in artifact paths.
    - `started_at`, `finished_at` — ISO 8601 timestamps; NON-DETERMINISTIC.
    - `duration_ms` — derived; NON-DETERMINISTIC.
    - `whatif_version` — the version of the whatifd package that ran;
      DETERMINISTIC for byte-equality diffs across runs.
    - `config_hash` — sha256 of the resolved config; DETERMINISTIC.
    - `selection_seed` — seeded RNG for trace selection; DETERMINISTIC.
    - `source`, `target` — adapter identifiers (e.g., "langfuse",
      "python:my_agent.replay:run"); DETERMINISTIC.
    - `trust_floor`, `decision_policy` — the structural and policy
      configurations used; DETERMINISTIC.
    - `environment` — host + dependencies; NON-DETERMINISTIC.
    - `agent_identity` — opt-in attribution for the agent under test
      (vendor, model, prompt-template ID). v0.1 optional; v1.0 required
      for a Ship verdict.
    - `redaction` — disclosure of redaction state per cardinal rule #5.
      Schema includes `profile`, `enabled`, and adapter-specific rule-
      version markers.
    - `sensitive_unwraps` — list of `SensitiveUnwrap` audit records
      drained from `whatifd/types/sensitive.py:_audit_log` at end of run.
      NON-DETERMINISTIC ordering (depends on call order across threads).
    """

    # Phase J — Determinism widening (cardinal #4): per-field
    # determinism annotation, source-of-truth on the dataclass.
    # The schema generator reads this attribute when descending
    # into the manifest's `$def` and emits `x-deterministic:
    # true|false` on each property accordingly. Fields NOT in
    # this set get `x-deterministic: false` (the default).
    # Adding/removing a field here propagates structurally to
    # the schema annotation; the determinism CI test reads the
    # schema, so a future refactor that moves a field's
    # determinism status updates the test surface automatically.
    # `ClassVar` annotation prevents dataclass machinery (and any
    # downstream Pydantic boundary that introspects __annotations__)
    # from treating this as an instance field. Cardinal #6: typed
    # boundary discipline. Without ClassVar, a future PR adding
    # Pydantic validation around RunManifest could mistakenly try
    # to coerce/validate this as an init field.
    _DETERMINISTIC_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "experiment_id",
            "whatif_version",
            "config_hash",
            "selection_seed",
            "source",
            "target",
            "trust_floor",
            "decision_policy",
            "experiment_shape",
        }
    )

    experiment_id: str
    started_at: str
    finished_at: str
    duration_ms: int
    whatif_version: str
    config_hash: str
    selection_seed: int
    source: str
    target: str
    trust_floor: TrustFloor
    decision_policy: DecisionPolicy
    environment: EnvironmentFingerprint
    agent_identity: Mapping[str, str] | None = None
    redaction: Mapping[str, str | bool] = field(default_factory=dict)
    sensitive_unwraps: list[SensitiveUnwrap] = field(default_factory=list)
    experiment_shape: ExperimentShape = "failure_rescue"
    """v0.2 addition. Defaulted on the manifest for caller ergonomics
    (most existing constructors are failure-rescue). Projected to the
    required top-level `experiment_shape` field on `ReportV01`."""
