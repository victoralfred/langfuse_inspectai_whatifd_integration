"""`replay_one_trace` — the per-trace replay kernel.

Phase 6.3a of the v0.1 implementation plan. The kernel is the
boundary that converts the three classes of runner-execution
failure into typed `ReplayFailure` records:

  - `CacheMissError` → `ReplayFailure(code="tool_cache_miss")`
  - timeout       → `ReplayFailure(code="runner_timeout")`
  - any other     → `ReplayFailure(code="runner_exception")`

The kernel is synchronous, per-trace, and standalone. Phase 6.3b
will compose it into a streaming pipeline with `ThreadPoolExecutor`
bounded concurrency; Phase 6.3c adds the async runner path. Keeping
the kernel as a pure function lets the streaming layer choose its
own concurrency strategy without rewriting the failure-conversion
logic.

## Why a separate kernel

Cardinal #1 says expected failures are structured data. The runner
is user code — it can:
- Call `tool_cache.lookup(...)` and get a `CacheMissError`
- Hang past the configured timeout
- Raise any Python exception

Each of these is a known failure mode that must produce a
`ReplayFailure`, not propagate as an exception out of the pipeline.
The kernel catches all three at one place; downstream code (the
pipeline streaming layer, the report assembler) sees a uniform
`ReplayResult` and never has to think about exception handling.

## Why threads for timeout (and the leaked-thread caveat)

Python has no portable way to kill a running thread. The kernel
uses `ThreadPoolExecutor(max_workers=1)` + `future.result(timeout=
...)`: on timeout, the future is cancelled (which only succeeds if
it hasn't started), but the underlying thread KEEPS RUNNING until
the user runner returns naturally. We accept this for v0.1:

- The runner is user-controlled code. We document the requirement
  that runners be timeout-aware (via inner I/O timeouts on HTTP
  clients, etc.) so the wall-clock limit isn't load-bearing.
- The kernel-level timeout is a backstop for runners that hang on
  e.g., an infinite loop. Such runners surface as `runner_timeout`
  in the report; the leaked thread eventually returns and is GC'd.
- A v0.2 hardening could move runners into a subprocess pool so
  termination is enforceable; not v0.1 scope.

The alternative timeout mechanisms (`signal.SIGALRM`, asyncio task
cancellation) have their own issues (main-thread-only; doesn't help
sync runners). The thread-pool approach is the least-bad default.

## Cardinal alignment

- **#1 failures-as-data:** all three exception classes catch and
  convert to typed `ReplayFailure`. Nothing escapes the kernel as
  an exception (a programmer bug — e.g., a `ReplayFailure`
  construction error from an unregistered code — does propagate;
  cardinal #1 covers EXPECTED failures, not whatifd bugs).
- **#5 sensitive data wrapped:** the kernel never inspects the
  runner's `ReplayOutput` for sensitive content. Adapter-side
  wrapping (Phase 4) is the responsibility for that.
- **#9 orchestration not compute:** the kernel orchestrates a
  single user-runner call. No data transformation, no scoring, no
  decision logic.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING

from whatifd.replay.result import ReplayFailure, ReplayResult, ReplaySuccess
from whatifd.replay.tool_cache import CacheMissError

if TYPE_CHECKING:
    from whatifd.contract import ReplayConfig, Runner, ToolCache, TraceInput


def replay_one_trace(
    *,
    trace_id: str,
    cohort: str,
    trace_input: TraceInput,
    config: ReplayConfig,
    tool_cache: ToolCache,
    runner: Runner,
    timeout_seconds: float,
) -> ReplayResult:
    """Run the runner for one trace; return a typed result.

    The runner is invoked in a worker thread so a wall-clock timeout
    can fire. The kernel returns:

    - `ReplaySuccess(trace_id, cohort, output)` when the runner
      returns a `ReplayOutput` cleanly within the timeout.
    - `ReplayFailure(code="tool_cache_miss")` when the runner's
      `tool_cache.lookup(...)` raised `CacheMissError`.
    - `ReplayFailure(code="runner_timeout")` when the runner did
      not return within `timeout_seconds`.
    - `ReplayFailure(code="runner_exception")` for any other
      exception escaping the runner.

    The order of catches matters: `CacheMissError` is more specific
    than the bare-`Exception` catch and must come first so a cache
    miss is classified correctly even though it IS a Python
    exception.

    Re-raise guarantee: `concurrent.futures.Future.result()`
    re-raises the worker thread's exception UNWRAPPED. CPython
    `_invoke_callbacks` stores the exception object via
    `self._exception = exc`, and `result()` does
    `raise self._exception`. So `CacheMissError` raised inside the
    runner thread arrives at this `except` clause as
    `CacheMissError`, not wrapped in any "thread exception" type.
    The `test_cache_miss_produces_typed_failure` test pins this end
    to end (the assertion `result.code == "tool_cache_miss"` would
    fail under any future Python that wrapped exceptions).

    `timeout_seconds` is enforced via `concurrent.futures.Future
    .result(timeout=...)`. On timeout, the underlying thread keeps
    running until the runner completes (Python can't kill threads);
    the kernel still returns immediately with the typed failure.
    See module docstring for the leaked-thread caveat.
    """
    # `max_workers=1` and a fresh executor per call: kernel calls
    # are independent and the streaming layer (Phase 6.3b) will
    # supply outer parallelism. A shared executor would serialize
    # kernel calls behind a queue, defeating the streaming layer's
    # bounded concurrency.
    #
    # Manual executor lifecycle (no `with`): the with-block's
    # `__exit__` calls `shutdown(wait=True)` which BLOCKS on the
    # running thread — defeating the timeout's purpose. We need to
    # call `shutdown(wait=False)` on the timeout path so the kernel
    # returns immediately while the runner thread leaks. The clean-
    # path shutdown waits as normal (the runner has already returned).
    ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whatifd-replay")
    future = ex.submit(runner, trace_input, config, tool_cache)

    try:
        output = future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        # Timeout: detach the executor (wait=False) so the kernel
        # returns immediately. The orphaned thread keeps running
        # until the runner returns naturally — Python can't kill
        # threads. NO subsequent shutdown(wait=True) call: that
        # would join() the thread and re-block.
        #
        # `future.cancel()` returns False here because the future
        # is already running (we got a timeout, so the worker
        # picked up the task). The concurrent.futures contract:
        # cancel() only succeeds for not-yet-started futures.
        # Harmless to call; documents intent ("we tried"); does
        # nothing in practice.
        future.cancel()
        ex.shutdown(wait=False)
        return _timeout_failure(
            trace_id=trace_id,
            cohort=cohort,
            timeout_seconds=timeout_seconds,
        )
    except CacheMissError as exc:
        ex.shutdown(wait=True)
        return ReplayFailure(
            trace_id=trace_id,
            cohort=cohort,
            code="tool_cache_miss",
            message=str(exc),
            details=exc.details_for_failure(),
        )
    except Exception as exc:
        # Any other exception escaping the runner is classified as
        # `runner_exception` per the registry. The exception type
        # and message land in `details`; the registry spec for
        # `runner_exception` lists them as required_details.
        ex.shutdown(wait=True)
        return _exception_failure(trace_id=trace_id, cohort=cohort, exc=exc)
    else:
        ex.shutdown(wait=True)
        return ReplaySuccess(trace_id=trace_id, cohort=cohort, output=output)


def _timeout_failure(*, trace_id: str, cohort: str, timeout_seconds: float) -> ReplayFailure:
    return ReplayFailure(
        trace_id=trace_id,
        cohort=cohort,
        code="runner_timeout",
        message=(
            f"runner exceeded {timeout_seconds}s timeout on trace {trace_id!r}. "
            "The replay thread continues until the runner returns naturally; "
            "Python cannot kill threads. Configure inner I/O timeouts in the "
            "runner to make the wall-clock limit a backstop, not the primary "
            "bound."
        ),
        # `runner_timeout` registry spec: required_details=
        # ("timeout_seconds",). Always emitted as float for shape
        # consistency with the parameter type. JsonPrimitive accepts
        # both int and float, but mixing produces a less predictable
        # downstream parse (jsonschema number vs integer arms).
        details={"timeout_seconds": float(timeout_seconds)},
    )


def _exception_failure(*, trace_id: str, cohort: str, exc: Exception) -> ReplayFailure:
    # Truncate the exception message at a reasonable bound — a
    # runner that raises with a 1MB message would otherwise bloat
    # the report. 2048 chars is enough for a typical traceback's
    # last line plus context.
    raw_message = str(exc)
    truncated = raw_message if len(raw_message) <= 2048 else raw_message[:2048] + "...(truncated)"
    return ReplayFailure(
        trace_id=trace_id,
        cohort=cohort,
        code="runner_exception",
        message=f"runner raised {type(exc).__name__} on trace {trace_id!r}: {truncated}",
        # `runner_exception` registry spec: required_details=
        # ("exception_type", "message").
        details={
            "exception_type": type(exc).__name__,
            "message": truncated,
        },
    )


# Public surface: the kernel function. Internal helpers stay private
# (leading underscore) so they don't accumulate as accidental API.
__all__ = ["replay_one_trace"]
