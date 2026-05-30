"""`InspectAIScorer` — implementation of `whatifd.adapters.Scorer`.

Wraps an Inspect AI scorer callable and produces `JudgeResult` +
`CacheKeyComponents` per the whatifd `Scorer` protocol. The
runtime contract follows `whatifd.adapters.protocols.Scorer`; the
conformance harness at `tests/adapters/conformance.py` is the
gating test.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from whatifd.adapters.protocols import (
    AdapterMetadata,
    JudgeResult,
)
from whatifd.cache.keying.v1 import CacheKeyComponents
from whatifd.contract import ScoreCase
from whatifd.types.sensitive import Sensitive

_log = logging.getLogger(__name__)

ADAPTER_ID = "inspect_ai"
SCORER_TYPE = "inspect_ai"
WHATIF_SCHEMA_VERSION = "v0.1"
SCORE_CASE_SERIALIZATION_VERSION = "v1"


@runtime_checkable
class _ScoreLike(Protocol):
    """Structural shape this adapter needs from an Inspect AI
    `Score`. Defined as a Protocol so tests can substitute a fake
    without importing inspect_ai, AND so any future Inspect AI
    SDK that renames or restructures fields can be adapted with a
    small projection rather than a rewrite. Mirrors the field set
    `inspect_ai.scorer.Score` exposes (`value`, `answer`,
    `explanation`, `metadata`, `history`)."""

    value: Any
    explanation: Any


# An Inspect AI scorer is `Callable[[TaskState, Target], Score | None]`
# at the type level (Inspect's Scorer Protocol declares sync return,
# but the framework awaits if the impl is `async def`). The adapter
# accepts the broader shape: `(case: ScoreCase) -> Score | None |
# Awaitable[Score | None]`. The caller is responsible for wiring
# their Inspect AI scorer into a `(ScoreCase) -> ...` callable —
# typically a small lambda that constructs a `TaskState` from the
# case and invokes the scorer.
_ScoreFn = Callable[[ScoreCase], Any]


def _hash16(*parts: str) -> str:
    """Produce a 16-hex-char digest from the joined parts. Matches
    `CacheKeyComponents.__post_init__` invariant (≥16 lowercase
    hex)."""
    h = hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()
    return h[:16]


def _hash16_mapping(m: Mapping[str, Any]) -> str:
    """Deterministic 16-hex-char digest of a small parameter
    mapping. Sorted keys + repr-of-value so dict-insertion-order
    doesn't change the hash."""
    pairs = sorted(f"{k}={v!r}" for k, v in m.items())
    return _hash16("params", *pairs)


