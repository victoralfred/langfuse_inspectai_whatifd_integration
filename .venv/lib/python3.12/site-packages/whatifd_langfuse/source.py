"""`LangfuseTraceSource` — implementation of `whatifd.adapters.TraceSource`.

Constructor takes a Langfuse API client (typed via `Protocol` so
tests can substitute a mock without pulling the langfuse module);
`iter_traces` paginates through `api.trace.list(...)` and projects
each `Trace` into a `RawTrace` with content fields wrapped as
`Sensitive[str]`. The runtime contract follows
`whatifd.adapters.protocols.TraceSource`; the conformance harness
at `tests/adapters/conformance.py` is the gating test.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from whatifd.adapters.protocols import (
    AdapterMetadata,
    RawTrace,
)
from whatifd.serialization.canonical import canonical_json_bytes
from whatifd.types.sensitive import Sensitive
from whatifd.types.statistical import ClusterKeySupport

_log = logging.getLogger(__name__)

ADAPTER_ID = "langfuse"


@runtime_checkable
class _TraceLike(Protocol):
    """Structural shape this adapter needs from a Langfuse `Trace`.

    Defined as a Protocol so tests can hand a mock object without
    importing langfuse, AND so any future Langfuse SDK that renames
    or restructures fields can be adapted with a small wrapper
    rather than a rewrite. Mirrors the field set the Langfuse v4
    `Trace` model exposes (`langfuse.api.commons.types.trace.Trace`).
    """

    id: str
    input: Any
    output: Any
    metadata: Any
    tags: Any
    user_id: Any
    session_id: Any


@runtime_checkable
class _TracesResponseLike(Protocol):
    data: list[Any]


@runtime_checkable
class _TraceClientLike(Protocol):
    def list(
        self,
        *,
        page: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> _TracesResponseLike: ...


@runtime_checkable
class _LangfuseAPILike(Protocol):
    trace: _TraceClientLike


def _stringify(value: Any) -> str:
    """Project a Langfuse `input` / `output` field (typed Any —
    str | dict | list | None per the SDK) into a canonical string.

    `None` and empty values render as the empty string so downstream
    `Sensitive[str]` wrapping always succeeds with a real `str`
    value. Dicts/lists round-trip through `canonical_json_bytes` so
    the same trace always projects to the same string (cardinal #4
    determinism for the projection step; determinism beyond that is
    the report layer's responsibility).

    Routes through `whatifd.serialization.canonical.canonical_json_bytes`
    rather than calling `json.dumps` directly. The project's banned-
    import discipline keeps `json.dumps` inside `whatifd/serialization/`
    so all canonical encoding goes through a single review surface
    (one layer of cardinal #5's three-layer defense — the encoder
    rejects unwrapped `Sensitive[T]` values).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    # `canonical_json_bytes` returns ASCII bytes; decode to str for
    # the `Sensitive[str]` wrap downstream. The encoding is stable
    # across platforms (sort_keys=True + ensure_ascii=True per the
    # canonical helper). Explicit `str(...)` so mypy knows the
    # decoded value matches the declared return type without
    # leaking `Any` from the canonical helper's signature.
    decoded: str = canonical_json_bytes(value).decode("ascii")
    return decoded


@dataclass(frozen=True, slots=True)
class LangfuseTraceSource:
    """Langfuse `TraceSource` adapter.

    Construct with a `LangfuseAPI`-shaped client (anything matching
    the `_LangfuseAPILike` Protocol — typically
    `langfuse.api.LangfuseAPI`) plus a `cohort_classifier` callback
    that maps a Langfuse `Trace` to a cohort name (`"failure"` or
    `"baseline"` for v0.1's failure-rescue scope).

    `page_limit` controls the per-page Langfuse fetch; the iterator
    keeps calling `api.trace.list(page=N, limit=page_limit)` until
    a page returns fewer rows than `page_limit` (the standard
    "exhausted" signal in Langfuse's pagination shape). `max_traces`
    caps the total iteration so a fixture run can't accidentally
    drain a production project.
    """

    api: _LangfuseAPILike
    cohort_classifier: Callable[[_TraceLike], str]
    page_limit: int = 50
    max_traces: int | None = None
    # `Mapping[str, Any]` (not `dict[str, Any]`) signals read-only
    # intent at the constructor boundary — the adapter never mutates
    # what the caller passed in. Default is a frozen empty mapping
    # via `MappingProxyType`; constructed-with-dict callers stay
    # compatible because `dict` IS a `Mapping`. Cardinal #6 spirit:
    # `dict[str, Any]` crossing a public constructor reads as a
    # write surface, which this isn't.
    list_kwargs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    sdk_version: str | None = None

    def iter_traces(self) -> Iterator[RawTrace]:
        emitted = 0
        page = 1
        while True:
            response = self.api.trace.list(
                page=page,
                limit=self.page_limit,
                **self.list_kwargs,
            )
            page_data = list(response.data)
            for trace in page_data:
                if self.max_traces is not None and emitted >= self.max_traces:
                    return
                yield self._project(trace)
                emitted += 1
            if len(page_data) < self.page_limit:
                return
            page += 1

    def adapter_metadata(self) -> AdapterMetadata:
        from whatifd_langfuse import __version__ as package_version

        return AdapterMetadata(
            adapter_id=ADAPTER_ID,
            package_version=package_version,
            sdk_version=self.sdk_version or _resolve_sdk_version(),
        )

    def cluster_key_support(self) -> ClusterKeySupport:
        # v0.1: empty. Langfuse traces carry `user_id` and
        # `session_id`, but using either as a cluster key would be
        # an unannounced inferential commitment (cardinal #10
        # forbids fabricating cluster keys for confirmatory
        # verdicts). v0.2+ surfaces explicit per-field opt-in via
        # `LangfuseTraceSource(cluster_keys=("session_id",))` or
        # similar; for now the source declares no clusters and the
        # methodology disclosure reflects that honestly.
        return ClusterKeySupport(available_keys=())

    def _project(self, trace: _TraceLike) -> RawTrace:
        # Sensitive-wrap boundary (cardinal #5): `input` and `output`
        # ARE user content and get wrapped. `metadata` is NOT
        # wrapped because Langfuse's `metadata` field carries
        # tooling state (path, latency_ms, status_code, sdk version,
        # resource attributes) — not user content. The `RawTrace`
        # protocol matches this contract: `user_message` and
        # `original_response` are typed `Sensitive[str]`; `metadata`
        # is `Mapping[str, Any]` precisely because it's tooling
        # state, not Sensitive payload.
        #
        # **If your Langfuse setup puts user content in metadata**
        # (e.g., embedding `user_id` or partial transcripts there),
        # subclass `LangfuseTraceSource` and override `_project` to
        # filter or wrap the relevant keys. Surfacing user content
        # through unwrapped metadata is a deployment misconfiguration
        # this adapter does not catch — it follows the documented
        # Langfuse semantics.
        return RawTrace(
            trace_id=trace.id,
            cohort=self.cohort_classifier(trace),
            user_message=Sensitive(_stringify(trace.input), classification="user_content"),
            original_response=Sensitive(_stringify(trace.output), classification="user_content"),
            metadata=dict(trace.metadata or {}),
        )


def _resolve_sdk_version() -> str | None:
    """Read `langfuse.__version__` lazily so importing this module
    doesn't drag the Langfuse SDK in until it's actually needed
    (e.g., a test that constructs `LangfuseTraceSource` with a
    mock client never imports langfuse). Returns None if the SDK
    isn't installed — the resulting `AdapterMetadata.sdk_version`
    is None, which the manifest accepts."""
    try:
        import langfuse

        version = getattr(langfuse, "__version__", None)
        if version is None:
            _log.debug(
                "langfuse SDK imported but exposes no __version__ attribute; "
                "AdapterMetadata.sdk_version will be None"
            )
        return version
    except ImportError:
        _log.debug(
            "langfuse SDK not installed; AdapterMetadata.sdk_version "
            "will be None. Install via `pip install whatifd-langfuse` "
            "(which pulls langfuse as a hard dep) or directly with "
            "`pip install langfuse`."
        )
        return None
