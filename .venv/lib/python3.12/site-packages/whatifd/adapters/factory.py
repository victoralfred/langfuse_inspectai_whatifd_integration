"""Adapter factory — config name → adapter instance.

Phase 10 CLI wiring foundation. The CLI's `_run_fork_pipeline`
needs to construct a concrete `TraceSource` and `Scorer` from the
adapter names in the validated `WhatifConfig`. This module owns
that dispatch.

## Why a registry, not a switch in cli.py

Three reasons:

1. **Lazy import.** Real adapter packages (`whatifd_langfuse`,
   `whatifd_inspect_ai`) MUST NOT be imported by core code paths
   (cardinal-enforced lazy-load contract pinned by
   `tests/unit/whatifd/adapters/test_protocols.py::
   test_core_modules_do_not_load_real_adapter_packages`). The
   factory imports them inside the dispatch function so `import
   whatifd.cli` doesn't drag them in transitively.
2. **Test isolation.** Wiring tests instantiate the stub adapter
   without spinning up a real Langfuse client. The factory's
   stub branch is the contract surface for that.
3. **Single source of truth.** Adapter name strings appear in the
   user's YAML, the config schema, the cascade catalog, and the
   wiring layer. Centralizing the dispatch here means adding a
   new adapter is one line + one branch + one test, not a
   project-wide grep.

## Why credentials read from the environment, not config

Langfuse credentials are secrets. v0.1's `WhatifConfig` is a
plain YAML/JSON file commonly checked into source control or PR
templates. Reading `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` /
`LANGFUSE_SECRET_KEY` from the environment matches the Langfuse
SDK's own convention and keeps secrets out of repo files. A
follow-up may add a config-block override for non-default hosts,
but the secret material stays in env.

## Failure surface

Construction failures raise `AdapterFactoryError`. The CLI
catches this at the dispatcher and exits with the setup-failure
code (cardinal #1: setup failures are structured signals to the
operator, not stack traces).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from whatifd.adapters.stub import StubScorer, StubTraceSource

if TYPE_CHECKING:
    from whatifd.adapters.protocols import Scorer, TraceSource
    from whatifd.config import ScorerConfig, SourceConfig


class AdapterFactoryError(Exception):
    """Raised when an adapter cannot be constructed.

    Carries an actionable message naming the adapter, the missing
    requirement, and the env var or config field that would fix
    it. The CLI converts the message into stderr output with the
    setup-failure exit code.
    """


_LANGFUSE_HOST_ENV_KEYS = ("LANGFUSE_HOST", "LANGFUSE_BASE_URL")
_LANGFUSE_CRED_ENV_KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")


def build_trace_source(cfg: SourceConfig) -> TraceSource:
    """Construct a `TraceSource` from `cfg.source`.

    Supported adapters in v0.1:
    - `"stub"` — synthetic in-memory source. Useful for end-to-end
      CLI tests and for the v0.1 release smoke. Yields zero traces
      (callers that need fixture data should construct
      `StubTraceSource(specs=...)` directly, not via this
      factory).
    - `"langfuse"` — real Langfuse adapter. Reads
      `LANGFUSE_HOST` (or `LANGFUSE_BASE_URL`) and
      `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` from the
      environment. Constructs the Langfuse SDK client lazily.
    """
    name = cfg.adapter
    if name == "stub":
        return StubTraceSource(specs=[])
    if name == "langfuse":
        return _build_langfuse_source()
    raise AdapterFactoryError(
        f"Unknown trace-source adapter {name!r}. v0.1 supports 'stub' and 'langfuse'."
    )


def build_scorer(cfg: ScorerConfig) -> Scorer:
    """Construct a `Scorer` from `cfg.scorer`.

    Supported adapters in v0.1:
    - `"stub"` — synthetic scorer whose default `score_fn` returns
      the constant `0.5` for every case (NOT zero, NOT "no
      judgment"). Useful for end-to-end CLI wiring tests;
      production runs MUST use a real scorer or every trace will
      appear to improve uniformly. Pinned by
      `test_build_scorer_stub_default_score_fn_returns_constant_0_5`.
    - `"inspect_ai"` — real Inspect AI adapter. Requires the
      caller to wire a `score_fn`; v0.1 does NOT load `score_fn`
      from config (it's user code, not config data). The CLI
      surfaces this with an actionable error pointing at the
      programmatic `run_pipeline` path; full config-loaded
      score_fn is a v0.2 surface (cascade-catalog "Scorer
      score_fn config-loadable").
    """
    name = cfg.adapter
    if name == "stub":
        # `StubScorer.score_fn` defaults to `_default_score_fn`,
        # which returns the constant `0.5` for every case (see
        # `whatifd.adapters.stub`). That's the right CLI smoke
        # default — non-None (so cardinal #1 None-failure paths
        # don't fire) and constant (so deltas are deterministic).
        # The pipeline interprets a constant 0.5 as "above
        # epsilon improvement on every trace," producing a Ship
        # verdict against an empty stub source's degenerate
        # zero-cohort case after floor-failure intercepts. **Do
        # NOT use `scorer.adapter='stub'` for a real run** — every
        # trace will appear to improve identically and the
        # verdict will be misleading.
        return StubScorer()
    if name == "inspect_ai":
        # v0.2: config-loaded score_fn closes the v0.1 setup-failure
        # cliff. Required fields are enforced by ScorerConfig's
        # model_validator before we get here, so cfg.score_fn etc.
        # are guaranteed non-None — but keep an assertion-style check
        # so a future refactor that drops the validator surfaces
        # immediately instead of producing an obscure InspectAIScorer
        # constructor error.
        if cfg.score_fn is None:
            raise AdapterFactoryError(
                "scorer.adapter='inspect_ai' requires scorer.score_fn (a "
                "`python:<module.path>:<attr>` reference). The config-validation "
                "layer normally catches this before factory dispatch; reaching "
                "this branch means the validator was bypassed."
            )
        from whatifd.scorer_loader import ScorerLoadError, load_score_fn

        try:
            score_fn = load_score_fn(cfg.score_fn)
        except ScorerLoadError as exc:
            raise AdapterFactoryError(str(exc)) from exc

        # Lazy import — the inspect_ai package is an optional adapter
        # extra. Importing at module top-level would violate the
        # core-modules-do-not-load-real-adapter-packages contract.
        # Cardinal #1: a missing optional package surfaces as a typed
        # AdapterFactoryError with an actionable install hint, never
        # a raw ImportError stack trace.
        try:
            from whatifd_inspect_ai import InspectAIScorer
        except ImportError as exc:
            raise AdapterFactoryError(
                "scorer.adapter='inspect_ai' requires the optional "
                "`whatifd-inspect-ai` package. Install with: "
                "`pip install whatifd-inspect-ai` (or `uv pip install whatifd-inspect-ai`)."
            ) from exc

        # Belt-and-suspenders runtime guards. The ScorerConfig
        # model_validator catches missing fields at config-load
        # time, but a caller using `model_construct` (Pydantic's
        # explicit validation-bypass escape hatch) can reach this
        # point with None. Cardinal #1: surface as a typed
        # AdapterFactoryError naming the missing field, not an
        # AssertionError. Each field is checked individually so the
        # error message is actionable.
        for field_name in ("judge_provider", "judge_model_id", "rubric_id", "rubric_text"):
            if getattr(cfg, field_name) is None:
                raise AdapterFactoryError(
                    f"scorer.adapter='inspect_ai' requires scorer.{field_name}. "
                    "The config-validation layer normally catches this before "
                    "factory dispatch; reaching this branch means the validator "
                    "was bypassed (e.g., via ScorerConfig.model_construct)."
                )

        scorer: Scorer = InspectAIScorer(
            score_fn=score_fn,
            judge_provider=cfg.judge_provider,
            judge_model_id=cfg.judge_model_id,
            judge_model_snapshot=cfg.judge_model_snapshot,
            rubric_id=cfg.rubric_id,
            rubric_text=cfg.rubric_text,
            scoring_parameters=cfg.scoring_parameters,
        )
        return scorer
    raise AdapterFactoryError(
        f"Unknown scorer adapter {name!r}. Supported in v0.2: 'stub' and "
        "'inspect_ai' (the latter requires the optional `whatifd-inspect-ai` "
        "package and the score_fn / judge_provider / judge_model_id / "
        "rubric_id / rubric_text fields)."
    )


def _build_langfuse_source() -> TraceSource:
    host = next((os.environ.get(k) for k in _LANGFUSE_HOST_ENV_KEYS if os.environ.get(k)), None)
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    if not host or not public_key or not secret_key:
        missing = []
        if not host:
            missing.append("LANGFUSE_HOST (or LANGFUSE_BASE_URL)")
        if not public_key:
            missing.append("LANGFUSE_PUBLIC_KEY")
        if not secret_key:
            missing.append("LANGFUSE_SECRET_KEY")
        raise AdapterFactoryError(
            "langfuse adapter requires environment credentials; missing: "
            + ", ".join(missing)
            + ". Set these env vars before running `whatifd fork`, or "
            "switch source.adapter to 'stub' for a credentialless smoke."
        )

    # Lazy import — keeps the cardinal-enforced lazy-load contract
    # (test_core_modules_do_not_load_real_adapter_packages). This
    # function only runs at CLI fork-time, never at module import.
    try:
        from langfuse import Langfuse  # type: ignore[import-not-found,unused-ignore]
        from whatifd_langfuse import (  # type: ignore[import-untyped,unused-ignore]
            LangfuseTraceSource,
        )
    except ImportError as exc:
        raise AdapterFactoryError(
            f"langfuse adapter import failed: {exc}. Install the package "
            "with `pip install whatifd-langfuse`."
        ) from exc

    # Wrap the Langfuse client construction in the same boundary
    # catch as the import: a malformed host, an auth handshake
    # failure, or any other constructor-time exception from the
    # SDK MUST surface as `AdapterFactoryError` for the CLI to
    # convert to setup-failure exit code (cardinal #1: every
    # expected failure is structured data, not a leaked stack
    # trace). Without this catch, a typo in `LANGFUSE_HOST` could
    # propagate as a raw `ValueError` past the CLI's
    # `AdapterFactoryError` handler.
    try:
        client = Langfuse(host=host, public_key=public_key, secret_key=secret_key)
        source: TraceSource = LangfuseTraceSource(
            api=client.api,
            # Default cohort classifier: tags-based, mirroring
            # the whatifd-langfuse README. v0.2 adds config-driven
            # classifier selection (see cascade-catalog).
            cohort_classifier=lambda t: (
                "failure" if "failure" in (getattr(t, "tags", None) or []) else "baseline"
            ),
        )
    except AdapterFactoryError:
        # Already typed; let it propagate without re-wrapping.
        raise
    except Exception as exc:
        raise AdapterFactoryError(
            f"langfuse adapter construction failed: {type(exc).__name__}: {exc}. "
            "Check LANGFUSE_HOST format and credential validity."
        ) from exc
    return source


__all__ = [
    "AdapterFactoryError",
    "build_scorer",
    "build_trace_source",
]
