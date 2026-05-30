# whatifd ↔ Langfuse: the correct end-to-end wiring

Grounded in your live data (`192.168.1.104:3000`, 2026-05-30).

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

### 3. scorer — the factual-accuracy evaluator, as a whatifd Scorer

This is the piece `whatifd-langfuse` does NOT ship today (it only ships a
TraceSource). It reuses your evaluator rubric so original and replayed are
scored with the SAME ruler (cardinal #10).

```python
from whatifd.adapters.protocols import Scorer, JudgeResult
from whatifd.contract import ScoreCase

class FactualAccuracyScorer:                       # implements Scorer
    def __init__(self, rubric: str, judge_model: str, reference_of):
        self._rubric = rubric
        self._judge = judge_model
        self._reference_of = reference_of          # trace_id → ground-truth text

    def score(self, case: ScoreCase) -> JudgeResult:
        reference = self._reference_of(case.trace_id)   # REAL ref, ≠ response
        s_orig = self._judge_call(case.original_output.text, reference)
        s_repl = self._judge_call(case.replayed_output.text, reference)
        # whatifd diffs original vs replayed internally; emit the replayed
        # score on the native scale (it pairs them into a TraceDelta).
        return JudgeResult(trace_id=case.trace_id, score=s_repl,
                           rationale=..., judge_model_id=self._judge)

    def cache_key_components(self, case): ...
    def adapter_metadata(self): ...
```

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
  adapter: factual_accuracy         # the evaluator-as-scorer above
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
```
