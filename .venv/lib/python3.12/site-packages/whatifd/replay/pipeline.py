"""`replay_stream` — bounded-concurrency streaming wrapper over the
kernel.

Phase 6.3b of the v0.1 implementation plan. The streaming pipeline
takes an iterable of `ReplayInputBundle` and yields `ReplayResult`
values via `replay_one_trace` (Phase 6.3a) under a sliding-window
`ThreadPoolExecutor`. Properties:

- **Bounded memory:** at most `max_workers` bundles are in flight
  at any time. The input iterable is consumed lazily — large
  batches don't load into memory.
- **Streaming yield:** results are yielded as they complete (any
  order). The caller can process them incrementally.
- **No order guarantee:** completions are in arrival order, NOT
  input order. The report aggregator (Phase 2.7 / Phase 9) sorts
  the failures and cohort_results by trace_id; the pipeline
  doesn't need to preserve order to satisfy the report contract.

## Why a sliding window, not gather-all-then-yield

A naive `[ex.submit(...) for b in bundles]` eagerly consumes the
input iterable, defeating streaming. For a 10k-trace fixture this
would queue all 10k tasks before yielding any result. The sliding
window keeps `max_workers` in flight: prime with N submits, yield
each completion, submit one more. Memory is O(max_workers), not
O(len(bundles)).

## Concurrency interaction with the kernel's per-call executor

The kernel (`replay_one_trace`) uses its OWN
`ThreadPoolExecutor(max_workers=1)` per call for timeout
enforcement. The streaming layer's outer executor submits kernel
CALLS, not runner calls — each kernel call is a single Python
function returning a `ReplayResult`. The outer pool's worker thread
runs the kernel; the kernel internally spawns ANOTHER worker thread
for the runner. Peak threads = 2 * max_workers (one outer + one
inner per concurrent kernel).

The kernel returns synchronously even on timeout (it
`shutdown(wait=False)` the inner executor and detaches the leaked
runner thread). So the streaming layer's `shutdown(wait=True)` at
end of `with` only waits for kernel returns, NOT for leaked runner
threads. The cascade catalog entry "Per-trace ThreadPoolExecutor +
leaked-thread-on-timeout pattern" warned against an outer
`wait=True` that would serialize timeouts; this layer is safe
because the kernel's timeout return is fast (it doesn't block on
the leak).

## Cardinal alignment

- **#1 failures-as-data:** the kernel is the failure-conversion
  boundary. The streaming layer just transports kernel results;
  it never raises for runner-related conditions. A bug in the
  kernel itself would propagate, which is correct (cardinal #1
  covers expected failures, not whatifd-internal bugs).
- **#9 orchestration not compute:** bounded thread pool, no CPU
  optimization, no shared-memory tricks. The pool exists to
  parallelize I/O-bound runner calls (LLM API requests).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING

from whatifd.replay.kernel import replay_one_trace
from whatifd.replay.result import ReplayResult

if TYPE_CHECKING:
    from whatifd.contract import ReplayConfig, Runner, ToolCache, TraceInput


@dataclass(frozen=True, slots=True)
class ReplayInputBundle:
    """One input record for the streaming pipeline.

    Carries everything `replay_one_trace` needs to process a single
    trace. The pipeline takes an `Iterable[ReplayInputBundle]`, so
    the adapter (Phase 4) builds a generator that yields these from
    the underlying trace stream.

    Frozen + slotted; no logic, just a data carrier. The trace_id
    plus cohort identify the trace within the run; the rest is the
    runner-call surface (`trace_input`, `config`, `tool_cache`,
    `runner`).

    ## Sensitive-data discipline (cardinal #5)

    `trace_input.user_message` (and any `metadata` keys) carry raw
    user content from the production trace. The ADAPTER (Phase 4)
    is responsible for wrapping any sensitive fields as
    `Sensitive[T]` BEFORE constructing the bundle. The bundle
    itself does not enforce or re-wrap; it relies on the adapter
    boundary. The pre-serialization graph walk
    (`assert_no_unredacted_sensitive`, Phase 5.4) is the runtime
    safety net that catches any unwrapped `Sensitive[T]` reaching
    the artifact-write path.
    """

    trace_id: str
    cohort: str
    trace_input: TraceInput
    config: ReplayConfig
    tool_cache: ToolCache
    runner: Runner


def replay_stream(
    bundles: Iterable[ReplayInputBundle],
    *,
    max_workers: int = 4,
    timeout_seconds: float = 60.0,
) -> Iterator[ReplayResult]:
    """Yield `ReplayResult` values for each input bundle.

    Bounded concurrency: at most `max_workers` kernel calls in
    flight at any time. The input iterable is consumed lazily.
    Results are yielded in completion order (NOT input order).

    Default `max_workers=4` is conservative. v0.1 use cases
    (~40-trace failure-rescue runs) don't benefit from aggressive
    parallelism; LLM judge-cache hits dominate. Callers with
    large batches and low-latency endpoints can tune up.

    `timeout_seconds` is forwarded to each kernel call. v0.1
    applies the same timeout to all bundles in the stream;
    per-bundle timeouts can be added by extending
    `ReplayInputBundle`.
    """
    if max_workers < 1:
        raise ValueError(
            f"replay_stream: max_workers must be >= 1; got {max_workers}. "
            "Concurrency below 1 is meaningless; the kernel is synchronous."
        )

    bundles_iter = iter(bundles)

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="whatifd-stream",
    ) as ex:
        # Prime: submit up to max_workers initial bundles. The
        # sliding-window invariant "at most N in flight" starts here.
        in_flight = {
            ex.submit(_call_kernel, bundle, timeout_seconds)
            for bundle in itertools.islice(bundles_iter, max_workers)
        }

        while in_flight:
            # Wait for at least one to complete. `FIRST_COMPLETED`
            # returns as soon as any future finishes — keeps the
            # window full when one completes much faster than others.
            done, _pending = wait(in_flight, return_when=FIRST_COMPLETED)

            for future in done:
                in_flight.discard(future)
                yield future.result()
                # Slide the window: pull the next bundle (if any)
                # and submit. The `try/except StopIteration` cleanly
                # drains: once the input iterator is exhausted, the
                # window shrinks each iteration until empty.
                try:
                    next_bundle = next(bundles_iter)
                except StopIteration:
                    continue
                in_flight.add(ex.submit(_call_kernel, next_bundle, timeout_seconds))


def _call_kernel(bundle: ReplayInputBundle, timeout_seconds: float) -> ReplayResult:
    """Trampoline from the streaming layer's executor into the
    kernel's per-call executor.

    Kept as a free function (not a lambda) so the closure shape
    is explicit and stack traces name `_call_kernel` rather than
    `<lambda>` for easier debugging. The double-executor pattern
    is documented at module top.
    """
    return replay_one_trace(
        trace_id=bundle.trace_id,
        cohort=bundle.cohort,
        trace_input=bundle.trace_input,
        config=bundle.config,
        tool_cache=bundle.tool_cache,
        runner=bundle.runner,
        timeout_seconds=timeout_seconds,
    )


__all__ = ["ReplayInputBundle", "replay_stream"]
