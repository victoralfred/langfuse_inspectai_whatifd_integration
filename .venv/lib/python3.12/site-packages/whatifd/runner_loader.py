"""Runner-target loader — parse `python:<module>:<attr>` and resolve.

Phase 10.2 of the v0.1 implementation plan. The CLI's
`_run_fork_pipeline` reads `cfg.target.runner` (a string of the
form `python:my_agent.replay:run`) and needs a callable that
satisfies the `Runner` or `AsyncRunner` protocol from
`whatifd.contract`.

## Design points

- **Top-level module, CLI-wiring scope.** The file lives at
  `src/whatifd/runner_loader.py` (top-level) rather than under a
  `whatifd/cli/` subpackage because the existing `whatifd/cli.py`
  module would collide with a `whatifd/cli/` package directory.
  Despite the placement, the loader is *consumed only by CLI
  fork wiring* (Phase 10.4's `_run_fork_pipeline`); no other
  whatifd core module imports it. Future v0.2 may add `module:`
  (no prefix) or other runner-reference families to this same
  loader. The placement is a pragmatic resolution of the
  cli/cli.py collision, not a public-API claim.
- **Sync/async classification at load time.** The replay kernel
  is split between `kernel.py` (sync, ThreadPoolExecutor) and
  `kernel_async.py` (async, asyncio.wait_for). Picking the right
  kernel needs to know which protocol the runner satisfies BEFORE
  the first trace flows through. The loader returns a tagged
  variant.
- **Errors are typed.** Cardinal #1: every expected failure is
  structured data, not a stack trace. `RunnerLoadError` carries
  an actionable message naming the bad reference, the import
  attempt that failed, and the next step.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from whatifd.contract import AsyncRunner, Runner

if TYPE_CHECKING:
    from collections.abc import Callable


class RunnerLoadError(Exception):
    """Raised when a runner-target reference cannot be resolved
    into a Runner-conforming callable.

    The message names the bad input verbatim plus the failure
    class (bad scheme / bad shape / module not importable /
    attribute missing / wrong protocol shape) so an operator
    sees exactly what to fix.
    """


@dataclass(frozen=True, slots=True)
class LoadedRunner:
    """Result of resolving `cfg.target.runner`.

    - `callable_` is the runner itself (renamed from `callable`
      to avoid shadowing the builtin).
    - `kind` tells the CLI which replay kernel to dispatch to.
    - `reference` is the original string for audit-log /
      RunManifest provenance.
    """

    callable_: Callable[..., object]
    kind: Literal["sync", "async"]
    reference: str


_PYTHON_PREFIX = "python:"


def load_runner(reference: str) -> LoadedRunner:
    """Resolve `cfg.target.runner` to a Runner / AsyncRunner.

    v0.1 accepts only the `python:<module.path>:<attr>` shape.
    Sync vs async is classified by `inspect.iscoroutinefunction`,
    NOT by Protocol `isinstance` alone. Both `Runner` and
    `AsyncRunner` are `runtime_checkable` and only verify
    attribute presence (`__call__`); a plain `async def` function
    satisfies both structurally. The async branch is checked first
    so an `async def` runner doesn't accidentally route to the sync
    kernel (which would treat the returned coroutine as a
    `ReplayOutput`). Protocol `isinstance` runs as a
    belt-and-suspenders check inside each branch so a future
    Protocol-shape extension catches regressions at load time.

    Returns a `LoadedRunner` carrying the callable, the kind,
    and the original reference string.
    """
    if not isinstance(reference, str) or not reference:
        raise RunnerLoadError(f"target.runner must be a non-empty string; got {reference!r}.")
    if not reference.startswith(_PYTHON_PREFIX):
        raise RunnerLoadError(
            f"target.runner {reference!r} has unsupported scheme. v0.1 supports "
            "`python:<module.path>:<attr>` only."
        )

    body = reference[len(_PYTHON_PREFIX) :]
    if body.count(":") != 1:
        raise RunnerLoadError(
            f"target.runner {reference!r} is malformed. Expected exactly one "
            "`:` separator after the `python:` prefix; got "
            f"{body.count(':')} additional separator(s). Format: "
            "`python:<module.path>:<attr>`."
        )

    module_path, _, attr = body.partition(":")
    if not module_path:
        raise RunnerLoadError(
            f"target.runner {reference!r} is missing the module path. Format: "
            "`python:<module.path>:<attr>`."
        )
    if not attr:
        raise RunnerLoadError(
            f"target.runner {reference!r} is missing the attribute name. "
            "Format: `python:<module.path>:<attr>`."
        )

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise RunnerLoadError(
            f"target.runner {reference!r}: module {module_path!r} could not be "
            f"imported ({exc}). Check the module path and that the package is "
            "installed in the current environment."
        ) from exc

    if not hasattr(module, attr):
        raise RunnerLoadError(
            f"target.runner {reference!r}: module {module_path!r} has no "
            f"attribute {attr!r}. Check the attribute name and the module's "
            "exports."
        )

    candidate = getattr(module, attr)

    # Cardinal sync/async classification.
    #
    # `Runner` and `AsyncRunner` are both `runtime_checkable`
    # Protocols, but `runtime_checkable` only verifies attribute
    # PRESENCE — not callable signature, not return type. A plain
    # function satisfies both Protocols structurally because both
    # declare `__call__`. We can't classify off `isinstance`
    # alone; we have to inspect whether the callable is an
    # `async def` (or a callable whose `__call__` is an
    # `async def`). `inspect.iscoroutinefunction` is the canonical
    # check and handles `functools.wraps`-decorated wrappers
    # correctly via `inspect.unwrap`.
    #
    # Validation order:
    # 1. Reject non-callables (an `int`, a class without
    #    `__call__`, etc.) with the protocol-shape error.
    # 2. Classify sync vs async by `iscoroutinefunction`.
    if not callable(candidate):
        raise RunnerLoadError(
            f"target.runner {reference!r}: resolved object {candidate!r} is "
            "not callable. Expected a function with signature "
            "`(trace_input, config, tool_cache) -> ReplayOutput`. See "
            "docs/runner-contract.md."
        )

    # `iscoroutinefunction` returns True for `async def` and for
    # objects whose `__call__` is an `async def` (after
    # `inspect.unwrap`-ing decorators). That matches the runtime
    # semantic the replay kernels rely on.
    # B004 false-positive escape: we're not testing callability
    # here (the `callable(candidate)` guard above already did
    # that). We need the bound `__call__` METHOD object so
    # `iscoroutinefunction` can inspect class-instance runners
    # whose `__call__` is `async def`. `getattr(..., None)` returns
    # None for builtins/functions where the attribute lookup
    # would still pass the callable() check; iscoroutinefunction
    # returns False on None, which is exactly the no-op we want.
    call_attr = getattr(candidate, "__call__", None)  # noqa: B004
    if inspect.iscoroutinefunction(candidate) or inspect.iscoroutinefunction(call_attr):
        # Belt-and-suspenders Protocol satisfaction check: if a
        # future contributor adds an extra attribute requirement
        # to AsyncRunner, this catches the regression at load
        # time rather than mid-replay.
        if not isinstance(candidate, AsyncRunner):
            raise RunnerLoadError(
                f"target.runner {reference!r}: async callable does not satisfy "
                "the AsyncRunner protocol. See docs/runner-contract.md."
            )
        return LoadedRunner(callable_=candidate, kind="async", reference=reference)

    if not isinstance(candidate, Runner):
        raise RunnerLoadError(
            f"target.runner {reference!r}: sync callable does not satisfy the "
            "Runner protocol. See docs/runner-contract.md."
        )
    return LoadedRunner(callable_=candidate, kind="sync", reference=reference)


__all__ = ["LoadedRunner", "RunnerLoadError", "load_runner"]
