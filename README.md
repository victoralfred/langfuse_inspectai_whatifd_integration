# fxtrade agent-faithfulness evaluation + whatifd harness

A local harness that
- (1) re-implements the broken Langfuse "Claude Code evaluator" as a **corrected faithfulness evaluator**
-  and (2) wires it end-to-end into a **whatifd failure-rescue experiment** against live Langfuse. Grounded in live data (`192.168.1.104:3000`, 2026-05-30).

## Layout

```
run_demo.py         ONE-COMMAND DEMO: runs the whole stack (0.2.1 compat ->
                    v2 cache proof -> live faithfulness rescue) and prints the
                    result. Selectable judge backend. Start here.
evaluator/
  faithfulness.py   The fixed evaluator: reference = the TOOL RESULTS a turn
                    saw (not the response — fixes the tautology); judge returns
                    a validated 1-5 score normalized to [0,1] (fixes the scale).
                    The shared RUBRIC lives here.
  inspect_scorer.py Inspect AI integration: the SAME rubric wrapped as a
                    whatifd InspectAIScorer, judging via inspect_ai's model
                    layer. Returns the faithfulness DELTA (replayed - original).
                    Also exposes a config-loadable `score_fn` (see config below).
  cached_scorer.py  CachedScorer: wraps any whatifd Scorer with whatifd's v2
                    cache-keying + storage (0.2.1 F-2.1 path). Re-scores MISS on
                    a changed replayed output instead of returning a stale delta.
  demo.py           Runs the evaluator on real agent turns, prints scores.
harness/
  whatifd_run.py    End-to-end (programmatic): fork agent turns -> cohort by
                    faithfulness -> replay under a candidate prompt -> score
                    delta -> whatifd verdict. SCORER_BACKEND = "inspect" |
                    "anthropic"; SCORER_CACHE_MODE = off|on|... (both selectable
                    from run_demo.py).
  validate_config_scorer.py  Proves the scorer lifts into the YAML: whatifd's
                    own load_config + build_scorer builds the InspectAIScorer
                    and scores a live case.
whatifd.config.yaml The production `whatifd fork` config (scorer lifts; runner
                    + cohorting caveats documented inline).
probes/             One-off Langfuse recon scripts (how the data was diagnosed)
                    + probe_scorer_cache.py (offline, deterministic v2-keying
                    proof; no creds needed).
reports/            Emitted ReportV01 JSON.
README.md           This file (design + wiring rationale below).
```

## Run the demo

`run_demo.py` is the single entry point. It runs three stages as one pipeline
and prints the result:

1. **whatifd 0.2.1 compatibility** (offline) — loads `whatifd.config.yaml` and
   builds the scorer through whatifd's own 0.2.1 validators.
2. **v2 scorer-cache keying proof** (offline, deterministic, no creds) — proves
   the 0.2.1 F-2.1 fix: a re-score after the replayed output changes MISSES the
   cache instead of returning the stale v1-collision delta.
3. **faithfulness rescue** (live) — the full pipeline against Langfuse +
   Anthropic; prints the verdict and (for the inspect judge) the live cache tally.

```bash
./.venv/bin/python run_demo.py                    # inspect judge, cache on (default)
./.venv/bin/python run_demo.py --judge anthropic  # raw Anthropic SDK judge instead
./.venv/bin/python run_demo.py --cache off        # disable the scorer cache
./.venv/bin/python run_demo.py --offline          # stages 0 + 1 only (no creds)
```

| Flag | Values | Meaning |
|---|---|---|
| `--judge` | `inspect` (default), `anthropic` | scorer-slot judge: Inspect AI's model layer, or the raw Anthropic SDK |
| `--cache` | `on` (default), `off`, `auto`, `read_only`, `refresh` | scorer-cache mode (live stage; effective only with `--judge inspect`) |
| `--offline` | — | run only the offline stages (0 + 1); skip the live pipeline |

