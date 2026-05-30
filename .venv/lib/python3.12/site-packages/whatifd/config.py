"""`whatifd.config` — typed configuration with hint generation.

Phase 8.1 of the v0.1 implementation plan. Top-level `WhatifConfig`
plus per-section sub-models. Pydantic v2 strict (`extra="forbid"`)
so unknown fields fail validation rather than silently absorb. A
hint generator translates Pydantic's stack-trace-flavored errors
into a multi-line message named-fields-and-suggestions.

## Sections

  - `source` — adapter ref (e.g., `"langfuse"`).
  - `target` — runner ref (e.g., `"python:my_agent.replay:run"`).
  - `selection` — per-cohort selection limits.
  - `change` — the proposed change (`system_prompt`, `model`, etc.
    — same shape as `whatifd.contract.ReplayConfig`).
  - `scorer` — scorer adapter + cache settings.
  - `decision` — verdict policy thresholds (mirrors v0.1's
    internal `DecisionPolicy` fields).
  - `reporting` — output profile + forensic acknowledgment block.
  - `timeouts` — replay / score wall-clock budgets.

## Two-affirmation rule (cardinal #7)

The `reporting.profile = "forensic"` capability is structurally
dangerous (full unredacted user content in the artifact). Cardinal
#7 requires affirmation across TWO independent surfaces before the
forensic capability activates:

  1. The config carries `reporting.profile: forensic` AND
     `reporting.forensic_acknowledgment` block populated.
  2. The CLI invocation includes `--profile forensic`.

`assert_two_affirmation(cfg, *, cli_profile)` enforces this. Single-
affirmation attempts (config-only, CLI-only, or
`forensic_acknowledgment` block missing) raise
`ForensicAffirmationError` with a message naming exactly which
surface is missing.

## Hint generation

`format_validation_errors(ValidationError)` produces a friendly
multi-line summary: per-error path + Pydantic message + a
suggestion drawn from the `_HINTS` table for the most common
misconfigurations. Falls back to "no hint registered" for codes
not in the table — operators see the raw Pydantic message either
way.

## Cardinal alignment

- **#7 two-affirmation:** load-bearing for forensic-profile
  enablement. Tests pin that single-surface attempts fail.
- **#5 sensitive data wrapped:** the forensic profile's structural
  danger is exactly that it bypasses redaction. The two-affirmation
  rule is the structural defense; without both surfaces, `redact()`
  applies the default profile.
- **#1 failures-as-data:** validation errors are typed
  (`ValidationError`); hint generation is presentational. The
  config-load function returns `WhatifConfig` or raises
  `ValidationError` — no silent defaults for missing required
  fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from whatifd.types.manifest import ExperimentShape
from whatifd.types.primitives import JsonPrimitive

# ---------------------------------------------------------------------------
# Forensic-profile enforcement (cardinal #7)
# ---------------------------------------------------------------------------


class ForensicAffirmationError(ValueError):
    """Raised when forensic-profile enablement fails the two-
    affirmation rule. Cardinal #7.

    The error message names exactly which surface is missing
    (config block vs CLI flag) so the operator can fix the right
    one. Both surfaces are required because either alone is
    insufficient — accidental enablement on either side would
    surface unredacted content otherwise.
    """


class TwoAffirmationProof:
    """Witness token: structurally proves the cardinal-#7 two-
    affirmation check ran AND passed.

    Mirrors the `FloorPassedProof` pattern from cardinal #2. The
    only function that produces a `TwoAffirmationProof` is
    `assert_two_affirmation`; any code path that consumes
    forensic-profile capability MUST accept a proof, forcing
    callers through the check.

    Constructing a `TwoAffirmationProof` outside this module is
    blocked by a closure-captured token (same pattern as
    `_FLOOR_INTERNAL_TOKEN` in `whatifd/decision/floor.py`):
    `__init__` requires a sentinel that only this module holds.
    A fabricated proof raises at construction.

    `forensic_active` records the verdict the check delivered:
    True iff both surfaces agreed on `forensic`; False otherwise.
    Forensic-path code branches on this — not on the raw
    config/CLI values — so the witness is the single source of
    truth for "are we writing unredacted artifacts".
    """

    __slots__ = ("forensic_active",)

    def __init__(self, *, forensic_active: bool, _token: object) -> None:
        if _token is not _PROOF_TOKEN:
            raise RuntimeError(
                "TwoAffirmationProof can only be constructed by "
                "`whatifd.config.assert_two_affirmation`. The witness-"
                "token closure-capture pattern (cardinal #7 mirror of "
                "cardinal #2's FloorPassedProof) prevents fabrication."
            )
        self.forensic_active = forensic_active


# Module-private sentinel used to authenticate `TwoAffirmationProof`
# construction. Captured by `assert_two_affirmation` via lexical
# scope; not exported. A future restructure that exports this token
# OR moves `assert_two_affirmation` out of this module breaks the
# witness guarantee — banned-import lint and the cascade entry
# track this constraint.
_PROOF_TOKEN = object()


# ---------------------------------------------------------------------------
# Per-section sub-models
# ---------------------------------------------------------------------------

# Pydantic v2 config: forbid unknown fields so a typo surfaces as
# a validation error, NOT as a silently-ignored value.
#
# Note on `strict` vs `extra`: we use `extra="forbid"` (rejects
# unknown fields) but NOT `strict=True` (which would reject lax
# type coercion). YAML parses `60` as int; the `replay_seconds:
# float` field needs the int→float coercion to accept it. The
# typo-protection that matters is `extra="forbid"`; strict mode
# was overzealous.
_STRICT = ConfigDict(extra="forbid")


class SourceConfig(BaseModel):
    """Trace source adapter reference + per-source options."""

    model_config = _STRICT

    adapter: str = Field(
        ...,
        description="Adapter name; e.g., 'langfuse'. Resolved at load time.",
    )
    # Adapter-specific options omitted from v0.1 per YAGNI:
    # `langfuse` (the only v0.1 adapter) takes no config-level
    # options. Phase 4 adapter integration will revisit if a future
    # adapter needs them; the typed shape (dedicated `<Adapter>Options`
    # model, NOT `dict[str, Any]`) lands then per cardinal #6.


class TargetConfig(BaseModel):
    """Runner target reference. v0.1 supports `python:` runners."""

    model_config = _STRICT

    runner: str = Field(
        ...,
        description=(
            "Runner reference; v0.1 supports `python:module.path:attr`. "
            "Async runners are detected at import time."
        ),
    )


class CohortSelectionConfig(BaseModel):
    model_config = _STRICT

    limit: int = Field(..., ge=1)
    filter: str | None = None


class SelectionConfig(BaseModel):
    """Per-cohort selection limits."""

    model_config = _STRICT

    failure_cohort: CohortSelectionConfig
    baseline_cohort: CohortSelectionConfig


class ChangeConfig(BaseModel):
    """The proposed change. Mirrors `whatifd.contract.ReplayConfig`
    keys; v0.1 supports `system_prompt` only."""

    model_config = _STRICT

    system_prompt: str | None = None
    model: str | None = None


class ScorerConfig(BaseModel):
    """Scorer configuration. v0.2 introduces config-loaded `score_fn`
    so the `inspect_ai` adapter is reachable from YAML; v0.1 was
    programmatic-only.

    For `adapter="inspect_ai"`: `score_fn`, `judge_provider`,
    `judge_model_id`, `rubric_id`, `rubric_text` are all required.
    The validator enforces this so misconfigured runs fail at
    startup with a named field, not at scorer-invocation time.

    For `adapter="stub"`: only `cache_mode` matters; the other fields
    are silently ignored if set (the validator does not reject them
    so that the same config block can be retargeted from stub→inspect_ai
    with one keystroke during development).
    """

    model_config = _STRICT

    adapter: Literal["stub", "inspect_ai"]
    """Scorer adapter name. Pinned to the v0.2 supported set as a
    Literal so unknown values fail at config-load with a named-field
    error rather than at factory dispatch time. New adapters land
    here + a corresponding factory branch + a cascade-catalog entry."""

    cache_mode: Literal["auto", "on", "off", "read_only", "refresh"] = "auto"

    # v0.2 inspect_ai fields. All optional at the schema level so
    # `adapter: stub` configs don't require them; the inspect_ai-specific
    # cross-field validator enforces presence below.
    # NOTE(docs-followup #81): the v0.2 caveat admonitions in
    # whatifd-docs/ are obsolete now that this field exists. Tracked
    # at https://github.com/victoralfred/whatifd/issues/81.
    score_fn: str | None = Field(
        default=None,
        description=(
            "`python:<module.path>:<attr>` reference to the Inspect AI score function. "
            "Required when adapter='inspect_ai'."
        ),
    )
    judge_provider: str | None = None
    judge_model_id: str | None = None
    judge_model_snapshot: str | None = None
    rubric_id: str | None = None
    rubric_text: str | None = None
    # `scoring_parameters` carries arbitrary JSON-primitive knobs
    # (temperature, max_tokens, ...) that pass through to the
    # InspectAIScorer. Bounded to `str | int | float | bool | None` so
    # no `dict[str, Any]` crosses the cardinal #6 boundary. Non-primitive
    # shapes (lists, tuples, nested dicts) are out of scope — operators
    # encode them as serialized strings (JSON or comma-separated) and the
    # score_fn deserializes. There is no other contract surface; this
    # comment is the documented convention.
    scoring_parameters: dict[str, JsonPrimitive] = Field(
        default_factory=dict,
        description="Arbitrary JSON-primitive knobs passed through to InspectAIScorer.",
    )

    @model_validator(mode="before")
    @classmethod
    def _validate_scoring_parameters_are_primitives(cls, data: object) -> object:
        # Runs BEFORE field-type validation so nested values surface
        # as a single named-field error rather than Pydantic's 4-arm
        # union rejection ("expected string OR int OR float OR bool
        # OR null"). Pins the serialized-string convention
        # structurally instead of relying on the doc comment.
        if not isinstance(data, dict):
            return data
        params = data.get("scoring_parameters")
        if not isinstance(params, dict):
            return data
        nested = [k for k, v in params.items() if isinstance(v, (list, dict, tuple, set))]
        if nested:
            raise ValueError(
                f"scorer.scoring_parameters: nested structures not allowed at keys "
                f"{sorted(nested)}. Values must be JSON primitives "
                "(str | int | float | bool | None). Encode complex shapes as "
                "serialized strings; the score_fn deserializes."
            )
        return data

    @model_validator(mode="after")
    def _validate_inspect_ai_required_fields(self) -> ScorerConfig:
        if self.adapter != "inspect_ai":
            return self
        missing = [
            name
            for name in ("score_fn", "judge_provider", "judge_model_id", "rubric_id", "rubric_text")
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(
                f"scorer.adapter='inspect_ai' requires: {', '.join(missing)}. v0.2 "
                "introduced config-loaded score_fn; populate the missing fields or "
                "fall back to scorer.adapter='stub' for offline/CLI smoke tests."
            )
        return self


class DecisionConfig(BaseModel):
    """Mirrors `whatifd.types.policy.DecisionPolicy` fields. The
    runtime constructs a `DecisionPolicy` from these values.
    """

    model_config = _STRICT

    require_baseline: bool = True
    max_baseline_regression_ratio: float = Field(0.10, ge=0.0, le=1.0)
    min_failure_improvement_ratio: float = Field(0.50, ge=0.0, le=1.0)
    max_ci_width: float | None = Field(None, ge=0.0)
    practical_delta_epsilon: float = Field(0.05, ge=0.0)


class ForensicAcknowledgment(BaseModel):
    """The config-side affirmation for forensic profile enablement.

    Populating this block is one of the two surfaces required by
    cardinal #7. The CLI flag is the other. The fields exist to
    make accidental enablement IMPOSSIBLE — a deployment that
    typos `forensic_ackn0wledgment` (zero) hits Pydantic's
    `extra="forbid"` and fails immediately.
    """

    model_config = _STRICT

    accepted_by: str = Field(..., min_length=1)
    # `accepted_at` accepts ISO 8601 dates / datetimes (subset
    # the regex below covers): `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SS`,
    # optional fractional seconds, optional timezone (`Z` or
    # `+HH:MM` / `-HH:MM`). The regex is intentionally permissive
    # rather than parsing — Pydantic's `datetime`/`date` types
    # would reject string-only YAML, and we want operators to
    # write dates as strings in YAML for forensic-audit clarity.
    # The regex catches obviously wrong values (e.g., free-text
    # like "yesterday") while accepting the canonical formats
    # operators will write.
    accepted_at: str = Field(
        ...,
        min_length=1,
        pattern=r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$",
    )
    reason: str = Field(..., min_length=1)


class ReportingConfig(BaseModel):
    model_config = _STRICT

    profile: Literal["default", "review", "minimal", "forensic"] = "default"
    forensic_acknowledgment: ForensicAcknowledgment | None = None

    @model_validator(mode="after")
    def _forensic_requires_acknowledgment_block(self) -> ReportingConfig:
        """Config-level half of the two-affirmation rule. The CLI
        half is enforced by `assert_two_affirmation` at startup —
        this validator only catches the case where `profile:
        forensic` is set without the acknowledgment block."""
        if self.profile == "forensic" and self.forensic_acknowledgment is None:
            raise ValueError(
                "reporting.profile='forensic' requires a populated "
                "reporting.forensic_acknowledgment block (cardinal #7 "
                "two-affirmation rule). Add accepted_by, accepted_at, "
                "and reason; then re-run with `--profile forensic`."
            )
        return self


class TimeoutsConfig(BaseModel):
    model_config = _STRICT

    replay_seconds: float = Field(60.0, gt=0.0)
    score_seconds: float = Field(30.0, gt=0.0)


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class WhatifConfig(BaseModel):
    """Top-level whatifd configuration.

    Loaded from `whatifd.config.yaml` (or alternative path) at CLI
    startup. Pydantic v2 strict mode rejects unknown fields at any
    nesting level — typos fail loud rather than silently absorb.
    """

    model_config = _STRICT

    source: SourceConfig
    target: TargetConfig
    selection: SelectionConfig
    change: ChangeConfig
    scorer: ScorerConfig
    # decision / reporting / timeouts sections are REQUIRED at the
    # top-level config. Each sub-model's fields carry their own
    # defaults, so an empty `decision: {}` block in YAML is enough
    # to opt in to the v0.1 defaults — but the section must be
    # present, not silently inferred. This avoids the
    # `model_construct` / `model_validate({})` factory complexity
    # while keeping mypy strict happy without the Pydantic plugin.
    decision: DecisionConfig
    reporting: ReportingConfig
    timeouts: TimeoutsConfig

    # Phase C wired the verdict-policy branch on experiment_shape;
    # this field closes the CLI loop. Defaults to "failure_rescue"
    # (v0.1 behavior) so existing configs don't require any change.
    # Set to "regression_check" for the regression-check shape;
    # see whatifd.types.manifest.ExperimentShape for the contract.
    experiment_shape: ExperimentShape = "failure_rescue"


# ---------------------------------------------------------------------------
# Two-affirmation enforcement
# ---------------------------------------------------------------------------


def assert_two_affirmation(cfg: WhatifConfig, *, cli_profile: str | None) -> TwoAffirmationProof:
    """Enforce the cardinal-#7 two-affirmation rule for forensic.

    Both surfaces must agree:
      - config: `reporting.profile == "forensic"` AND
        `reporting.forensic_acknowledgment` block populated
      - CLI: `--profile forensic` (passed as `cli_profile="forensic"`)

    The config-side post-init validator already catches "profile
    forensic but no acknowledgment block". This function's job is
    to enforce the cross-surface match: forensic on one side
    without forensic on the other is the dangerous-misconfiguration
    case.

    ## CLI startup ordering

    Call this at CLI startup IMMEDIATELY after `WhatifConfig` loads
    and BEFORE any forensic-path code runs. Config construction
    alone enforces only the config-side half of cardinal #7; the
    cross-surface check below is what catches `--profile forensic`
    without the matching config block (or vice versa). Phase 8.2
    CLI must call this before resolving the redaction profile.

    ## Witness token

    Returns a `TwoAffirmationProof`. Forensic-path code (Phase
    8.2 + downstream renderer / artifact-bundle code) accepts the
    proof as a parameter — callers that skip the affirmation check
    cannot type-check against the forensic-path API. Mirrors
    cardinal #2's `FloorPassedProof` witness pattern.

    `proof.forensic_active` records the verdict: True iff both
    surfaces agreed on `forensic`. Forensic-path code branches on
    this — not on raw config/CLI values — so the witness is the
    single source of truth for "are we writing unredacted
    artifacts".
    """
    config_says_forensic = cfg.reporting.profile == "forensic"
    cli_says_forensic = cli_profile == "forensic"

    if config_says_forensic and not cli_says_forensic:
        raise ForensicAffirmationError(
            "Forensic profile enabled in config (reporting.profile='forensic') "
            "but the CLI invocation did not include `--profile forensic`. "
            "Cardinal #7 requires affirmation across BOTH surfaces. "
            "Either add `--profile forensic` to the command line or "
            "remove the forensic profile from config."
        )
    if cli_says_forensic and not config_says_forensic:
        raise ForensicAffirmationError(
            "CLI invoked with `--profile forensic` but config does not "
            "set reporting.profile='forensic' with a populated "
            "forensic_acknowledgment block. Cardinal #7 requires "
            "affirmation across BOTH surfaces. The CLI flag alone is "
            "insufficient."
        )

    return TwoAffirmationProof(
        forensic_active=config_says_forensic and cli_says_forensic,
        _token=_PROOF_TOKEN,
    )


# ---------------------------------------------------------------------------
# Hint generation
# ---------------------------------------------------------------------------

# Map (full-loc-path, error-type) -> human-readable hint. The path
# is the dotted location string (Pydantic's `loc` tuple joined
# with `.`). Full-path keys (rather than just the leaf field name)
# disambiguate identically-named fields in different sections —
# e.g., a future `source.adapter` and `scorer.adapter` get
# distinct hints.
#
# Caveat for v0.2+ array-field schemas: list indices in `loc`
# (e.g., `('decision', 'thresholds', 0)`) join as
# integers-as-strings (`'decision.thresholds.0'`). A registered
# hint keyed on that exact path would only fire for index 0;
# index 1, 2, ... would miss the table. v0.1 has no array
# fields with constraints, so this isn't load-bearing yet. When
# array constraints land, the hint table will need a regex/glob
# matcher to cover all indices for a given field.
#
# `model_validator` errors arrive with `loc=()` and `type=
# 'value_error'` — `format_validation_errors` joins empty `loc`
# to the literal `'(root)'` string and looks up
# `('(root)', 'value_error')`. The forensic-acknowledgment
# validator hits this path; its registered hint below mirrors
# the validator's raised message for operator-facing
# consistency.
_HINTS: dict[tuple[str, str], str] = {
    ("source.adapter", "missing"): ("set `source.adapter` to your tracer name (e.g., 'langfuse')."),
    ("target.runner", "missing"): ("set `target.runner` to a `python:module.path:attr` string."),
    ("selection.failure_cohort.limit", "greater_than_equal"): (
        "selection.failure_cohort.limit must be >= 1; use a positive integer."
    ),
    ("selection.baseline_cohort.limit", "greater_than_equal"): (
        "selection.baseline_cohort.limit must be >= 1; use a positive integer."
    ),
    ("decision.max_baseline_regression_ratio", "less_than_equal"): (
        "decision.max_baseline_regression_ratio must be a fraction in [0, 1]."
    ),
    ("decision.max_baseline_regression_ratio", "greater_than_equal"): (
        "decision.max_baseline_regression_ratio must be a fraction in [0, 1]."
    ),
    ("decision.min_failure_improvement_ratio", "less_than_equal"): (
        "decision.min_failure_improvement_ratio must be a fraction in [0, 1]."
    ),
    ("decision.min_failure_improvement_ratio", "greater_than_equal"): (
        "decision.min_failure_improvement_ratio must be a fraction in [0, 1]."
    ),
    ("decision.max_ci_width", "greater_than_equal"): (
        "decision.max_ci_width must be >= 0 (or omit to disable the check)."
    ),
    ("decision.practical_delta_epsilon", "greater_than_equal"): (
        "decision.practical_delta_epsilon must be >= 0."
    ),
    ("timeouts.replay_seconds", "greater_than"): ("timeouts.replay_seconds must be > 0."),
    ("timeouts.score_seconds", "greater_than"): ("timeouts.score_seconds must be > 0."),
    # Pydantic emits `model_validator` errors with `loc=
    # ('reporting',)` because the validator is attached to the
    # `ReportingConfig` field on `WhatifConfig`. The `loc` tuple
    # joins via `.` (one element → bare `'reporting'`, no leading
    # dot). The trailing colon in the rendered output (`reporting:
    # Value error...`) comes from the f-string in
    # `format_validation_errors`; the hint key matches the joined
    # path without the colon.
    ("reporting", "value_error"): (
        "model-level validation on `reporting` failed; see the "
        "message above. The most common cause is "
        "`reporting.profile='forensic'` without a populated "
        "`reporting.forensic_acknowledgment` block."
    ),
}


def format_validation_errors(exc: ValidationError) -> str:
    """Format a Pydantic `ValidationError` as a multi-line operator-
    facing message with hints where available.

    Each error in `exc.errors()` becomes a block:

      <dotted.field.path>: <pydantic-message>
        Hint: <suggestion> (when registered in _HINTS)

    Errors are emitted in `exc.errors()` order — Pydantic surfaces
    them depth-first by location, which produces a readable
    walkthrough of the misconfiguration without sorting.
    """
    lines: list[str] = ["whatifd config validation failed:", ""]
    for err in exc.errors():
        path = ".".join(str(part) for part in err["loc"]) or "(root)"
        msg = err["msg"]
        lines.append(f"  {path}: {msg}")
        hint = _HINTS.get((path, err["type"]))
        if hint is not None:
            lines.append(f"    Hint: {hint}")
        lines.append("")
    # Trim the trailing blank line so the output ends cleanly.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


class ConfigFileError(Exception):
    """Raised when the config file cannot be read or parsed.

    Distinct from Pydantic's `ValidationError`: file-system errors
    (path missing, permission denied) and YAML parse errors are
    structurally different from schema-validation errors — they
    arise BEFORE the schema sees the data. Cardinal #1: every
    expected failure at the load boundary is a typed exception,
    not a propagated OSError or yaml.YAMLError.
    """


def load_config(path: Path) -> WhatifConfig:
    """Load and validate a `whatifd` config file.

    Returns the validated `WhatifConfig`. Failure modes:

    - `path` does not exist → `ConfigFileError`
    - `path` is unreadable (permission, I/O) → `ConfigFileError`
    - YAML parse error → `ConfigFileError`
    - Schema-validation error → `pydantic.ValidationError` (pass to
      `format_validation_errors` for an operator-facing message)

    The two distinct exception types reflect distinct operator
    actions: `ConfigFileError` means "fix the file"; `ValidationError`
    means "fix the field". Cardinal #1: failure-as-data at the load
    boundary; the CLI catches both and renders the appropriate
    message before exiting non-zero.

    `path` accepts `.yaml` / `.yml` / `.json` extensions; the parser
    is selected by extension. Other extensions raise
    `ConfigFileError` rather than guessing.

    TOML support (e.g., `pyproject.toml`-style configs) is
    deferred from v0.1: it would require either tomllib (Python
    3.11+ stdlib, available) plus a separate writer for tooling
    that emits TOML, OR a third-party library that adds a dep.
    Operators who want TOML can convert to YAML at the build
    stage. Phase 8.2's CLI will accept `--config <path>` so
    extension-driven dispatch is the right place to add TOML when
    motivated by a real user need.
    """
    if not path.exists():
        raise ConfigFileError(f"config file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigFileError(f"cannot read config file {path}: {exc}") from exc

    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # already a project dep via pyyaml
        except ImportError as exc:  # pragma: no cover (pyyaml is required)
            raise ConfigFileError("PyYAML is required to load .yaml/.yml configs") from exc
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigFileError(f"YAML parse error in {path}: {exc}") from exc
    elif suffix == ".json":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigFileError(f"JSON parse error in {path}: {exc}") from exc
    else:
        raise ConfigFileError(
            f"unsupported config extension {suffix!r} on {path}; use .yaml, .yml, or .json"
        )

    if not isinstance(data, dict):
        raise ConfigFileError(
            f"config file {path} must parse to a mapping; got {type(data).__name__}"
        )

    # `model_validate(data)` rather than `WhatifConfig(**data)` —
    # the kwargs-unpack form would shadow Pydantic internals if
    # the YAML included a top-level key like `model_config` or
    # `model_fields`. `model_validate` accepts the dict directly,
    # eliminating the keyword-collision class entirely.
    return WhatifConfig.model_validate(data)


__all__ = [
    "ChangeConfig",
    "CohortSelectionConfig",
    "ConfigFileError",
    "DecisionConfig",
    "ForensicAcknowledgment",
    "ForensicAffirmationError",
    "ReportingConfig",
    "ScorerConfig",
    "SelectionConfig",
    "SourceConfig",
    "TargetConfig",
    "TimeoutsConfig",
    "TwoAffirmationProof",
    "WhatifConfig",
    "assert_two_affirmation",
    "format_validation_errors",
    "load_config",
]
