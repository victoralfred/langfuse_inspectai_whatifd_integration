"""`whatifd` CLI entry point.

Phase 8.2 of the v0.1 implementation plan. Typer-based command
surface:

  - `whatifd fork [--config PATH] [--profile {default|review|minimal|forensic}]`
    — main entrypoint. Loads config, runs two-affirmation,
    threads `TwoAffirmationProof` to the (Phase 4 / Phase 9)
    adapter pipeline. v0.1 8.2 ships the CLI SHELL — argument
    parsing, config load, two-affirmation, exit-code dispatch.
    The actual fork execution is gated on Phase 4 adapter
    integration; missing adapter → exit 2 with a clear setup
    message, NOT a silent fallback.
  - `whatifd report-migrate <report.json>` — v0.1.x no-op (no
    schema breaks within v0.1.x); real migration logic when v0.2
    ships
  - `whatifd cache rebuild|unlock|verify` — full Phase 8.3 implementations
  - `whatifd diff <prev.json> <new.json>` — full Phase 8.4 diff renderer

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Ship verdict |
| `1` | Don't Ship verdict |
| `2` | Inconclusive verdict, setup failure, floor violation, OR config error |

Floor violations ALWAYS produce exit 2 regardless of policy
(cardinal #2). Setup failures (missing config, validation
errors, missing forensic affirmation) also produce exit 2
because they prevent producing a verdict at all.

## Two-affirmation invocation point

`assert_two_affirmation` is called IMMEDIATELY after
`load_config` returns and BEFORE any forensic-path code runs.
The returned `TwoAffirmationProof` is threaded to downstream
code that consumes the redaction profile (Phase 8.5+ /
Phase 4 adapter). Cascade-catalog entry "CLI must enforce
two-affirmation before forensic-path code" tracks this.

## Why a thin CLI shell now

The downstream pipeline (replay → score → decision → render)
still has open dependencies on Phase 4 (adapters) and Phase 9
(integration). Shipping the CLI shell now gets the CLI surface,
exit-code semantics, and config-load flow in place; the missing
adapter integration surfaces as a typed exit-2 setup failure,
NOT as a runtime crash that bypasses cardinal #1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from whatifd.cache import DEFAULT_CACHE_ROOT
from whatifd.cache.recovery import rebuild, unlock, verify
from whatifd.config import (
    ConfigFileError,
    ForensicAffirmationError,
    TwoAffirmationProof,
    WhatifConfig,
    assert_two_affirmation,
    format_validation_errors,
    load_config,
)
from whatifd.diff import (
    DiffError,
    compute_diff,
    load_report,
    render_diff_markdown,
)
from whatifd.statistical import (
    BOOTSTRAP_CI_LEVEL_DECIMAL,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
)

# Default config-file path. Operators override via `--config`.
_DEFAULT_CONFIG_PATH = Path("whatifd.config.yaml")

# Default cache root is imported from the package's single source
# of truth (`whatifd.cache.DEFAULT_CACHE_ROOT`) so a future change
# in the storage-layer canonical path propagates here automatically.

# Exit codes per the cardinal-#2 / phase-8 contract.
#
# Semantics:
#   0 - command succeeded. For `whatifd fork` specifically, 0
#       means a Ship verdict (the alias `EXIT_SHIP` documents
#       that meaning at the call site). For commands that do not
#       produce a verdict (`report-migrate` no-op, future
#       `cache verify` clean-pass), 0 means "command did its
#       job"; use the `EXIT_SUCCESS` alias for clarity.
#   1 - Don't Ship verdict (fork only).
#   2 - Inconclusive verdict / setup failure / floor violation.
#       Floor violations always produce 2 regardless of policy.
EXIT_SUCCESS = 0
EXIT_SHIP = 0  # alias: fork-specific semantic name for exit 0
EXIT_DONT_SHIP = 1
EXIT_INCONCLUSIVE_OR_SETUP_FAILURE = 2

app = typer.Typer(
    name="whatifd",
    help=(
        "whatifd: trust-first experiment runner for LLM behavior changes. "
        "Fork production traces, replay with a proposed change, score the "
        "diff, emit a defensible Ship / Don't Ship / Inconclusive verdict."
    ),
    no_args_is_help=True,
    add_completion=False,
)


# ---------------------------------------------------------------------------
# `whatifd fork` — main entry
# ---------------------------------------------------------------------------


@app.command()
def fork(
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            "-c",
            help="Path to the whatifd config file (.yaml/.yml/.json).",
        ),
    ] = _DEFAULT_CONFIG_PATH,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            "-p",
            help=(
                "Reporting profile override; must match the config's "
                "reporting.profile. `forensic` requires the "
                "forensic_acknowledgment block per cardinal #7."
            ),
        ),
    ] = None,
) -> None:
    """Fork production traces, replay with the proposed change, emit
    a verdict. Exit code 0 = Ship, 1 = Don't Ship, 2 = Inconclusive
    or setup failure.
    """
    try:
        cfg = load_config(config)
    except ConfigFileError as exc:
        typer.echo(f"whatifd: config error: {exc}", err=True)
        raise typer.Exit(code=EXIT_INCONCLUSIVE_OR_SETUP_FAILURE) from exc
    except ValidationError as exc:
        typer.echo(format_validation_errors(exc), err=True)
        raise typer.Exit(code=EXIT_INCONCLUSIVE_OR_SETUP_FAILURE) from exc

    # TODO(cardinal #7): the cascade-catalog entry "CLI must enforce
    # two-affirmation before forensic-path code" pins this call as
    # the load-bearing site. The witness-token threading downstream
    # is Phase 4 / Phase 9 work.
    try:
        proof = assert_two_affirmation(cfg, cli_profile=profile)
    except ForensicAffirmationError as exc:
        typer.echo(f"whatifd: {exc}", err=True)
        raise typer.Exit(code=EXIT_INCONCLUSIVE_OR_SETUP_FAILURE) from exc

    # Phase 8.2 dispatches into _run_fork_pipeline, which holds
    # the typed-proof contract: callers MUST pass a
    # TwoAffirmationProof. The compiler now rejects any future
    # refactor that bypasses the witness — the threading is
    # structural, not by comment convention.
    exit_code = _run_fork_pipeline(cfg, proof)
    raise typer.Exit(code=exit_code)


def _compute_config_hash(cfg: WhatifConfig) -> str:
    """Compute a deterministic sha256 over the validated config.

    The `RunManifest.config_hash` field is `x-deterministic: true`
    in the schema. Returning a real hash here means determinism
    tests (Phase 9A.3) can pin reproducibility across runs that
    share a config — and consumers reading the report can
    distinguish "same config" from "different config" without
    being misled by a placeholder zero.

    Uses the project's canonical JSON encoder so the hash matches
    `whatifd.cache.keying`'s discipline: sorted keys, no
    whitespace, deterministic float formatting.
    """
    import hashlib

    from whatifd.serialization import canonical_json_bytes

    payload = cfg.model_dump(mode="json")
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _run_fork_pipeline(cfg: WhatifConfig, proof: TwoAffirmationProof) -> int:
    """Execute the fork pipeline (replay → score → decision →
    render) and return the appropriate exit code.

    Phase 10.4 wiring. The signature is the same load-bearing
    contract surface (cfg, proof) → int the dispatcher always
    declared; the body now actually does the work:

    1. Build the trace source from `cfg.source` (Phase 10.1
       factory).
    2. Build the scorer from `cfg.scorer` (Phase 10.1 factory).
    3. Load the runner from `cfg.target.runner` (Phase 10.2
       loader).
    4. Build the per-trace `delta_fn` closure threading runner
       + scorer through the replay kernel (Phase 10.3).
    5. Build a `RunManifest` from cfg + runtime info.
    6. Call `run_pipeline(...)` → `ReportV01`.
    7. Run the cardinal-#5 graph walk
       `assert_no_unredacted_sensitive(report)` BEFORE serialization
       (cascade-catalog entry "Artifact-write call-site sequencing
       for graph walk").
    8. Serialize JSON + render Markdown to the configured
       `cfg.reporting.*_path` outputs.
    9. Return the exit code derived from `report.verdict_state`.

    ## Cardinal alignment in this body

    - **#1 failures-as-data:** every adapter / loader exception is
      caught and converted to a setup-failure stderr + exit 2. No
      stack traces leak.
    - **#2 trust floor:** `run_pipeline` enforces the floor; this
      dispatcher does NOT bypass it. The witness-token threading
      lives inside `compute_verdict` — exit 2 surfaces here
      whenever `verdict_state == "inconclusive"`.
    - **#5 Sensitive at boundary:** `assert_no_unredacted_sensitive`
      runs BEFORE `encode_report_v01` so an unwrapped Sensitive
      anywhere in the report tree fails loud at the graph walk
      (the structural defense), not silently at the encoder fallback.
    - **#7 two-affirmation:** the witness-token guard below is
      executable enforcement; it survives `python -O`.
    """
    if not isinstance(proof, TwoAffirmationProof):
        raise TypeError(
            "_run_fork_pipeline must receive a TwoAffirmationProof "
            "from assert_two_affirmation; bypassing the witness "
            "violates cardinal #7."
        )
    # v0.1 redaction discipline: ALL reports go through the same
    # `Sensitive[T]` + graph-walk + encoder-reject path regardless
    # of `proof.forensic_active`, so the witness is consumed as
    # presence-evidence (the isinstance guard above) rather than as
    # a redaction-profile selector. The forensic-bundle dispatch
    # that WOULD read `proof.forensic_active` is v0.2 work — see
    # cascade-catalog "Forensic profile bundle emission". Marking
    # the unused-binding here so a future contributor doesn't
    # mistake the silent discard for a missed wiring.
    _ = proof.forensic_active  # consumed as presence-evidence; bundle path is v0.2

    # Lazy imports keep the module-load cost of `import whatifd.cli`
    # bounded (typer / cli surface). Adapter factory + run_pipeline +
    # render are heavier; importing them here also preserves the
    # cardinal-enforced lazy-load contract for adapter packages.
    import datetime
    import platform
    import sys

    from whatifd import __version__ as _whatif_version
    from whatifd.adapters import AdapterFactoryError, build_scorer, build_trace_source
    from whatifd.cache.summary import CachePolicySnapshot, CacheSummary
    from whatifd.cli_pipeline import build_delta_fn
    from whatifd.pipeline import run_pipeline
    from whatifd.render.markdown import render_full_report
    from whatifd.runner_loader import RunnerLoadError, load_runner
    from whatifd.serialization import assert_no_unredacted_sensitive, encode_report_v01
    from whatifd.types.manifest import EnvironmentFingerprint, RunManifest
    from whatifd.types.policy import DecisionPolicy, TrustFloor
    from whatifd.types.primitives import DecimalString
    from whatifd.types.statistical import (
        BootstrapMethodDisclosure,
        EffectSizeDisclosure,
        JudgeMethodDisclosure,
        MethodologyDisclosure,
        MultiplicityDisclosure,
    )

    started_at = datetime.datetime.now(datetime.UTC)
    try:
        trace_source = build_trace_source(cfg.source)
        scorer = build_scorer(cfg.scorer)
        loaded_runner = load_runner(cfg.target.runner)
    except (AdapterFactoryError, RunnerLoadError) as exc:
        typer.echo(f"whatifd: setup failure: {exc}", err=True)
        return EXIT_INCONCLUSIVE_OR_SETUP_FAILURE

    delta_fn = build_delta_fn(
        loaded_runner=loaded_runner,
        scorer=scorer,
        change=cfg.change,
        replay_timeout_seconds=cfg.timeouts.replay_seconds,
    )

    # v0.1 floor + policy come from `cfg.decision`; the cli-pipeline
    # closure handles per-trace replay/score, run_pipeline owns the
    # cohort aggregation + verdict.
    floor = TrustFloor()  # v0.1: floor defaults; v0.2 may pull from cfg
    policy = DecisionPolicy(
        require_baseline=cfg.decision.require_baseline,
        max_baseline_regression_ratio=cfg.decision.max_baseline_regression_ratio,
        min_failure_improvement_ratio=cfg.decision.min_failure_improvement_ratio,
        max_ci_width=cfg.decision.max_ci_width,
        practical_delta_epsilon=cfg.decision.practical_delta_epsilon,
    )

    # MethodologyDisclosure: Phase E.2 flipped this to declare the
    # real `paired_percentile_bootstrap` method. Cardinal #10
    # (statistical claims match the design): every bootstrap
    # parameter the disclosure carries (seed, resamples, ci_level)
    # MUST echo what the pipeline actually ran. Importing all three
    # from `whatifd.pipeline` instead of duplicating literals
    # eliminates the silent-drift class — a future change to any
    # parameter updates both sites at once. Cluster-paired bootstrap
    # (where resamples respect cluster boundaries like session_id)
    # is the v0.3 surface; the schema enum already distinguishes
    # `cluster_paired_percentile_bootstrap`.
    methodology = MethodologyDisclosure(
        unit_of_analysis="paired_trace_delta",
        primary_metric="faithfulness",
        primary_endpoints=("failure.faithfulness", "baseline.faithfulness"),
        cohorts=("failure", "baseline"),
        bootstrap=BootstrapMethodDisclosure(
            method="paired_percentile_bootstrap",
            resamples=BOOTSTRAP_RESAMPLES,
            seed=BOOTSTRAP_SEED,
            sample_unit="paired_trace_delta",
            ci_level=BOOTSTRAP_CI_LEVEL_DECIMAL,
            cluster_key=None,
            assumptions=(
                "i.i.d. resampling across paired traces (no cluster boundaries respected)",
            ),
            unavailable_reason=None,
        ),
        multiplicity=MultiplicityDisclosure(
            primary_endpoint_count=2,
            correction="none",
            reason="single primary metric per cohort; no correction applied",
        ),
        # JudgeMethodDisclosure: v0.1 dispatcher fills what it can
        # from the live scorer's `adapter_metadata()` (adapter_id,
        # package_version, sdk_version). The prompt/rubric hashes
        # need a representative ScoreCase to compute via
        # `scorer.cache_key_components(case)`, which the dispatcher
        # does NOT have at this point in the flow (scoring happens
        # downstream inside `delta_fn`). Rather than emit zero-bytes
        # that look like real hashes (a misleading methodology
        # disclosure — cardinal #10), tag the placeholders with a
        # human-readable sentinel so a reviewer reading the report
        # immediately sees v0.1 hasn't yet projected the actual
        # rubric/prompt provenance.
        # TODO(Phase 11): widen `run_pipeline` to accept the scorer
        # directly so the methodology disclosure can include the
        # first-trace rubric+prompt hashes; cascade-catalog entry
        # "Phase 11: scorer projection through run_pipeline".
        judge=JudgeMethodDisclosure(
            scorer=scorer.adapter_metadata().adapter_id,
            scorer_version=scorer.adapter_metadata().package_version,
            judge_provider=cfg.scorer.adapter,
            judge_model=cfg.scorer.adapter,
            judge_model_version=scorer.adapter_metadata().sdk_version,
            rendered_prompt_hash="v01-cli-placeholder-no-scorecase",
            rubric_hash="v01-cli-placeholder-no-scorecase",
            scorer_cache_enabled=cfg.scorer.cache_mode != "off",
            scorer_cache_mode=cfg.scorer.cache_mode if cfg.scorer.cache_mode != "auto" else "off",
            # v0.1 dispatcher does NOT yet wire the scorer cache
            # subsystem into the pipeline (cache_summary above is
            # mode="off" with hits=0/misses=0; documented Phase 10.5
            # work). Hits/misses are 0 by reality, not by
            # placeholder. `reproducibility_addressed` follows the
            # actual cache state — claiming True when the cache is
            # off would be cardinal-#10 untruthful methodology
            # disclosure (the doctrine bot caught this on PR #70).
            # When the cache wires in, the dispatcher will set this
            # to `cfg.scorer.cache_mode != "off"` truthfully.
            scorer_cache_hits=0,
            scorer_cache_misses=0,
            reproducibility_addressed=False,
            reliability_measured=False,
            validity_measured=False,
            calibration_measured=False,
            bias_audit_measured=False,
        ),
        effect_size=EffectSizeDisclosure(
            practical_delta=DecimalString(f"{cfg.decision.practical_delta_epsilon:.3f}"),
            practical_delta_source="policy",
            judge_noise_floor=None,
        ),
        per_trace_inference="descriptive_only",
        causal_claim_scope="associated_under_cached_tool_replay",
    )

    # CacheSummary: v0.1 fork CLI emits an "off" cache summary; the
    # cache subsystem is exercised programmatically by callers that
    # want it. A future Phase 10.5 may wire `cfg.scorer.cache_mode`
    # through to a real CacheSummary projection.
    cache_summary = CacheSummary(
        schema_version="v1",
        key_version="v1",
        mode="off",
        storage_profile="normalized_result_only",
        storage_path=str(DEFAULT_CACHE_ROOT),
        hits=0,
        misses=0,
        writes=0,
        stale_hits=0,
        corrupted_entries=0,
        policy=CachePolicySnapshot(
            mode="off",
            warn_after_days=30,
            block_after_days=90,
            storage_profile="normalized_result_only",
        ),
        policy_violations=(),
        oldest_hit_age_days=None,
        models_distribution={},
    )

    runtime = RunManifest(
        experiment_id="whatifd-fork",
        started_at=started_at.isoformat(),
        finished_at=datetime.datetime.now(datetime.UTC).isoformat(),
        duration_ms=int((datetime.datetime.now(datetime.UTC) - started_at).total_seconds() * 1000),
        whatif_version=_whatif_version,
        # config_hash is `x-deterministic: true` in the schema —
        # consumers expect a real hex sha256. Emitting "0"*64 (a
        # legal hex string) would silently look like a real hash.
        # Cardinal #4 (determinism opt-in) + cardinal #10
        # (truthful methodology): hash the loaded config at
        # dispatch time so the field actually means what consumers
        # expect. Lazy import of canonical_json_bytes from the
        # serialization layer keeps the import surface clean.
        config_hash=_compute_config_hash(cfg),
        selection_seed=42,
        source=cfg.source.adapter,
        target=loaded_runner.reference,
        trust_floor=floor,
        decision_policy=policy,
        environment=EnvironmentFingerprint(
            python=platform.python_version(),
            platform=sys.platform,
            whatif_version=_whatif_version,
        ),
        experiment_shape=cfg.experiment_shape,
    )

    try:
        report = run_pipeline(
            trace_source,
            delta_fn=delta_fn,
            floor=floor,
            policy=policy,
            runtime=runtime,
            methodology=methodology,
            cache_summary=cache_summary,
        )
    except Exception as exc:  # boundary catch; cardinal #1
        typer.echo(f"whatifd: pipeline error: {type(exc).__name__}: {exc}", err=True)
        return EXIT_INCONCLUSIVE_OR_SETUP_FAILURE

    # Cardinal #5 structural defense: walk the report tree before
    # serialization. The encoder's reject-unwrapped-Sensitive in
    # `default()` is the last-line fallback; the graph walk is the
    # primary defense. Cascade-catalog entry "Artifact-write
    # call-site sequencing for graph walk" pins this ordering.
    try:
        assert_no_unredacted_sensitive(report)
    except Exception as exc:
        typer.echo(
            f"whatifd: cardinal-#5 graph-walk failed: {exc}. This is a "
            "structural defect — the report contains unwrapped "
            "Sensitive[T] values. Refusing to serialize.",
            err=True,
        )
        return EXIT_INCONCLUSIVE_OR_SETUP_FAILURE

    # JSON + Markdown artifacts.
    report_md_path = Path(f"./reports/whatifd-fork-{started_at.strftime('%Y-%m-%d')}.md")
    report_json_path = report_md_path.with_suffix(".json")
    report_md_path.parent.mkdir(parents=True, exist_ok=True)
    report_json_path.write_bytes(encode_report_v01(report))
    report_md_path.write_text(render_full_report(report), encoding="utf-8")
    typer.echo(f"whatifd: report written to {report_md_path} (+ .json)")

    # Exit code from verdict_state.
    if report.verdict_state == "ship":
        return EXIT_SHIP
    if report.verdict_state == "dont_ship":
        return EXIT_DONT_SHIP
    return EXIT_INCONCLUSIVE_OR_SETUP_FAILURE


# ---------------------------------------------------------------------------
# Subcommands (Phase 8.3 / 8.4 / 8.5 — fully implemented)
# ---------------------------------------------------------------------------


cache_app = typer.Typer(help="Cache management subcommands.")
app.add_typer(cache_app, name="cache")


@cache_app.command("rebuild")
def cache_rebuild(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Required to actually delete entries. Without this flag, "
                "the command is a no-op safety belt against typos."
            ),
        ),
    ] = False,
    cache_root: Annotated[
        Path,
        typer.Option("--cache-root", help="Cache root (default `.whatifd/cache`)."),
    ] = DEFAULT_CACHE_ROOT,
) -> None:
    """Wipe `<cache-root>/entries/`. Preserves `meta.json` and the
    lock file so the storage layer's schema-version contract stays
    intact; only cached values are removed.

    Exits 0 on a clean rebuild OR a no-op-because-no-entries-dir.
    Exits 2 when `--force` is missing (safety belt).
    """

    result = rebuild(cache_root, force=force)
    if result.error == "force_required":
        typer.echo(
            "whatifd cache rebuild: refusing to delete without --force.",
            err=True,
        )
        raise typer.Exit(code=EXIT_INCONCLUSIVE_OR_SETUP_FAILURE)
    if result.error == "entries_dir_missing":
        typer.echo(
            f"whatifd cache rebuild: no entries directory at {cache_root}/entries (already clean).",
        )
        raise typer.Exit(code=EXIT_SUCCESS)
    summary = (
        f"whatifd cache rebuild: removed {result.entries_removed} entries "
        f"across {result.bucket_dirs_removed} bucket directories under "
        f"{cache_root}/entries."
    )
    typer.echo(summary)
    # Surface anomaly counts when non-zero so the operator sees
    # the same information the result dataclass carries. A silent
    # zero leaves clean output; a positive count prints the
    # anomaly with its location so the operator can investigate.
    if result.non_bucket_skipped:
        typer.echo(
            f"  note: skipped {result.non_bucket_skipped} non-directory "
            f"path(s) directly under {cache_root}/entries (stray files; "
            "preserved for inspection).",
            err=True,
        )
    if result.non_file_skipped_in_bucket:
        typer.echo(
            f"  note: skipped {result.non_file_skipped_in_bucket} "
            f"non-file path(s) inside bucket directories (unexpected "
            "subdirs; bucket dirs preserved for inspection).",
            err=True,
        )
    raise typer.Exit(code=EXIT_SUCCESS)


@cache_app.command("unlock")
def cache_unlock(
    allow_alive: Annotated[
        bool,
        typer.Option(
            "--allow-alive",
            help=(
                "Override the live-PID safety check. Use only when "
                "you're sure the recorded process is gone."
            ),
        ),
    ] = False,
    cache_root: Annotated[
        Path,
        typer.Option("--cache-root", help="Cache root (default `.whatifd/cache`)."),
    ] = DEFAULT_CACHE_ROOT,
) -> None:
    """Remove `<cache-root>/.lock` after a PID-alive safety check.

    Default refuses to clobber a live lock; `--allow-alive`
    overrides. Exits 0 on successful unlock OR no-lock-file.
    Exits 2 when the lock holder is alive and `--allow-alive`
    was not passed.
    """

    result = unlock(cache_root, allow_alive=allow_alive)
    if result.error == "no_lock_file":
        typer.echo(
            f"whatifd cache unlock: no lock file at {cache_root}/.lock (already unlocked).",
        )
        raise typer.Exit(code=EXIT_SUCCESS)
    if result.error == "lock_holder_alive":
        typer.echo(
            "whatifd cache unlock: lock holder is still alive. Pass --allow-alive to override.",
            err=True,
        )
        raise typer.Exit(code=EXIT_INCONCLUSIVE_OR_SETUP_FAILURE)
    if result.error == "unlink_failed":
        # The closed UnlockErrorCode literal lets us match
        # exhaustively on the sentinel; `unlink_error` carries
        # the OS-level diagnostic separately.
        typer.echo(
            f"whatifd cache unlock: unlink failed: {result.unlink_error}",
            err=True,
        )
        raise typer.Exit(code=EXIT_INCONCLUSIVE_OR_SETUP_FAILURE)

    if result.pid_was_alive:
        typer.echo(
            "whatifd cache unlock: removed lock file (live-PID override via --allow-alive).",
        )
    else:
        typer.echo("whatifd cache unlock: removed stale lock file.")
    raise typer.Exit(code=EXIT_SUCCESS)


@cache_app.command("verify")
def cache_verify(
    cache_root: Annotated[
        Path,
        typer.Option("--cache-root", help="Cache root (default `.whatifd/cache`)."),
    ] = DEFAULT_CACHE_ROOT,
) -> None:
    """Verify cache-entry structural integrity.

    Walks `<cache-root>/entries/` and confirms each JSON file
    parses as a valid CacheEntry. Exits 0 if all entries valid OR
    no entries directory exists. Exits 2 if any entry is corrupted
    (operator should run `whatifd cache rebuild --force`).

    v0.1 checks structural integrity only; cryptographic
    content-hash verification is deferred to v0.2.
    """
    result = verify(cache_root)
    if result.vacuous:
        typer.echo(
            f"whatifd cache verify: no entries directory at {cache_root}/entries (vacuously clean).",
        )
        raise typer.Exit(code=EXIT_SUCCESS)
    # Print the headline (clean OR corrupted) first, then the
    # anomaly counts apply equally to both branches — operators
    # need the full picture in one invocation, not just on the
    # success path.
    if result.corrupted:
        typer.echo(
            f"whatifd cache verify: {len(result.corrupted)}/{result.total} "
            "entries are corrupted. Files:",
            err=True,
        )
        for p in result.corrupted:
            typer.echo(f"  {p}", err=True)
        typer.echo(
            "Run `whatifd cache rebuild --force` to wipe and start clean.",
            err=True,
        )
    else:
        typer.echo(f"whatifd cache verify: {result.valid}/{result.total} entries OK.")
    # Anomaly counts surface on both paths so an operator sees
    # everything in one invocation. The entries that DO parse
    # pass the structural check; these counters describe layout
    # anomalies that exist independently of corruption.
    if result.non_bucket_skipped:
        typer.echo(
            f"  note: skipped {result.non_bucket_skipped} non-directory "
            f"path(s) directly under {cache_root}/entries (stray files).",
            err=True,
        )
    if result.non_file_skipped_in_bucket:
        typer.echo(
            f"  note: skipped {result.non_file_skipped_in_bucket} "
            "non-file path(s) inside bucket directories (unexpected "
            "subdirs).",
            err=True,
        )
    raise typer.Exit(code=EXIT_INCONCLUSIVE_OR_SETUP_FAILURE if result.corrupted else EXIT_SUCCESS)


@app.command()
def diff(
    prev: Annotated[Path, typer.Argument(help="Previous report.json")],
    new: Annotated[Path, typer.Argument(help="New report.json")],
) -> None:
    """Compare two whatifd reports and emit a Markdown diff to stdout.

    Exits 0 on a successful render (whether or not anything changed —
    the diff is descriptive, not a verdict). Exits 2 on file-level
    errors (missing file, parse failure, non-mapping JSON) surfaced
    as `DiffError` from `load_report`.
    """
    try:
        prev_data = load_report(prev)
        new_data = load_report(new)
    except DiffError as exc:
        typer.echo(f"whatifd diff: {exc}", err=True)
        raise typer.Exit(code=EXIT_INCONCLUSIVE_OR_SETUP_FAILURE) from exc
    report = compute_diff(prev_data, new_data)
    typer.echo(render_diff_markdown(report), nl=False)
    raise typer.Exit(code=EXIT_SUCCESS)


@app.command("report-migrate")
def report_migrate(
    report: Annotated[Path, typer.Argument(help="Report file to migrate")],
    in_place: Annotated[
        bool,
        typer.Option("--in-place", help="Overwrite the input file instead of writing alongside."),
    ] = False,
) -> None:
    """Migrate a report to the current schema.

    v0.1 → v0.2 migration is structurally additive: v0.2 introduced
    the required top-level `experiment_shape` field. Old v0.1 reports
    are upgraded by injecting `experiment_shape: "failure_rescue"`
    (the only shape that existed in v0.1) and bumping `schema_version`
    + `schema_uri`.

    Cardinal #1 (failure-as-data): malformed input produces a
    structured stderr message + exit 2, never an unhandled exception.

    Output: writes `<report>.v0.2.json` next to the input by default,
    or overwrites the input with `--in-place`. Already-current reports
    are reported as no-ops with exit 0.
    """
    from whatifd.report.migrate import (
        MigrationError,
        migrate_report,
    )
    from whatifd.report.models_v01 import REPORT_SCHEMA_VERSION
    from whatifd.serialization import ReportLoadError, load_report_json

    try:
        raw = load_report_json(report)
    except ReportLoadError as exc:
        typer.echo(f"whatifd report-migrate: {exc}", err=True)
        raise typer.Exit(code=EXIT_INCONCLUSIVE_OR_SETUP_FAILURE) from exc

    try:
        migrated, changed = migrate_report(raw)
    except MigrationError as exc:
        typer.echo(f"whatifd report-migrate: {exc}", err=True)
        raise typer.Exit(code=EXIT_INCONCLUSIVE_OR_SETUP_FAILURE) from exc

    if not changed:
        typer.echo(
            f"whatifd report-migrate: {report} already at v{REPORT_SCHEMA_VERSION}. No-op.",
        )
        raise typer.Exit(code=EXIT_SUCCESS)

    from whatifd.serialization import canonical_json_bytes

    # `parent / (stem + suffix)` rather than `with_suffix` — explicit
    # about intent: keep the original stem, append the version marker,
    # land beside the input. `with_suffix` strips only the last suffix
    # which works for `report.json` but is less obvious for multi-dot
    # filenames like `run.2026-05-10.json`.
    out_path = (
        report if in_place else report.parent / f"{report.stem}.v{REPORT_SCHEMA_VERSION}.json"
    )
    out_path.write_bytes(canonical_json_bytes(migrated) + b"\n")
    typer.echo(f"whatifd report-migrate: wrote {out_path} (v{REPORT_SCHEMA_VERSION}).")
    raise typer.Exit(code=EXIT_SUCCESS)


def main() -> None:
    """Console-script entry point (`pyproject.toml` declares
    `whatifd = whatifd.cli:main`)."""
    app()


if __name__ == "__main__":
    main()
