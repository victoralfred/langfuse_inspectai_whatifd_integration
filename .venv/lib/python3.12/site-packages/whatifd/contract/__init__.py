"""whatifd runner contract - the user-facing API for - target`.

A trace alone is not executable. To replay an agent with a modified config,
`whatifd` calls a user-supplied runner that knows how to reconstitute the agent.

The user runner produces *only* the replayed output. `whatifd` owns everything
else - the original trace artifact, the cohort label, the metadata, the
comparison, the scoring, the verdict.

Example:
    # my_agent/replay.py
    from whatifd.contract import TraceInput, ReplayConfig, ToolCache, ReplayOutput

    def run(
        trace_input: TraceInput,
        config: ReplayConfig,
        tool_cache: ToolCache,
    ) -> ReplayOutput:
        agent = build_agent(
            system_prompt=config.system_prompt,
            tool_cache=tool_cache,
        )
        text = agent.run(trace_input.user_message)
        return ReplayOutput(text=text)

Then on the command line:

    whatifd fork --target "python:my_agent.replay:run" ...
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Inputs the runner receives
# ---------------------------------------------------------------------------


class TraceInput(BaseModel):
    """The original user input recovered from a production trace.

    This is what your agent originally received. Your runner is asked to
    re-execute starting from this input, but with the proposed `config` change
    applied.
    """

    model_config = ConfigDict(extra="allow")

    user_message: str = Field(
        ..., description="The user-visible message that originally entered the agent."
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form trace metadata pulled from the source tracer.",
    )


class ReplayConfig(BaseModel):
    """The modified configuration to apply during replay.

    Only fields whose values differ from the original config will be set;
    everything else falls back to whatever your `build_agent()` defaults to.

    v0.1 supports `system_prompt` only. v0.2 adds `model`. v0.3 widens to
    arbitrary tool / parameter overrides via the `overrides` dict.
    """

    model_config = ConfigDict(extra="forbid")

    system_prompt: str | None = Field(
        default=None,
        description="A new system prompt to apply for this replay (v0.1).",
    )
    model: str | None = Field(
        default=None,
        description="A new model identifier to apply (v0.2+).",
    )
    overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form per-tool/parameter overrides (v0.3+).",
    )


class ToolCache(BaseModel):
    """Cached tool outputs from the original trace.

    When your agent calls a tool during replay, look up the cached output
    via `lookup(tool_name, args)` *before* calling the tool live. This is
    what prevents replay from re-firing side effects.

    v0.1 enforces a strict `use-original` policy: live calls are not
    permitted; if the cache misses, the trace is recorded as a replay
    failure (it surfaces in the report's "Replay validity" section).

    v0.3 adds an opt-in `live` policy with per-tool allowlists for cases
    where original outputs are stale (e.g. time-sensitive APIs).
    """

    model_config = ConfigDict(extra="forbid")

    cache: dict[str, Any] = Field(
        default_factory=dict,
        description="Internal: keyed by canonical tool-call signature.",
    )
    policy: Literal["use-original", "live"] = Field(
        default="use-original",
        description="Cache policy enforced by whatifd.",
    )

    def lookup(self, tool_name: str, args: dict[str, Any]) -> Any | None:
        """Return the cached tool output for this call, or None if absent."""
        return self.cache.get(self._key(tool_name, args))

    @staticmethod
    def _key(tool_name: str, args: dict[str, Any]) -> str:
        # Hash-input canonical encoding via the centralized helper.
        # Same pattern as `whatifd/cache/keying/v1.py`: hash inputs go
        # through `whatifd/serialization/canonical.py::canonical_json_bytes`
        # so the Phase 5 banned-import lint sees zero `json.dumps`
        # outside the serialization package.
        #
        # Lazy import: a top-level
        # `from whatifd.serialization import canonical_json_bytes`
        # cycles through `whatifd.serialization.lock_io` →
        # `whatifd.cache._types` → `whatifd.cache.lock` →
        # `whatifd.serialization`. The function-level import resolves
        # at call time, after all modules finish loading. Documented
        # in cascade-catalog "Serialization ↔ report ↔ cache import
        # cycle" — this is the same workaround used by
        # `whatifd.serialization.encoder` for its TYPE_CHECKING
        # ReportV01 reference.
        #
        # Per-call overhead: negligible. Python caches the resolved
        # module in `sys.modules` after the first load; subsequent
        # `from whatifd.serialization import ...` statements are dict
        # lookups (microsecond-scale), not re-imports. ToolCache._key
        # is called once per cache lookup; the cost is invisible
        # against the surrounding I/O.
        from whatifd.serialization import canonical_json_bytes

        return f"{tool_name}::{canonical_json_bytes(args).decode('ascii')}"


# ---------------------------------------------------------------------------
# Output the runner produces
# ---------------------------------------------------------------------------


class ReplayOutput(BaseModel):
    """The output your runner produces for a single replay.

    Keep this minimal: the final response text, plus any per-tool span
    information your agent collected. `whatifd` owns everything else
    (originals, cohort labels, comparison, scoring).
    """

    model_config = ConfigDict(extra="allow")

    text: str = Field(..., description="The agent's final response.")
    tool_spans: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per-tool spans recorded during replay (optional but useful).",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form metadata you want to attach to this replay.",
    )


# ---------------------------------------------------------------------------
# Internal: not constructed by user runners
# ---------------------------------------------------------------------------


class TraceOutput(BaseModel):
    """The original output as recorded in the production trace.

    Constructed by `whatifd` from the ingested trace. Users never construct
    this themselves; it's exposed here for type clarity in scoring code.
    """

    model_config = ConfigDict(extra="allow")

    text: str
    tool_spans: list[dict[str, Any]] = Field(default_factory=list)


class ScoreCase(BaseModel):
    """The unit handed to scorers - internal to `whatifd`.

    Constructed by `whatifd` from (a) the original trace artifact and
    (b) the user runner's `ReplayOutput`. The scorer compares
    `original_output` vs `replayed_output` and emits a delta.

    Users do not construct `ScoreCase` directly. Documented here so that
    custom scorer plugins (v0.2+) have a clear type to consume.
    """

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    cohort: Literal["failure", "baseline"]
    input: TraceInput
    original_output: TraceOutput
    replayed_output: ReplayOutput
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# The protocol whatifd expects when invoking --target
# ---------------------------------------------------------------------------


@runtime_checkable
class Runner(Protocol):
    """The shape `whatifd` expects when invoking your - target` (sync).

    Implement a function (or callable) matching this signature, then point
     - target` at it via the `python:module.path:attr` syntax:

        whatifd fork --target "python:my_agent.replay:run" ...
    """

    def __call__(
        self,
        trace_input: TraceInput,
        config: ReplayConfig,
        tool_cache: ToolCache,
    ) -> ReplayOutput: ...


@runtime_checkable
class AsyncRunner(Protocol):
    """Async variant of `Runner`. Implement when your agent is built
    on `httpx.AsyncClient`, `asyncio` primitives, or otherwise wants
    to express I/O-bound replay as native coroutines.

    The whatifd async kernel (`whatifd.replay.kernel_async.
    replay_one_trace_async`) consumes this protocol with portable
    cancellation: `asyncio.wait_for(timeout=...)` cancels the Task
    on expiry — no leaked-thread workaround needed (unlike the sync
    path).

    Sync and async runners are NOT interchangeable; the sync kernel
    runs sync runners in a thread pool, the async kernel awaits
    coroutines directly. The user picks one and uses the matching
    kernel/stream entry point.
    """

    def __call__(
        self,
        trace_input: TraceInput,
        config: ReplayConfig,
        tool_cache: ToolCache,
    ) -> Awaitable[ReplayOutput]: ...


__all__ = [
    "AsyncRunner",
    "ReplayConfig",
    "ReplayOutput",
    "Runner",
    "ScoreCase",
    "ToolCache",
    "TraceInput",
    "TraceOutput",
]
