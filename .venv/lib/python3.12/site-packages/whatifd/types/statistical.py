"""Statistical types — Phase 1.7, cardinal rule #10.

The statistical-claims layer of the type model. Cardinal #10 doctrine:
"whatifd uses paired trace deltas as the unit of analysis, predeclared
cohort-level endpoints as the basis for verdict, and descriptive (not
inferential) framing for per-trace evidence. Methodology is disclosed
in every report. Scorer caching addresses reproducibility — NOT
reliability, validity, calibration, or absence of bias."

Types in this module fall into three groups:

1. **Trace-delta types** — `TraceDelta` (internal, float arithmetic;
   pairing is structural per CASCADE "Paired-delta as atomic unit") and
   `TraceDeltaReportV01` (public, DecimalString-serialized).

2. **Methodology disclosure types** — five disclosure dataclasses that
   compose into `MethodologyDisclosure`. The composite is a required
   field on `ReportV01` per cardinal #10; schema validation enforces
   presence + content.

3. **Clustering types** — `ClusterKeySupport`, `ClusterSelection`,
   `ClusteringPolicy`. v0.1 declares the cluster-bootstrap structural
   commitment (adapter declares; policy resolves; methodology block
   discloses); the cluster-resampling math is deferred to v0.2 (i.i.d.
   bootstrap with explicit disclosure when no cluster key is available).

Reliability/validity/calibration/bias are EXPLICITLY DISCLOSED-AS-
UNMEASURED via boolean fields on `JudgeMethodDisclosure`. The renderer
shows these in the methodology block — does NOT silently omit them.
v0.1 defaults to `reproducibility_addressed=True` and the other four
False; v0.2/v0.3 add real measurement with opt-in subsets and human-
labeled calibration sets.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from whatifd.types.primitives import DecimalString

# --- Trace delta types --------------------------------------------------


@dataclass(frozen=True, slots=True)
class TraceDelta:
    """Internal type: paired delta with float arithmetic.

    The atomic unit of statistical analysis. `delta` is computed at
    construction from `replayed_score - original_score` so callers can
    pass any two of the three fields and the third is derived.

    Pairing is structural — `original_score` and `replayed_score` are
    stored together, never as separate Sequence[float] arrays. Analysis
    functions in `whatifd/internal/stats.py` (Phase 6) accept
    `Sequence[TraceDelta]` only; mypy strict catches signature misuse.

    `cluster_id` is the resolved cluster key (e.g., conversation_id) for
    cluster-bootstrap. None when no cluster key is available; the
    methodology block discloses the i.i.d. assumption.

    `strata` is for v0.2 stratified sampling; v0.1 leaves it empty.
    """

    trace_id: str
    cohort: str
    metric: str
    original_score: float
    replayed_score: float
    delta: float = field(init=False)
    cluster_id: str | None = None
    strata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "delta", self.replayed_score - self.original_score)


@dataclass(frozen=True, slots=True)
class TraceDeltaReportV01:
    """Public type: DecimalString-serialized form for ReportV01.

    Float arithmetic happens internally on `TraceDelta`; emission via
    `format(value, '.3f')` produces platform-stable `DecimalString`
    values for cross-platform determinism per cardinal rule #4.

    The projection layer in `whatifd/report/projection.py` (Phase 5)
    converts internal `TraceDelta` to public `TraceDeltaReportV01`.
    """

    trace_id: str
    cohort: str
    metric: str
    original_score: DecimalString
    replayed_score: DecimalString
    delta: DecimalString
    cluster_id: str | None = None
    strata: Mapping[str, str] = field(default_factory=dict)


# --- Methodology disclosure types ---------------------------------------


@dataclass(frozen=True, slots=True)
class BootstrapMethodDisclosure:
    """How uncertainty was estimated.

    `method == "unavailable"` carries `unavailable_reason` (e.g., "sample
    too small", "cache locked, scoring stage did not run"). The renderer
    surfaces this directly so reviewers can see why CI is missing
    rather than seeing silence.
    """

    method: Literal[
        "paired_percentile_bootstrap",
        "cluster_paired_percentile_bootstrap",
        "unavailable",
    ]
    resamples: int | None
    seed: int | None
    sample_unit: Literal["paired_trace_delta"]
    ci_level: DecimalString  # e.g., "0.95"
    cluster_key: str | None
    assumptions: tuple[str, ...]  # e.g., ("trace_independence",) when no cluster
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MultiplicityDisclosure:
    """Multiple-comparison correction stance.

    v0.1 default: `correction="none"` with reason "single primary metric
    per cohort; no correction applied". Multiple primary metrics with
    Holm correction is deferred to v0.2.
    """

    primary_endpoint_count: int
    correction: Literal["none", "holm", "bonferroni", "bh_fdr"]
    reason: str


@dataclass(frozen=True, slots=True)
class JudgeMethodDisclosure:
    """Judge configuration and reliability state.

    The five-concept reliability discipline (cardinal #10): scorer
    caching addresses ONE concept (reproducibility); the other four
    (reliability, validity, calibration, bias) are explicitly disclosed
    as unmeasured by default. The renderer shows all five — never
    silently omits.
    """

    scorer: str
    scorer_version: str | None
    judge_provider: str | None
    judge_model: str
    judge_model_version: str | None
    rendered_prompt_hash: str
    rubric_hash: str | None

    scorer_cache_enabled: bool
    scorer_cache_mode: Literal["off", "on", "read_only", "refresh", "auto"]
    scorer_cache_hits: int
    scorer_cache_misses: int

    # The five reliability concepts. v0.1 default: only reproducibility
    # addressed; the other four marked False (unmeasured).
    reproducibility_addressed: bool
    reliability_measured: bool
    validity_measured: bool
    calibration_measured: bool
    bias_audit_measured: bool


@dataclass(frozen=True, slots=True)
class EffectSizeDisclosure:
    """Practical-delta threshold + calibration source.

    `practical_delta_source="policy"` is the v0.1 default — the
    epsilon=0.05 default is policy, not empirically calibrated. v0.3
    supports calibration sets and `practical_delta_source` becomes
    `"calibrated_from_judge_noise_floor"`.

    `warning` populated when epsilon < judge_noise_floor — i.e., the
    policy threshold is below the noise floor of the judge, so observed
    "improvements" within that range are likely noise.
    """

    practical_delta: DecimalString
    practical_delta_source: Literal[
        "policy",
        "calibrated_from_judge_noise_floor",
        "unknown",
    ]
    judge_noise_floor: DecimalString | None
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class MethodologyDisclosure:
    """Required field on every ReportV01 — the cardinal #10 surface.

    A reviewer who reads only this block can answer:
    - What was the unit of analysis? (`unit_of_analysis: paired_trace_delta`)
    - What primary endpoints drove the verdict? (`primary_endpoints`)
    - How was uncertainty estimated? (`bootstrap.method, resamples, seed`)
    - Were correlations between traces accounted for? (`bootstrap.cluster_key`)
    - Was the judge consistent across runs? (`judge.scorer_cache_enabled`)
    - Has the judge been validated? (`judge.validity_measured` — v0.1: False)
    - What magnitude threshold was used? (`effect_size.practical_delta`)
    - Has that threshold been calibrated? (`effect_size.practical_delta_source`)
    - What multiplicity correction was applied? (`multiplicity.correction`)
    - What's the maximum claim allowed? (`causal_claim_scope`)

    `per_trace_inference` and `causal_claim_scope` are sealed Literals
    because v0.1 has only one allowed value for each. v0.2+ may extend
    these (a v0.1.x patch can add Literal values; type-changing is v1.0).
    """

    unit_of_analysis: Literal["paired_trace_delta"]
    primary_metric: str
    primary_endpoints: tuple[str, ...]
    cohorts: tuple[str, ...]

    bootstrap: BootstrapMethodDisclosure
    multiplicity: MultiplicityDisclosure
    judge: JudgeMethodDisclosure
    effect_size: EffectSizeDisclosure

    per_trace_inference: Literal["descriptive_only"]
    causal_claim_scope: Literal["associated_under_cached_tool_replay"]

    limitations: tuple[str, ...] = ()


# --- Clustering types ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClusterKeySupport:
    """Declared by `TraceSource.cluster_key_support()` (Phase 4).

    Lists the real cluster keys the tracer adapter can supply (stable
    identifiers from the production system: user_id, session_id,
    conversation_id, account_id, etc.). Empty tuple means "no clusters
    available; assume i.i.d." — the report discloses this assumption.

    Cardinal #10 forbids fabricating cluster keys (e.g., k-means on
    embeddings) for confirmatory verdicts in v0.1.
    """

    available_keys: tuple[str, ...]
    preferred_order: tuple[str, ...] = (
        "conversation_id",
        "session_id",
        "user_id",
    )


@dataclass(frozen=True, slots=True)
class ClusterSelection:
    """The resolved cluster choice for a run.

    Result of `resolve_cluster_key(source, policy)` (Phase 2). Recorded
    in `RunManifest` and `MethodologyDisclosure.bootstrap.cluster_key`.

    `mode == "selected"` carries a non-None `key`. `mode == "none"` and
    `mode == "unavailable"` carry `key=None`; they differ in why no
    clustering happened (none = explicit i.i.d.; unavailable = adapter
    couldn't supply any).
    """

    mode: Literal["none", "selected", "unavailable"]
    key: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class ClusteringPolicy:
    """User-configurable clustering policy.

    `fallback_behavior` controls what happens when no cluster key is
    available:
    - `assume_independent` — silent fallback (NOT recommended)
    - `warn` — default; report discloses i.i.d. assumption
    - `refuse` — block Ship if no cluster key available (CI-strict mode)
    """

    cluster_key: Literal[
        "none",
        "auto",
        "user_id",
        "session_id",
        "conversation_id",
    ] = "auto"

    fallback_behavior: Literal[
        "assume_independent",
        "warn",
        "refuse",
    ] = "warn"