**Creds:** the live stage needs `ANTHROPIC_API_KEY` + `LANGFUSE_BASE_URL`/
`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` in the environment (Langfuse creds
can also come from the harness's `settings.json`). Without them, the live stage
auto-skips. `--offline` runs the offline stages with no creds at all.

### Run the pieces individually

```bash
PYTHONPATH=evaluator ./.venv/bin/python probes/probe_scorer_cache.py   # v2 cache proof (offline)
./.venv/bin/python evaluator/demo.py                                   # validate the evaluator fix
./.venv/bin/python harness/whatifd_run.py                              # full programmatic whatifd run
PYTHONPATH=evaluator:harness ./.venv/bin/python harness/validate_config_scorer.py  # prove the config scorer lift
```

## What the two bugs were, and the fix

The Langfuse server-side evaluator had two defects (diagnosed 2026-05-30):

1. **Tautology** — it fed `response to evaluate` == `reference context`
   (byte-identical across 24/24 traces), so it could only ever say "fully
   accurate." **Fix:** the reference is now the **tool results the turn
   actually observed** (pulled from the agent trace's `[TOOL]` observations),
   which is distinct from the response — so fabrication is detectable.
2. **Scale** — it recorded `score: 1` while reasoning "fully accurate" on a
   1-5 rubric where 5 = best. **Fix:** the judge returns a validated 1-5
   integer, normalized `5 -> 1.0`, `1 -> 0.0`.

## Validated results (live)

Current live state (`run_demo.py`, 2026-05-30, whatifd 0.2.1) — one qualifying
agent turn on the Langfuse page, scored through the Inspect AI judge with the
v2 scorer cache enabled:

```
[baseline] Claude Code - Turn 43: original faithfulness 5/5   (every claim supported)
  [inspect] replay 587ccd7f: delta +0.00                       no regression on the baseline
[cache] mode=on key_version=v2 | hits=0 misses=1 writes=1
VERDICT: inconclusive
  finding [blocks_all] ci_unavailable_for_required_cohort: 'baseline': sample_too_small
  finding [blocks_all] required_cohort_absent: 'failure' matched zero traces (absent)
```

- whatifd correctly returns **Inconclusive** on thin data — one trace, and it
  classifies into `baseline`, so the `failure` cohort is empty. Both findings
  are actionable: `ci_unavailable_for_required_cohort` (CI needs ≥5 samples) and
  0.2.1's `required_cohort_absent` (the missing-cohort surface this release
  added). It refuses to ship on thin data.
- The scorer cache rode the **v2 keying** path: one cold miss + write, the entry
  persisted under a `v2:` key. The deterministic anti-collision proof is Stage 1
  of the demo (`probes/probe_scorer_cache.py`).
- A real Ship/Don't-Ship needs ≥5 turns per cohort, which accumulate as the
  agent keeps working on fxtrade — and a non-empty `failure` cohort (pre-tag
  low-faithfulness turns, or let real failures land). Re-run `run_demo.py` once
  more turns exist.

**Earlier snapshot (2 turns, before the page rotated)** — the richer result that
proves the evaluator *discriminates* and the candidate prompt *rescues*:

```
[failure ] Turn 27: original faithfulness 2/5   (claimed 5 files written; tools confirm fewer)
[baseline] Turn 26: original faithfulness 5/5   (every claim supported)
  replay Turn 27: 0.25 -> 1.00  (delta +0.75)   candidate prompt rescued the failure
  replay Turn 26: 1.00 -> 1.00  (delta +0.00)   no regression on the baseline
```

The evaluator scored **2/5 vs 5/5** where the old one gave a constant 1 (the fix
is proven), and the candidate change showed the failure-rescue shape: **+0.75 on
the failure, +0.00 on the baseline.** The verdict was still Inconclusive at
n=1/cohort — the same thin-data refusal, just with both cohorts populated.

## Scorer backends: Inspect AI (default) vs raw Anthropic

The harness can score through either backend — `run_demo.py --judge {inspect,anthropic}`,
or the `SCORER_BACKEND` constant in `harness/whatifd_run.py`:

| Backend (`--judge`) | What runs the judge | When to use |
|---|---|---|
| `inspect` (default) | `whatifd_inspect_ai.InspectAIScorer` + `inspect_ai`'s provider-agnostic model layer (`anthropic/claude-...`) | The production path. Same scorer slot a `whatifd fork` CI gate uses; swap providers by changing one model string. |
| `anthropic` | `evaluator/faithfulness.py` calling the Anthropic SDK directly | Dependency-light local debugging; fewer moving parts. (Accepts `custom` as a legacy alias.) |

Both use the **identical rubric** (`evaluator/faithfulness.py::RUBRIC`), so the
ruler is the same — only the plumbing differs. The Inspect AI path scores BOTH
the original and replayed output fresh and returns their difference as the
delta (cardinal #10: one ruler, both sides).

Observed (live): `inspect` gave failure **+0.50** / baseline **+0.00**;
`anthropic` gave **+0.75** / **+0.00** — same rescue shape, expected LLM-judge
variance.

> **Scope of `--judge`:** it selects the **scorer** (the delta judge). The
> cohort classifier in `FaithfulnessSource` still uses the raw-Anthropic judge
> regardless. The **scorer cache** (`--cache`) only engages on `--judge inspect`,
> because whatifd's cache wraps a whatifd `Scorer` (the `InspectAIScorer`); the
> raw-Anthropic delta path is not a whatifd Scorer, so the cache stays off there.

## Production config (`whatifd.config.yaml`) — what lifts, what doesn't

The Inspect AI scorer + rubric are lifted into a real `whatifd fork` config and
**validated through whatifd's own loader**:

```bash
PYTHONPATH=evaluator:harness ./.venv/bin/python harness/validate_config_scorer.py
# config scorer.adapter = 'inspect_ai'
# build_scorer(...) -> InspectAIScorer  (rubric_id=faithfulness-v1)
# scorer.score(case) -> JudgeResult.score (the delta) = +0.50   ← scored a live turn
```

`whatifd.config.yaml` references the lifted scorer as:

```yaml
scorer:
  adapter: inspect_ai
  score_fn: python:inspect_scorer:score_fn   # re-fetches the reference by trace_id
  judge_provider: anthropic
  judge_model_id: claude-haiku-4-5-20251001
  rubric_id: faithfulness-v1
  rubric_text: | ...
```

**The scorer half lifts cleanly.** The standalone `score_fn` works because
whatifd's `ScoreCase` carries `trace_id`, so it re-fetches the tool-results
reference from Langfuse itself (the config path does NOT thread the reference —
`RawTrace.metadata` is dropped and the v0.2 `ToolCache` is empty).

**The runner and cohorting do NOT lift** (real whatifd v0.2 limits, documented
inline in the config):

| Slot | Config path | Why |
|---|---|---|
| Scorer | ✅ lifts | `ScoreCase.trace_id` lets `score_fn` re-fetch the reference |
| Runner | ❌ degraded | `Runner` gets only `user_message` — no `trace_id`, empty `ToolCache`, metadata dropped → cannot re-synthesize against tool results |
| Cohorting | ❌ tag-only | the langfuse factory uses a TAG-based classifier, not faithfulness; forks ALL traces, not just agent turns |

So until whatifd v0.3 ships configurable cohort classifiers + reference
threading (cascade "cohort_classifier configurable"), **the programmatic
harness (`harness/whatifd_run.py`) is the runnable full pipeline**, and
`whatifd.config.yaml` is the migration target whose **scorer block already
works today**. To bridge the gap now, pre-tag low-faithfulness turns `failure`
in Langfuse so the tag classifier populates the cohorts.

---

# Appendix: whatifd ↔ Langfuse wiring rationale

## The core correction: a role swap

You forked the **evaluator** traces and used the evaluator's own score as the
signal. whatifd's model is the other way around:

```
WRONG (what live_langfuse_test.py did)
──────────────────────────────────────
  fork  "Execute evaluator" traces
   └─ signal = the evaluator's self-score        ← all 1, zero variance
   └─ no runner, no replay                        ← delta faked (0.4/0.05)
  ⇒ single baseline cohort, meaningless verdict


RIGHT (whatifd's intended shape)
────────────────────────────────
  fork  "Claude Code - Turn N" AGENT traces       ← the thing you're changing
   ├─ cohort   = agent.scores['Claude Code evaluator'] < threshold → failure
   ├─ runner   = re-run the agent WITH a proposed change → replayed response
   └─ scorer   = the factual-accuracy evaluator, run on BOTH
                 original & replayed responses vs a REAL reference
                 ⇒ delta = eval(replayed) − eval(original)
  ⇒ failure_rescue verdict: did the change rescue the low-scoring turns
     WITHOUT regressing the high-scoring ones?
```

The evaluator is the **ruler**, not the thing under test. The agent is the
thing under test.

## Data-flow (live field names)

```
Langfuse
  trace "Claude Code - Turn 27"            ← AGENT trace (fork target)
    .input   {role:user,  content:"...question..."}
    .output  {role:assistant, content:"...response..."}     ← original_output
    .scores  [("Claude Code evaluator", 1.0), ...]          ← cohort signal
    .metadata.target_trace_id / observation_id              ← links eval↔agent
        │
        ▼
  LangfuseTraceSource(cohort_classifier=by_attached_score)
        │  yields RawTrace(user_message, original_response, cohort)
        ▼
  Runner (python:my_agent:run)        applies change.system_prompt
        │  → ReplayOutput(text="...new response...")
        ▼
  Scorer  (factual-accuracy evaluator as a whatifd Scorer)
        │  score(original) and score(replayed) vs REFERENCE
        │  → JudgeResult(score: float)   delta = replayed − original
        ▼
  run_pipeline → floor → verdict → ReportV01 → exit 0/1/2
```

## Two upstream DATA fixes that must land first

These are in `projects/trading`, not whatifd. Without them the scorer is blind.

1. **Reference ≠ response.** Today the evaluator prompt sets
   `AI response to evaluate` == `Reference context` (byte-identical → always
   "fully accurate"). The reference must be the **ground truth the agent was
   supposed to be faithful to** — the actual tool results / retrieved context —
   NOT the agent's own answer. For a Claude Code turn that's the tool_result
   blocks the agent saw, not its assistant message.

2. **Score scale.** Rubric is 1–5 (5 = best) but recorded `score` is 1 while
   the reasoning says "fully accurate" (= 5). Fix the extraction so the emitted
   integer matches the rubric, then normalize 1–5 → 0–1 in the classifier.

## The 4 whatifd surfaces (skeletons)

### 1. cohort classifier — read the evaluator score ATTACHED to the agent trace

```python
EVAL_SCORE_NAME = "Claude Code evaluator"
FAILURE_BELOW = 0.6   # on a 0–1 normalized scale

def classify(trace) -> str:
    # Langfuse attaches evaluator scores to the agent trace via .scores.
    vals = [s.value for s in (trace.scores or []) if s.name == EVAL_SCORE_NAME]
    if not vals:
        return "baseline"                      # unscored → treat as baseline
    norm = (sum(vals) / len(vals) - 1) / 4     # 1–5 → 0–1  (adjust to your scale)
    return "failure" if norm < FAILURE_BELOW else "baseline"
```

### 2. runner — replay the AGENT with the proposed change (`python:my_agent:run`)

```python
from whatifd.contract import TraceInput, ReplayConfig, ToolCache, ReplayOutput

def run(ti: TraceInput, cfg: ReplayConfig, cache: ToolCache) -> ReplayOutput:
    # Re-invoke YOUR agent on the original user turn, applying the change.
    # cfg.system_prompt is the candidate change whatifd is testing.
    # ToolCache replays the original tool outputs so side effects don't re-fire.
    new_text = my_agent.invoke(
        user_message=ti.user_message,
        system_prompt=cfg.system_prompt,
        tool_cache=cache,
    )
    return ReplayOutput(text=new_text, tool_spans=[], metadata={})
```

### 3. scorer — reuse `InspectAIScorer`, do NOT build a `LangfuseScorer`

**whatifd is an integration, not a reinvention.** Scoring a replay means
running a judge with a rubric — and `whatifd-inspect-ai` already ships that
integration. A bespoke `LangfuseScorer` would duplicate it (or lean on
Langfuse's *unstable* `evaluators` API to re-run judging). The original
design is correct: **Langfuse = source, Inspect AI = scorer.** Both
original and replayed outputs go through the SAME Inspect AI judge, so the
ruler is consistent (cardinal #10).

You reuse your Langfuse evaluator by giving Inspect AI the **same rubric
text + judge model** your Langfuse evaluator uses. The rubric is copied
(Langfuse only exposes evaluator configs through its unstable API, so
whatifd does not auto-fetch it — copying keeps the dependency stable):

```yaml
scorer:
  adapter: inspect_ai
  score_fn: python:my_rubrics.faithfulness:score   # your scoring callable
  judge_provider: anthropic
  judge_model_id: claude-sonnet-4-6                # same model as your LF evaluator
  rubric_id: faithfulness-v3
  rubric_text: |                                    # copied from the LF evaluator
    You are an expert evaluator assessing factual accuracy...
    Score 1-5: 5 = fully accurate, ... 1 = fabricated.
```

`InspectAIScorer` (config-loaded via `scorer.adapter: inspect_ai`) then
scores both `case.original_output` and `case.replayed_output` and whatifd
pairs them into a `TraceDelta`. No new adapter, no new judge — just the
existing integration pointed at your rubric.

> The reference/ground-truth (the thing the response is judged against)
> must be REAL and ≠ the response — see the "two upstream DATA fixes"
> above. Encode it in your `score_fn` / rubric (e.g., pass the tool
> results as the reference), not the response itself.

### 4. config — failure_rescue, wired

```yaml
source:
  adapter: langfuse                 # forks AGENT traces (not evaluator traces)
target:
  runner: python:my_agent.whatifd_runner:run
selection:
  failure_cohort:  { limit: 50 }    # low-scoring agent turns
  baseline_cohort: { limit: 50 }    # high-scoring agent turns
change:
  system_prompt: prompts/candidate.txt
scorer:
  adapter: inspect_ai               # the existing judge integration (§3)
  score_fn: python:my_rubrics.faithfulness:score
  judge_provider: anthropic
  judge_model_id: claude-sonnet-4-6
  rubric_id: faithfulness-v3
  rubric_text: |                     # copied from your Langfuse evaluator
    You are an expert evaluator assessing factual accuracy...
decision: {}
experiment_shape: failure_rescue
```

## Why this finally produces a verdict

- **Variance** comes from forking AGENT traces with a spread of evaluator
  scores (once reference ≠ response, real failures appear).
- **A delta** exists because the runner produces a *replayed* response and the
  scorer judges both sides with one ruler.
- **failure_rescue** can then answer the real question: did `candidate.txt`
  rescue the hallucinating turns without regressing the clean ones →
  Ship / Don't-Ship.
