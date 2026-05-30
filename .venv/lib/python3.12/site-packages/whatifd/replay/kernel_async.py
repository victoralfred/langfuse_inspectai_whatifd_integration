"""`replay_one_trace_async` — async per-trace replay kernel.

Phase 6.3c of the v0.1 implementation plan. The async sibling to
`whatifd.replay.kernel.replay_one_trace`. Same three failure
classifications, same `ReplayResult` shape, different concurrency
primitive: a coroutine awaited under `asyncio.wait_for(timeout=...)`.

## Why a separate async kernel

Sync runners run in a thread pool (the sync kernel's
`ThreadPoolExecutor(max_workers=1)`); async runners are coroutines
that need to be awaited in the calling event loop. Wrapping an
async runner in a thread to use the sync kernel would defeat its
async-ness: the runner's `await`s would block on a fresh event
loop per call, defeating the whole point of running on the
caller's loop.

So: separate kernels, same failure-conversion contract. Callers
choose based on their runner shape; mixing is not supported.

## Why async cancellation works (unlike threads)

`asyncio.wait_for(coro, timeout=...)` schedules a cancellation:
on expiry, it sends a `CancelledError` into the running task,
which propagates out at the next `await` boundary. Async cleanup
runs (try/finally / context manager exits / `async with` releases).
The runner stops cleanly; nothing leaks.

Contrast with the sync kernel's threads: Python has no portable
way to kill a thread, so on timeout the runner thread keeps
running until it returns naturally. The async path doesn't have
this problem — `wait_for` cancellation IS portable. The cascade-
catalog entry "Per-trace ThreadPoolExecutor + leaked-thread-on-
timeout pattern" notes this explicitly: "async cancellation IS
portable (`asyncio.Task.cancel()`), so the async path doesn't
need the leaked-thread workaround."

## Cardinal alignment

- **#1 failures-as-data:** identical to the sync kernel. The
  three exception classes catch and convert to typed
  `ReplayFailure`. `BaseException` (KeyboardInterrupt, SystemExit,
  CancelledError-NOT-from-timeout) propagates per the same
  doctrine.
- **#5 sensitive data wrapped:** the kernel never inspects the
  runner's `ReplayOutput`. Adapter-side wrapping (Phase 4) owns
  that.
- **#9 orchestration not compute:** orchestrates a single async
  runner call. The async loop is the caller's.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from whatifd.replay.result import ReplayFailure, ReplayResult, ReplaySuccess
from whatifd.replay.tool_cache import CacheMissError

if TYPE_CHECKING:
    from whatifd.contract import AsyncRunner, ReplayConfig, ToolCache, TraceInput


async def replay_one_trace_async(
    *,
    trace_id: str,
    cohort: str,
    trace_input: TraceInput,
    config: ReplayConfig,
    tool_cache: ToolCache,
    runner: AsyncRunner,
    timeout_seconds: float,
) -> ReplayResult:
    """Async replay kernel; mirrors `replay_one_trace` failure
    classification but on a coroutine.

    Returns:

    - `ReplaySuccess(trace_id, cohort, output)` when the runner
      coroutine returns a `ReplayOutput` cleanly within the
      timeout.
    - `ReplayFailure(code="tool_cache_miss")` when the runner's
      `tool_cache.lookup(...)` raised `CacheMissError`.
    - `ReplayFailure(code="runner_timeout")` when the runner
      coroutine did not complete within `timeout_seconds`.
    - `ReplayFailure(code="runner_exception")` for any other
      exception escaping the runner.

    The order of catches matches the sync kernel: `CacheMissError`
    before bare `Exception`. `asyncio.TimeoutError` (Python 3.11+:
    `TimeoutError`) is the timeout signal from `asyncio.wait_for`.

    Cancellation discipline: an `asyncio.CancelledError` arriving
    from OUTSIDE the timeout (e.g., the caller cancelled the
    `replay_one_trace_async` Task itself) propagates — it's NOT
    swept into `runner_exception`. The cardinal #1 doctrine
    excludes `BaseException` from "expected failures";
    `CancelledError` inherits `BaseException` (Python 3.8+) for
    exactly this reason. Only the timeout's own `TimeoutError`
    becomes `ReplayFailure(runner_timeout)`.
    """
    # Catch ordering, async vs sync:
    #
    # In the SYNC kernel (kernel.py), the catches are:
    #     FuturesTimeoutError -> CacheMissError -> Exception
    # The order matters because CacheMissError IS an Exception
    # subclass, so it MUST be caught before the bare-Exception
    # branch. FuturesTimeoutError comes from `Future.result(timeout
    # =...)` — a fundamentally different mechanism that doesn't
    # propagate from runner code, so its position is for clarity
    # not correctness.
    #
    # Here in the ASYNC kernel, the catches are:
    #     TimeoutError -> CacheMissError -> Exception
    # TimeoutError is raised by `asyncio.wait_for` ONLY (not by
    # the runner — async runners don't raise TimeoutError on their
    # own clock; they're cancelled FROM OUTSIDE by wait_for). It
    # does NOT overlap with CacheMissError or any user-runner
    # exception. The ordering matches the sync kernel's intent
    # (CacheMissError before bare Exception is the load-bearing
    # constraint); the timeout-first position is parallel structure
    # so the two kernels read alike.
    runner_coro = runner(trace_input, config, tool_cache)
    try:
        output = await asyncio.wait_for(runner_coro, timeout=timeout_seconds)
    except TimeoutError:
        # Python 3.11+: asyncio.TimeoutError is an alias for
        # builtins.TimeoutError. `wait_for` cancelled the task
        # cleanly on expiry; cleanup ran in the runner via
        # CancelledError -> finally / async-with exits. Nothing
        # leaks.
        return ReplayFailure(
            trace_id=trace_id,
            cohort=cohort,
            code="runner_timeout",
            message=(
                f"async runner exceeded {timeout_seconds}s timeout on "
                f"trace {trace_id!r}. The task was cancelled cleanly via "
                "asyncio.wait_for; runner cleanup ran via CancelledError."
            ),
            details={"timeout_seconds": float(timeout_seconds)},
        )
    except CacheMissError as exc:
        return ReplayFailure(
            trace_id=trace_id,
            cohort=cohort,
            code="tool_cache_miss",
            message=str(exc),
            details=exc.details_for_failure(),
        )
    except Exception as exc:
        # Mirrors the sync kernel's exception_failure builder.
        # Inlined here rather than importing the sync helper to
        # avoid coupling the async path to the sync module's
        # private API surface; the truncation logic is identical.
        raw_message = str(exc)
        truncated = (
            raw_message if len(raw_message) <= 2048 else raw_message[:2048] + "...(truncated)"
        )
        return ReplayFailure(
            trace_id=trace_id,
            cohort=cohort,
            code="runner_exception",
            message=(
                f"async runner raised {type(exc).__name__} on trace {trace_id!r}: {truncated}"
            ),
            details={
                "exception_type": type(exc).__name__,
                "message": truncated,
            },
        )
    else:
        return ReplaySuccess(trace_id=trace_id, cohort=cohort, output=output)


__all__ = ["replay_one_trace_async"]
