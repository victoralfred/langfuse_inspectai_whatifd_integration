"""Paired-percentile bootstrap — Phase E.1 of the v0.2 plan.

Doctrinally-correct replacement for the v0.1 empirical-percentile
shortcut in `whatifd.pipeline`. Resamples the per-trace delta
sequence with replacement N times, computes the bootstrap
distribution of the median, and returns the empirical 5th/95th
percentile of that distribution as the CI bounds.

## Why paired bootstrap, not stratified

Cardinal #10's unit of analysis is the **paired trace delta**: each
trace produces an `(original, replayed)` pair, and the per-trace
delta is the analytic atom. Bootstrap resampling at the
paired-trace level (rather than at the original-or-replayed-only
level) preserves that pairing — every resample draws whole pairs,
not orphaned originals or orphaned replays.

For v0.2 the resampling is i.i.d. across paired traces; cluster-
paired bootstrap (where resamples respect cluster boundaries like
session_id) is the v0.3 surface. The schema enum already
distinguishes `paired_percentile_bootstrap` from
`cluster_paired_percentile_bootstrap` so this module's output is
forward-compatible with the cluster variant.

## Determinism

`seed` is required (no default) so a future caller can't accidentally
ship a non-reproducible CI. The method seeds a fresh
`random.Random` instance — NOT the global `random` module — so
concurrent runs in the same process don't interleave.

## Why not numpy

Cardinal #9 (orchestration, not compute). The bootstrap is N
typically in [1000, 10000] and pure Python `random.choices` is
adequate for floor-passing verdicts. The v0.2 cascade catalog
notes a vectorized-numpy variant as a v0.3 optimization gated on
profile data showing this is a real bottleneck; the schema enum
stays unchanged either way.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Result of a paired-percentile bootstrap run.

    All numeric fields are floats; the caller is responsible for
    DecimalString formatting at the wire boundary (cardinal #4
    determinism — the wire shape is the deterministic surface, not
    the in-memory float).

    Fields:
    - `median`: the median of the original (unsampled) delta sequence.
    - `ci_lower`, `ci_upper`: lower/upper bound of the bootstrap CI
      at the configured `ci_level`.
    - `resamples`: the number of bootstrap iterations actually run.
      Echoed for `MethodologyDisclosure.bootstrap.resamples`.
    - `seed`: the random seed used. Echoed for the disclosure too.
    - `ci_level`: the confidence level as a fraction in (0, 1)
      (e.g., 0.95 for a 95% CI). Echoed for the disclosure.
    """

    median: float
    ci_lower: float
    ci_upper: float
    resamples: int
    seed: int
    ci_level: float


def paired_percentile_bootstrap(
    deltas: Sequence[float],
    *,
    resamples: int = 2000,
    ci_level: float = 0.95,
    seed: int,
) -> BootstrapResult:
    """Compute a paired-percentile bootstrap CI for the median of `deltas`.

    `deltas` is the sequence of per-trace deltas (one float per
    paired trace). `resamples` is the number of bootstrap iterations
    (default 2000 — adequate for floor-passing 95% CIs at the trace
    counts cardinal #2 requires). `ci_level` is the confidence
    level as a fraction in (0, 1).

    Raises `ValueError` on:
    - empty `deltas` (caller must filter; bootstrap on zero samples
      is undefined),
    - `resamples < 1`,
    - `ci_level not in (0, 1)`.

    Cardinal #1: structural input errors raise typed `ValueError`,
    not silent zeros. The pipeline layer catches and surfaces these
    as structured `ReplayFailure` if they originate from per-trace
    data; for direct callers, the exception is the right shape.

    ## Why `seed` is required but `resamples` defaults

    `seed` carries reproducibility — without it, the same input
    produces a different CI each call, which silently breaks
    cardinal #4 (determinism opt-in per field). Forcing the caller
    to pass it makes the choice explicit; `seed=0` is a legitimate
    "I don't care" answer, but it's an answer, not an omission.

    `resamples` carries statistical power, not correctness. 2000 is
    the convergent default in the bootstrap literature for 95%
    percentile CIs at sample sizes in the cardinal-#2 floor range
    (n in [10, 200]); a caller who passes a different value usually
    has a calibration reason, and the default is safe when they
    don't. Forcing the caller to pass `resamples` would be ceremony
    without doctrinal payoff.

    `ci_level` follows the same logic — 0.95 is the convergent
    confidence level for the report's primary CI surface; opting
    out is a legitimate calibration choice but the default is safe.
    """
    if not deltas:
        raise ValueError("paired_percentile_bootstrap: deltas must be non-empty")
    if resamples < 1:
        raise ValueError(f"paired_percentile_bootstrap: resamples must be >= 1, got {resamples}")
    if not (0.0 < ci_level < 1.0):
        raise ValueError(f"paired_percentile_bootstrap: ci_level must be in (0, 1), got {ci_level}")

    rng = random.Random(seed)
    n = len(deltas)
    deltas_list = list(deltas)
    # Bootstrap distribution of the median statistic.
    bootstrap_medians: list[float] = []
    for _ in range(resamples):
        # Paired resample with replacement: draw n indices, take
        # the corresponding deltas. Each draw preserves the paired-
        # trace unit (cardinal #10: the unit of analysis is the
        # delta, not the original or replayed in isolation).
        sample = rng.choices(deltas_list, k=n)
        bootstrap_medians.append(statistics.median(sample))

    # Empirical percentile method: sort the bootstrap distribution,
    # take the (alpha/2, 1 - alpha/2) percentiles. For ci_level=0.95
    # that's the 2.5th and 97.5th percentile of the resampled
    # medians.
    bootstrap_medians.sort()
    alpha = 1.0 - ci_level
    # Edge case: `resamples=1` is structurally valid (the validator
    # only blocks resamples<1) but produces a degenerate CI where
    # `lower_idx == upper_idx == 0` — both bounds collapse to
    # `bootstrap_medians[0]`. Callers running with resamples=1
    # almost certainly want the deterministic-test behavior; for
    # real CI estimation the cascade-catalog default of
    # `resamples >= 200` is the floor.
    lower_idx = round((alpha / 2.0) * (resamples - 1))
    upper_idx = round((1.0 - alpha / 2.0) * (resamples - 1))
    ci_lower = bootstrap_medians[lower_idx]
    ci_upper = bootstrap_medians[upper_idx]

    return BootstrapResult(
        median=statistics.median(deltas_list),
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        resamples=resamples,
        seed=seed,
        ci_level=ci_level,
    )
