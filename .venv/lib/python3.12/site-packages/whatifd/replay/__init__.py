"""`whatifd.replay` — pipeline for fork → replay → score.

Phase 6 of the v0.1 implementation plan. The pipeline streams traces
through three stages:

  trace ingestion (adapter)
    → replay (user runner via `whatifd.contract`)
    → score (scorer adapter)
    → ScoreCase | FailureRecord

`whatifd.replay.result` (Phase 6.1, this delivery) carries the typed
result of the replay stage — `ReplaySuccess | ReplayFailure`. The
`pipeline` (Phase 6.3) consumes these and either hands the success
on to scoring or projects the failure to a `FailureRecord` for the
report.

The early delivery of `result.py` lets Phase 6.2 (`tool_cache.py`)
raise its `CacheMissError` and have a typed shape to convert into
without forward references.
"""

from whatifd.replay.kernel import replay_one_trace
from whatifd.replay.kernel_async import replay_one_trace_async
from whatifd.replay.pipeline import ReplayInputBundle, replay_stream
from whatifd.replay.result import ReplayFailure, ReplayResult, ReplaySuccess

__all__ = [
    "ReplayFailure",
    "ReplayInputBundle",
    "ReplayResult",
    "ReplaySuccess",
    "replay_one_trace",
    "replay_one_trace_async",
    "replay_stream",
]