@dataclass(frozen=True, slots=True)
class InspectAIScorer:
    """Inspect AI `Scorer` adapter.

    Construct with:
    - `score_fn`: callable `(ScoreCase) -> Score | None | Awaitable[
       Score | None]`. The caller wires their Inspect AI scorer
       into this callable — typically a small lambda that builds
       a `TaskState` from the `ScoreCase` and invokes the Inspect
       scorer. Async return values are awaited via `asyncio.run`,
       which creates a fresh event loop and therefore CANNOT be
       called from within an already-running loop (e.g., Jupyter,
       FastAPI, async test runners). Callers in async contexts
       should run `score()` on a worker thread (e.g.,
       `asyncio.to_thread(scorer.score, case)`) or pre-resolve the
       coroutine before passing a sync `score_fn`.
    - `judge_provider` / `judge_model_id` / `judge_model_snapshot`:
       the model-provider identifiers for cache-key components and
       methodology disclosure. `judge_model_snapshot` is `str |
       None` because not every provider exposes a snapshot pin;
       absent providers MUST pass `None` explicitly so the field's
       presence in the cache-key shape is constant (cardinal #6).
    - `rubric_id` / `rubric_text`: the human-named identifier and
       the literal rubric text. Hashed into the cache key so a
       rubric edit invalidates cache entries.
    - `scoring_parameters`: an arbitrary `Mapping[str, Any]` of
       knobs (temperature, max_tokens, etc.). Hashed into the
       cache key. Default empty.
    - `sdk_version`: optional override for the Inspect AI version
       string. Defaults to the installed `inspect_ai.__version__`.

    Cardinal #5: `Score.explanation` is wrapped as `Sensitive[str]`
    in `_project_score`. Cardinal #1: a `score_fn` that returns
    None or raises surfaces as `JudgeResult(score=None)`; the
    pipeline converts that into a `FailureRecord`.
    """

    score_fn: _ScoreFn
    judge_provider: str
    judge_model_id: str
    rubric_id: str
    rubric_text: str
    judge_model_snapshot: str | None = None
    scoring_parameters: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    sdk_version: str | None = None

    def score(self, case: ScoreCase) -> JudgeResult:
        try:
            result = self.score_fn(case)
            if inspect.iscoroutine(result):
                # asyncio.run requires a coroutine; iscoroutine narrows
                # the type from generic Awaitable to Coroutine. Inspect
                # AI scorers using `async def` produce coroutines, which
                # is the supported case.
                result = asyncio.run(result)
        except Exception as exc:  # boundary catch; surfaces as cardinal-#1 None-score
            _log.debug(
                "InspectAIScorer.score_fn raised on trace_id=%s: %s",
                case.trace_id,
                exc,
            )
            return JudgeResult(
                trace_id=case.trace_id,
                score=None,
                rationale=Sensitive(
                    f"score_fn raised: {type(exc).__name__}: {exc}",
                    classification="judge_rationale",
                ),
                judge_model_id=self.judge_model_id,
                judge_model_snapshot=self.judge_model_snapshot,
            )
        return self._project_score(case, result)

    def cache_key_components(self, case: ScoreCase) -> CacheKeyComponents:
        return CacheKeyComponents(
            whatif_schema_version=WHATIF_SCHEMA_VERSION,
            whatif_scorer_adapter_version=_resolve_package_version(),
            scorer_type=SCORER_TYPE,
            scorer_package_version=_resolve_sdk_version(self.sdk_version),
            judge_provider=self.judge_provider,
            judge_model_id=self.judge_model_id,
            judge_model_snapshot=self.judge_model_snapshot,
            rendered_prompt_hash=_hash16("prompt", self.rubric_text, case.input.user_message),
            rubric_hash=_hash16("rubric", self.rubric_id, self.rubric_text),
            scoring_parameters_hash=_hash16_mapping(self.scoring_parameters),
            score_case_serialization_version=SCORE_CASE_SERIALIZATION_VERSION,
            score_case_hash=_hash16("case", case.trace_id, case.cohort),
        )

    def adapter_metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            adapter_id=ADAPTER_ID,
            package_version=_resolve_package_version(),
            sdk_version=_resolve_sdk_version(self.sdk_version),
        )

    def _project_score(self, case: ScoreCase, raw: _ScoreLike | None) -> JudgeResult:
        """Project an Inspect AI `Score | None` into a `JudgeResult`.

        `None` → cardinal-#1 structural failure
        (`JudgeResult.score=None`). A `Score` with non-numeric
        `value` is also surfaced as `score=None` with a rationale
        pointing at the projection failure; downstream guards see
        the structured signal rather than crashing on `float(...)`.
        """
        if raw is None:
            return JudgeResult(
                trace_id=case.trace_id,
                score=None,
                rationale=Sensitive(
                    "Inspect AI scorer returned None (structural failure)",
                    classification="judge_rationale",
                ),
                judge_model_id=self.judge_model_id,
                judge_model_snapshot=self.judge_model_snapshot,
            )

        score_value: float | None
        try:
            score_value = float(raw.value)
        except (TypeError, ValueError):
            _log.debug(
                "Inspect AI Score.value is not float-coercible "
                "(trace_id=%s, value=%r); surfacing as cardinal-#1 None",
                case.trace_id,
                raw.value,
            )
            score_value = None

        explanation_raw = "" if raw.explanation is None else str(raw.explanation)
        return JudgeResult(
            trace_id=case.trace_id,
            score=score_value,
            rationale=Sensitive(explanation_raw, classification="judge_rationale"),
            judge_model_id=self.judge_model_id,
            judge_model_snapshot=self.judge_model_snapshot,
        )


def _resolve_package_version() -> str:
    """Read this package's `__version__` lazily to avoid an import
    cycle between `__init__.py` (which imports `InspectAIScorer`
    from this module) and this module reading the package version
    at import time."""
    from whatifd_inspect_ai import __version__

    return __version__


def _resolve_sdk_version(override: str | None) -> str:
    """Read `inspect_ai.__version__` lazily so importing this module
    doesn't drag the SDK in until it's actually needed (e.g., a
    test that constructs `InspectAIScorer` with a fake `score_fn`
    never imports inspect_ai). Falls back to the literal "unknown"
    string if the SDK isn't installed; `CacheKeyComponents` rejects
    empty strings on `scorer_package_version`, so we MUST emit a
    non-empty placeholder.
    """
    if override is not None:
        return override
    try:
        import inspect_ai

        version = getattr(inspect_ai, "__version__", None)
        if version is None:
            _log.debug("inspect_ai SDK imported but exposes no __version__ attribute")
            return "unknown"
        return str(version)
    except ImportError:
        _log.debug(
            "inspect_ai SDK not installed; AdapterMetadata.sdk_version "
            "and CacheKeyComponents.scorer_package_version will fall "
            "back to 'unknown'."
        )
        return "unknown"
