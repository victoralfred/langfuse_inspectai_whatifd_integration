"""Floor evaluator and `FloorPassedProof` witness token (cardinal rule #2).

The trust floor is structural — it cannot be bypassed by configuration.
This module implements the type-level enforcement: `Ship` requires a
`FloorPassedProof`, and the only function that can produce one is
`evaluate_floor()`. Any code path that constructs a Ship is forced to
go through floor evaluation.

## The closure-capture pattern (CASCADE-010, v0.1 resolution)

The original deliberation considered three approaches to enforcing
"only `evaluate_floor` can produce valid proofs":

1. **Underscore-token convention** — `_FLOOR_INTERNAL_TOKEN = object()`
   at module level; constructor compares against it. Bypassable by
   `from whatifd.decision.floor import _FLOOR_INTERNAL_TOKEN` (single-
   underscore is private by convention only). Rejected: convention,
   not structural.

2. **Closure-capture** (this implementation) — the token lives in the
   closure of `_build_floor_machinery()`. After that function returns,
   the local `_floor_token` is unreachable through any module-level
   name. The class `FloorPassedProof` and the function `evaluate_floor`
   both close over the token; they're returned to module scope, but
   the token itself is not. Adversarial code that imports this module
   thoroughly cannot find a name that resolves to the token.

3. **Verify-on-construction** — `Ship.__post_init__` re-runs floor logic
   to verify the proof matches its cohort results. Closes cross-run
   proof reuse but adds runtime cost. Deferred to v1.0; CASCADE-205.

This module ships option 2. CASCADE-010 is resolved-for-v0.1 by this
file. The v1.0 hardening (cohort-hash binding) is a separate cascade.

## Known v0.1 limit

Closure-capture is bypassable by Python introspection:
`FloorPassedProof.__init__.__closure__[N].cell_contents` extracts the
captured token. Any contributor running that as production code is
visibly doing something adversarial — code review catches it. The
property test "no DecisionPolicy configuration produces Ship when
evaluate_floor returns FloorFailureSet" (Phase 2 gate) covers
configuration-coverage gaps. Together: structural enforcement against
accidental bypass; visible enforcement against deliberate bypass;
property test against policy-coverage gaps.

## Layering and mypy reconciliation

`FloorPassedProof` lives here, not in `whatifd/types/verdict.py`,
because closure-capture requires the class and its producer to be in
the same module. `Ship` (in `types/verdict.py`) imports `FloorPassedProof`
under `TYPE_CHECKING` so the runtime types/ layer carries no
dependency on decision/.

mypy can't analyze classes built inside closures as named types. To
satisfy strict typing, this module declares `FloorPassedProof` and
`FloorFailureSet` as `TYPE_CHECKING`-only stubs at module top — mypy
sees those stubs as the public type. At runtime, the same names are
rebound to the closure-built classes (with `# type: ignore[no-redef]`
on the rebind). Static analysis and runtime see consistent surface;
the closure-capture protection is preserved at runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from whatifd.types.cohort import CohortResult, FloorFailure
from whatifd.types.policy import TrustFloor
from whatifd.types.primitives import DecimalString

if TYPE_CHECKING:
    # Type stubs for static analysis. The runtime classes are built inside
    # `_build_floor_machinery()` with closure-captured validation; these
    # stubs declare the public surface so mypy can resolve `FloorPassedProof`
    # as a type in `Ship.proof`.

    class FloorPassedProof:
        """Witness token; runtime impl is closure-captured below."""

        floor_version: str
        evaluated_at: str

        def __init__(
            self,
            *,
            _token: object,
            floor_version: str,
            evaluated_at: str,
        ) -> None: ...

        def __repr__(self) -> str: ...
        def __eq__(self, other: object) -> bool: ...
        def __hash__(self) -> int: ...

    @dataclass(frozen=True, slots=True)
    class FloorFailureSet:
        """Failure-branch return; runtime impl is closure-captured below."""

        failures: list[FloorFailure] = field(default_factory=list)

        def __iter__(self) -> Iterator[FloorFailure]: ...
        def __len__(self) -> int: ...
        def __bool__(self) -> bool: ...


def _build_floor_machinery() -> tuple[type, type, Callable[..., object]]:
    """Module-init factory.

    Binds `_floor_token` in this function's closure. Returns the runtime
    `FloorPassedProof` class, `FloorFailureSet` class, and `evaluate_floor`
    function. The token is unreachable from any module-level name — only
    the closures of `FloorPassedProof.__init__` and `evaluate_floor` see it.
    """
    _floor_token = object()

    class FloorPassedProof:
        __slots__ = ("evaluated_at", "floor_version")

        floor_version: str
        evaluated_at: str

        def __init__(
            self,
            *,
            _token: object,
            floor_version: str,
            evaluated_at: str,
        ) -> None:
            if _token is not _floor_token:
                raise TypeError(
                    "FloorPassedProof cannot be constructed externally. "
                    "Obtain a proof via evaluate_floor()."
                )
            object.__setattr__(self, "floor_version", floor_version)
            object.__setattr__(self, "evaluated_at", evaluated_at)

        def __setattr__(self, name: str, value: object) -> None:
            raise AttributeError(f"FloorPassedProof is immutable; cannot set {name!r}")

        def __repr__(self) -> str:
            return (
                f"FloorPassedProof(floor_version={self.floor_version!r}, "
                f"evaluated_at={self.evaluated_at!r})"
            )

        def __eq__(self, other: object) -> bool:
            if not isinstance(other, FloorPassedProof):
                return NotImplemented
            return (
                self.floor_version == other.floor_version
                and self.evaluated_at == other.evaluated_at
            )

        def __hash__(self) -> int:
            return hash((self.floor_version, self.evaluated_at))

    @dataclass(frozen=True, slots=True)
    class FloorFailureSet:
        failures: list[FloorFailure] = field(default_factory=list)

        def __iter__(self) -> Iterator[FloorFailure]:
            return iter(self.failures)

        def __len__(self) -> int:
            return len(self.failures)

        def __bool__(self) -> bool:
            return bool(self.failures)

    def evaluate_floor(
        cohort_results: Sequence[CohortResult],
        floor: TrustFloor,
        required_cohorts: Sequence[str],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> FloorPassedProof | FloorFailureSet:
        """Evaluate the trust floor for a run.

        Per cardinal rule #2, the floor is structural: failing any rule
        for any required cohort yields a `FloorFailureSet` (the run is
        Inconclusive); passing all rules yields a `FloorPassedProof`
        (the only object that allows `Ship` construction).

        Per-cohort rule failures are computed from each cohort's raw
        counts (`selected`, `replayed`, `scored`) by
        `compute_cohort_floor_failures`. Aggregation across required
        cohorts happens here.

        A required cohort that's absent from `cohort_results` is itself a
        `blocks_all` floor failure with rule `"required_cohort_present"`
        — distinct from the per-cohort numeric rules so the renderer can
        surface the missing-cohort case directly.

        An empty `required_cohorts` is itself a structural failure under
        cardinal #2 — it would otherwise produce a vacuous proof
        (no rules to fail) and let `Ship` construct on a misconfigured
        policy. We emit `required_cohorts_nonempty` (severity `blocks_all`)
        so the floor refuses to issue a proof for a policy with nothing
        to require. Per cardinal #1 this is a structured failure, not an
        exception — the verdict layer turns it into Inconclusive with an
        actionable message.

        `now` is injectable for deterministic tests; defaults to UTC wall
        clock. The proof's `evaluated_at` is the ISO 8601 string at the
        moment the floor passed.
        """
        if not required_cohorts:
            return FloorFailureSet(
                failures=[
                    FloorFailure(
                        rule="required_cohorts_nonempty",
                        observed=0,
                        threshold=1,
                        severity="blocks_all",
                    )
                ]
            )

        cohorts_by_name = {c.name: c for c in cohort_results}
        all_failures: list[FloorFailure] = []
        for required in required_cohorts:
            cohort = cohorts_by_name.get(required)
            if cohort is None:
                all_failures.append(
                    FloorFailure(
                        rule="required_cohort_present",
                        observed="absent",
                        threshold=1,
                        severity="blocks_all",
                    )
                )
                continue
            all_failures.extend(compute_cohort_floor_failures(cohort, floor))

        if all_failures:
            return FloorFailureSet(failures=all_failures)

        clock = now if now is not None else (lambda: datetime.now(UTC))
        return FloorPassedProof(
            _token=_floor_token,
            floor_version=floor.version,
            evaluated_at=clock().isoformat(),
        )

    return FloorPassedProof, FloorFailureSet, evaluate_floor


def compute_cohort_floor_failures(
    cohort: CohortResult,
    floor: TrustFloor,
) -> list[FloorFailure]:
    """Per-cohort floor evaluation against the four rules in `TrustFloor`.

    The rules and their canonical names match `TrustFloor.rule_names()`.
    Counts below threshold yield `blocks_all` failures (no evidence
    exists). The replay-validity ratio yields a `blocks_ship` failure
    when below threshold but with non-zero `selected` (some evidence
    exists, but its quality is below the floor).

    The ratio rule is skipped when `selected == 0` — the
    `min_selected_per_required_cohort` rule already catches that case
    with higher severity, and `0/0` has no meaningful ratio.

    Returns an empty list when the cohort passes all rules.
    """
    failures: list[FloorFailure] = []

    if cohort.selected < floor.min_selected_per_required_cohort:
        failures.append(
            FloorFailure(
                rule="min_selected_per_required_cohort",
                observed=cohort.selected,
                threshold=floor.min_selected_per_required_cohort,
                severity="blocks_all",
            )
        )

    if cohort.replayed < floor.min_replayed_per_required_cohort:
        failures.append(
            FloorFailure(
                rule="min_replayed_per_required_cohort",
                observed=cohort.replayed,
                threshold=floor.min_replayed_per_required_cohort,
                severity="blocks_all",
            )
        )

    if cohort.scored < floor.min_scored_per_required_cohort:
        failures.append(
            FloorFailure(
                rule="min_scored_per_required_cohort",
                observed=cohort.scored,
                threshold=floor.min_scored_per_required_cohort,
                severity="blocks_all",
            )
        )

    if cohort.selected > 0:
        ratio = cohort.replayed / cohort.selected
        if ratio < floor.min_replay_validity_ratio_per_required_cohort:
            failures.append(
                FloorFailure(
                    rule="min_replay_validity_ratio_per_required_cohort",
                    observed=cast(DecimalString, format(ratio, ".3f")),
                    threshold=floor.min_replay_validity_ratio_per_required_cohort,
                    severity="blocks_ship",
                )
            )

    return failures


# Bind the closure-captured machinery to module-level names. The token
# itself is NOT bound at module level — it lives only in the closure of
# FloorPassedProof.__init__ and evaluate_floor.
#
# mypy already saw the TYPE_CHECKING stubs at the top of this file; the
# runtime rebind is the actual class. We accept the [misc, assignment]
# ignore as the cost of structural enforcement — closure-capture is
# stronger than module-level convention; the cost is mypy ergonomics.
FloorPassedProof, FloorFailureSet, evaluate_floor = _build_floor_machinery()  # type: ignore[misc, assignment]
