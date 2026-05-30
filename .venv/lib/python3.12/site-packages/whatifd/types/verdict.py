"""Verdict types: `Ship`, `DontShip`, `Inconclusive`, `Verdict` union.

The terminal output of the decision pipeline. One of these three is
constructed per run; the projection layer in `whatifd/report/projection.py`
(Phase 5) flattens them into the public `ReportV01.verdict_state`.

Cardinal rule #2 (trust floor cannot be bypassed) is enforced at the
type level by the `proof: FloorPassedProof` field on `Ship`. The
`FloorPassedProof` class lives in `whatifd/decision/floor.py` because
the closure-capture pattern that prevents external construction
requires the class and its producer to be in the same module. This
module imports `FloorPassedProof` under `TYPE_CHECKING` only — runtime
types/ has no dependency on decision/.

The trust chain at type level:
1. `evaluate_floor()` is the only function with the closure-captured token.
2. `FloorPassedProof.__init__` validates the token; external construction raises.
3. ∴ `FloorPassedProof` instances exist only when `evaluate_floor()` produced them.
4. `Ship` requires a `FloorPassedProof`.
5. ∴ `Ship` cannot exist without floor passing.

`DontShip` and `Inconclusive` carry `blocking_findings` lists — subsets
of `findings` filtered by severity. `__post_init__` validates the
severity invariant (DontShip: blocks_ship only; Inconclusive: blocks_ship
or blocks_all).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from whatifd.types.cohort import CohortResult, FloorFailure
from whatifd.types.finding import DecisionFinding

if TYPE_CHECKING:
    from whatifd.decision.floor import FloorPassedProof


@dataclass(frozen=True, slots=True)
class Ship:
    """Verdict: the change can ship.

    Constructed only when `evaluate_floor()` returned a `FloorPassedProof`
    AND the policy guards produced no `blocks_ship` or `blocks_all`
    findings.

    The `proof` field is the witness — its presence guarantees the trust
    floor passed for the cohorts this verdict represents. The closure-
    capture in `whatifd/decision/floor.py` prevents anyone from
    fabricating a proof.

    Internal type. The public report shape (`ReportV01.verdict_state`)
    is `Literal["ship"]` — the projection layer (Phase 5) flattens.
    """

    proof: FloorPassedProof
    cohort_results: list[CohortResult]
    findings: list[DecisionFinding]


@dataclass(frozen=True, slots=True)
class DontShip:
    """Verdict: the change should not ship.

    At least one policy guard produced a `blocks_ship` finding. Floor
    passed (otherwise the verdict would be `Inconclusive`).

    `blocking_findings` is the subset of `findings` with severity
    `blocks_ship`. Validated in `__post_init__`.
    """

    cohort_results: list[CohortResult]
    findings: list[DecisionFinding]
    blocking_findings: list[DecisionFinding] = field(default_factory=list)

    def __post_init__(self) -> None:
        for f in self.blocking_findings:
            if f.severity != "blocks_ship":
                raise ValueError(
                    f"DontShip.blocking_findings entries must have "
                    f"severity='blocks_ship'; got {f.severity!r} "
                    f"for code={f.code!r}"
                )


@dataclass(frozen=True, slots=True)
class Inconclusive:
    """Verdict: the run cannot produce a credible Ship/Don't Ship.

    Either the trust floor failed (insufficient evidence; `floor_failures`
    is non-empty) OR a `blocks_all` finding fired (e.g., scorer cache
    locked, scoring stage couldn't run; `floor_failures` empty,
    `blocking_findings` carries the operational issue).

    `blocking_findings` is the subset of `findings` with severity in
    `{blocks_ship, blocks_all}`. Validated in `__post_init__`.
    """

    cohort_results: list[CohortResult]
    findings: list[DecisionFinding]
    blocking_findings: list[DecisionFinding] = field(default_factory=list)
    floor_failures: list[FloorFailure] = field(default_factory=list)

    def __post_init__(self) -> None:
        allowed = ("blocks_ship", "blocks_all")
        for f in self.blocking_findings:
            if f.severity not in allowed:
                raise ValueError(
                    f"Inconclusive.blocking_findings entries must have "
                    f"severity in {allowed}; got {f.severity!r} "
                    f"for code={f.code!r}"
                )


# Sealed union over the three verdict states. Pattern matching with
# `match` exhaustively covers all cases; mypy strict catches missing
# cases. Adding a new verdict state in v1.0 (e.g., `Conditionally Ship`)
# is a major schema bump per the public-schema versioning rules.
Verdict = Ship | DontShip | Inconclusive
