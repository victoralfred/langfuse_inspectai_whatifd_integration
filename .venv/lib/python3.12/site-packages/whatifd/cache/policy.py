"""Cache policy — Phase 3.4.

Resolves the user's `scorer_cache_mode` config setting into a concrete
mode the cache layer can act on, taking environment signals into
account. The fourth sub-phase of Phase 3 per the v0.1 implementation
plan; pairs with Phase 3.1/3.2/3.3 (keying, storage, lock).

## What this module does

The user config carries `scorer_cache_mode: ScorerCacheMode = "auto"`
by default (see `whatifd.types.policy.DecisionPolicy`). `auto` is an
under-specified input meaning "pick a sensible default from the
environment." This module's job is to resolve it.

Resolution rules per `references/phases.md` §3.4 +
`references/contracts.md` §"CI environment detection":

- Input is concrete (`on`/`off`/`read_only`/`refresh`): pass through
  unchanged. The user explicitly chose; we don't second-guess.
- Input is `auto` AND a CI env signal is present (`CI=true`,
  `GITHUB_ACTIONS=true`, etc.): resolve to `on` ("read AND write").
  Emit a `cache_mode_inferred` finding so the manifest discloses the
  inference — cardinal #1 (failures-as-data) extends to resolution
  decisions; the manifest should never imply the user picked a mode
  they didn't pick.
- Input is `auto` AND no CI signal: resolve to `auto` unchanged.
  Interactive default; the cache layer's `auto` semantics (read on
  hit, conservative write) take over.

## What this module does NOT do

- **Read or write cache entries.** Phase 3.2 storage is the I/O.
- **Acquire the cache lock.** Phase 3.3 lock is the single-writer
  primitive.
- **Build the `CacheSummary`.** Phase 3.5 will aggregate
  hits/misses/writes/etc. into the report-required field.
- **Validate `scorer_cache_warn_after_days` / `block_after_days`.**
  Those are policy-quality fields read by the eventual
  `cache_staleness_guard` (cascade-tracked, blocked on Phase 3
  completing).

## Cardinal alignment

- **#1 (failures-as-data):** mode inference is structured data — a
  `DecisionFinding` named `cache_mode_inferred` with
  `input_mode`/`resolved_mode`/`env_signal` details. Not a log line,
  not a printf.
- **#6 (typed boundaries):** `CachePolicyResolution` is a frozen
  dataclass; no `dict[str, Any]` crosses module boundaries.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from whatifd.decision.finding_codes import make_decision_finding
from whatifd.types.finding import DecisionFinding
from whatifd.types.policy import ScorerCacheMode

# Environment variables that signal a non-interactive CI runner.
# Listed conservatively per `references/contracts.md` ("CI=true,
# GITHUB_ACTIONS=true, etc."). The list is non-exhaustive by design —
# any of these being truthy flips the inference; absent all of them,
# resolution falls through to interactive defaults.
#
# Truthy convention notes:
# - CI / GITHUB_ACTIONS / GITLAB_CI / BUILDKITE follow a boolean
#   convention (`true` / `1` to indicate active; `false` / `0` to
#   opt out).
# - JENKINS_URL is intentionally different — Jenkins exports the
#   URL of the controller (e.g., `https://jenkins.example.com/`)
#   rather than a boolean. The truthy check below
#   (`value and value.lower() not in ("false", "0")`) accepts ANY
#   non-empty non-opt-out string, so the URL form works without a
#   per-var special case. The cost is that pathological values like
#   `JENKINS_URL=anything` would also flip; this is acceptable
#   because operators don't set CI env vars to misleading values
#   in practice, and the alternative (per-var format validation)
#   adds complexity without a real failure mode it prevents.
_CI_ENV_VARS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "JENKINS_URL")


@dataclass(frozen=True, slots=True)
class CachePolicyResolution:
    """The output of `resolve_cache_mode`.

    Carries the resolved mode AND any findings emitted during
    resolution. The caller (typically the verdict-projection layer in
    Phase 2.6+ once it gets wired) splices `findings` into the report's
    `decision_findings` list and uses `mode` to drive cache I/O.

    A frozen dataclass (not a tuple) so future extensions
    (`resolution_reason`, `env_signals_seen`) land as additional fields
    without breaking the call site.
    """

    mode: ScorerCacheMode
    findings: tuple[DecisionFinding, ...]


def resolve_cache_mode(
    config_mode: ScorerCacheMode,
    env: Mapping[str, str],
) -> CachePolicyResolution:
    """Resolve `config_mode` into a concrete mode, taking `env` into
    account.

    `env` is typically `os.environ` (or a test stub) — the function is
    pure with respect to the inputs, so tests can pass a tailored
    Mapping without monkeypatching `os.environ` globally.

    Returns a `CachePolicyResolution` with:
    - `mode`: the resolved `ScorerCacheMode`.
    - `findings`: zero or one `DecisionFinding` describing the
      inference. A finding is emitted ONLY when the input was `auto`
      AND the resolution flipped to a different concrete mode. Pure
      pass-through (concrete input, or `auto` → `auto`) emits no
      finding — the manifest already records the input config, so a
      "no inference happened" finding would be noise.
    """
    if config_mode != "auto":
        # User chose explicitly; pass through.
        return CachePolicyResolution(mode=config_mode, findings=())

    detected = _detected_ci_signal(env)
    if detected is None:
        # Interactive: no inference; `auto` stays `auto`.
        return CachePolicyResolution(mode="auto", findings=())

    finding = make_decision_finding(
        "cache_mode_inferred",
        message=(
            f"scorer cache mode resolved to 'on' from input 'auto' "
            f"based on environment signal {detected!r}"
        ),
        details={
            "input_mode": "auto",
            "resolved_mode": "on",
            "env_signal": detected,
        },
    )
    return CachePolicyResolution(mode="on", findings=(finding,))


def _detected_ci_signal(env: Mapping[str, str]) -> str | None:
    """Return the name of the first CI env var with a truthy value,
    or None if no signal is detected.

    Truthy = non-empty string AND not the literal `"false"`/`"0"`. The
    common pattern is `CI=true` (most CI systems set this), but some
    older runners set `CI=1`. We accept either; we reject `false`/`0`
    so a user could explicitly opt out via `CI=false` even if some
    other tooling set it.
    """
    for var in _CI_ENV_VARS:
        value = env.get(var)
        if value and value.lower() not in ("false", "0"):
            return var
    return None
