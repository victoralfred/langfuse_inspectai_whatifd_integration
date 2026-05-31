# whatifd by example

What `whatifd` actually produces — a worked example for the launch post. Every
report below is **real `render_full_report` output** (and the JSON is a real
`ReportV01` from a live run), not a mock-up. Consistent with the trust-first
pitch: no prettied-up screenshots.

`whatifd` forks production traces, replays them with a proposed change, scores
the diff, and emits a **Ship / Don't-Ship / Inconclusive** verdict you can gate
a PR on. `pip install whatifd` · Apache-2.0 · https://whatif.codes

---

## 1. Define the experiment — `whatifd.config.yaml`

```yaml
source:  { adapter: langfuse }                      # fork real production traces
target:  { runner: "python:my_agent.replay:run" }   # how to re-run one trace
selection:
  failure_cohort:  { limit: 50 }    # traces today's agent gets wrong
  baseline_cohort: { limit: 50 }    # traces it gets right (guard against regressions)
change:
  system_prompt: prompts/candidate-v3.txt           # the thing you're testing
scorer:
  adapter: inspect_ai
  score_fn: "python:my_rubrics.faithfulness:score"
  judge_provider: anthropic
  judge_model_id: claude-haiku-4-5
experiment_shape: failure_rescue
```

```bash
$ whatifd fork --config whatifd.config.yaml
# → reports/whatifd-fork-<date>.{md,json}
# exit 0 = Ship · 1 = Don't Ship · 2 = Inconclusive / setup failure
```

---

## 2. Ship — the change helped without regressing the baseline

CI line: `✓ whatifd: Ship — failures 14/20 ↑, baseline 19/20 stable`

```markdown
# whatifd verdict: Ship

**All floor rules passed. All policy rules passed.**

## Stats

**Failures (20):**   improved 14   unchanged 4   regressed 2   median Δ 0.310   CI [0.180, 0.440]
**Baseline (20):**   improved 3   unchanged 16   regressed 1   median Δ 0.020   CI [-0.010, 0.050]

## Replay validity

**failure:** 20 selected, 20 replayed (100.0%), 20 scored (100.0%).
**baseline:** 20 selected, 20 replayed (100.0%), 20 scored (100.0%).

## Suggested next steps

No actionable findings — the verdict is Ship.

## Methodology
- Unit: paired_trace_delta · Endpoints: failure_improvement, baseline_non_regression
- Bootstrap: paired_percentile_bootstrap, B=5000, seed=42 · CI level: 0.95
- Causal scope: associated_under_cached_tool_replay
- Judge: claude-haiku-4-5 · Cache: enabled (38 hits, 2 misses)
- Reliability state: reproducibility=yes, reliability=no, validity=no, calibration=no, bias=no

[Manifest →](manifest.json)
```

---

## 3. Don't-Ship — the change rescued failures but regressed the baseline

CI line: `✗ whatifd: Don't Ship — baseline cohort regressed 6/20 traces (30%), exceeding…`

```markdown
# whatifd verdict: Don't Ship

**baseline cohort regressed 6/20 traces (30%), exceeding the 10% threshold.**

## Stats

**Failures (20):**   improved 14   unchanged 3   regressed 3   median Δ 0.280   CI [0.150, 0.410]
**Baseline (20):**   improved 1    unchanged 13  regressed 6   median Δ -0.180  CI [-0.240, -0.120]

## Replay validity

**failure:** 20 selected, 20 replayed (100.0%), 20 scored (100.0%).
**baseline:** 20 selected, 20 replayed (100.0%), 20 scored (100.0%).

## Suggested next steps

### Baseline cohort regressed beyond the policy threshold.
1. Identify which baseline traces regressed and look for a common pattern
   (prompt change, tool behavior change, model output drift).
2. Run `whatifd diff <previous-report.json> <this-report.json>` to compare
   against a known-good run.
3. If the failure-cohort gain justifies it, adjust
   `DecisionPolicy.max_baseline_regression_ratio` — but only with a
   documented rationale.
4. Otherwise, do not ship. Iterate on the change to reduce baseline impact.

## Methodology
- Unit: paired_trace_delta · Endpoints: failure_improvement, baseline_non_regression
- Bootstrap: paired_percentile_bootstrap, B=5000, seed=42 · CI level: 0.95
- Causal scope: associated_under_cached_tool_replay
- Reliability state: reproducibility=yes, reliability=no, validity=no, calibration=no, bias=no

[Manifest →](manifest.json)
```

---

## 4. Inconclusive — the evidence is too thin to claim anything

The differentiator: rather than emit a confident-looking number, `whatifd`
returns **Inconclusive** when a required cohort can't clear the trust floor
(here: too few scored traces to compute a bootstrap CI). Real `ReportV01` JSON
(the machine-readable side, from a live run):

```json
{
  "schema_uri": "https://whatif.codes/schema/report/v0.2.json",
  "verdict_state": "inconclusive",
  "experiment_shape": "failure_rescue",
  "cohort_results": [
    {"name": "failure",  "selected": 2, "scored": 2, "median_delta": null, "floor_passed": false},
    {"name": "baseline", "selected": 1, "scored": 1, "median_delta": null, "floor_passed": false}
  ],
  "decision_findings": [
    {"code": "ci_unavailable_for_required_cohort", "severity": "blocks_all"}
  ],
  "methodology": {
    "primary_metric": "faithfulness",
    "causal_claim_scope": "associated_under_cached_tool_replay"
  }
}
```

---

## What this shows

- **A verdict, not a vanity metric.** Ship / Don't-Ship / Inconclusive maps to a
  CI exit code (0 / 1 / 2) — gate a PR on it.
- **Baseline guard.** A change that rescues failures but regresses the
  known-good set is a **Don't-Ship**, not a win.
- **Trust-first.** Every report discloses its methodology, pins tool outputs so
  side effects don't re-fire, claims only "associated under cached-tool replay"
  (never "caused"), and returns **Inconclusive** rather than faking confidence
  on thin evidence.

Docs & walkthroughs: https://whatif.codes · Code: https://github.com/victoralfred/whatifd
