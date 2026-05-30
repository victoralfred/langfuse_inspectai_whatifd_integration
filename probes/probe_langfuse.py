"""Read-only probe of the live Langfuse instance.

Loads creds from the trading project's settings.json env block, lists a
sample of traces, and reports the shape we actually have to work with:
tags, scores (the auto-scorer output), input/output presence. No whatifd
involved — this is pure reconnaissance so the real test classifies cohorts
against the data that exists, not the data we assumed.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

_SETTINGS = Path("/home/voseghale/projects/trading/.claude/settings.json")
_env = json.loads(_SETTINGS.read_text())["env"]

HOST = _env["LANGFUSE_BASE_URL"]
PUBLIC = _env["LANGFUSE_PUBLIC_KEY"]
SECRET = _env["LANGFUSE_SECRET_KEY"]

print(f"Langfuse host: {HOST}")

from langfuse.api import LangfuseAPI

api = LangfuseAPI(base_url=HOST, username=PUBLIC, password=SECRET)

# --- list a page of traces -------------------------------------------------
resp = api.trace.list(page=1, limit=50)
traces = list(resp.data)
print(f"\nTraces on page 1 (limit 50): {len(traces)}")

if not traces:
    raise SystemExit("No traces returned — nothing to classify.")

# --- field availability ----------------------------------------------------
sample = traces[0]
print(f"\nTrace object attributes: {sorted(a for a in dir(sample) if not a.startswith('_'))}")

tag_counter: Counter[str] = Counter()
has_input = has_output = has_scores = 0
for t in traces:
    for tag in (getattr(t, "tags", None) or []):
        tag_counter[tag] += 1
    if getattr(t, "input", None) not in (None, "", [], {}):
        has_input += 1
    if getattr(t, "output", None) not in (None, "", [], {}):
        has_output += 1
    if getattr(t, "scores", None):
        has_scores += 1

print(f"\ntraces with non-empty input:  {has_input}/{len(traces)}")
print(f"traces with non-empty output: {has_output}/{len(traces)}")
print(f"traces with .scores attr set: {has_scores}/{len(traces)}")
print(f"\nTag distribution: {dict(tag_counter) or '(no tags on any trace)'}")

# --- scores: try the dedicated scores endpoint -----------------------------
# In Langfuse v3/v4 scores are first-class objects, often NOT inlined on the
# trace.list payload. Fetch them via the scores API and bucket by name+value.
print("\n--- scores via api.score.v2.list / api.score.list ---")
score_client = None
for path in ("score_v_2", "score", "scores"):
    score_client = getattr(api, path, None)
    if score_client is not None:
        print(f"using api.{path}")
        break

if score_client is None:
    print("no score client found on api; available:",
          sorted(a for a in dir(api) if not a.startswith('_')))
else:
    try:
        sresp = score_client.list(page=1, limit=50)
        scores = list(getattr(sresp, "data", []))
        print(f"scores returned: {len(scores)}")
        if scores:
            s0 = scores[0]
            print(f"score attrs: {sorted(a for a in dir(s0) if not a.startswith('_'))}")
            name_counter: Counter[str] = Counter()
            vals: list[float] = []
            by_trace: dict[str, list] = {}
            for s in scores:
                nm = getattr(s, "name", "?")
                name_counter[nm] += 1
                v = getattr(s, "value", None)
                if isinstance(v, (int, float)):
                    vals.append(float(v))
                tid = getattr(s, "trace_id", None) or getattr(s, "traceId", None)
                by_trace.setdefault(tid, []).append((nm, getattr(s, "value", None)))
            print(f"score names: {dict(name_counter)}")
            if vals:
                vals_sorted = sorted(vals)
                n = len(vals_sorted)
                print(f"numeric values: n={n} min={vals_sorted[0]:.3f} "
                      f"median={vals_sorted[n//2]:.3f} max={vals_sorted[-1]:.3f}")
                print(f"distinct values: {sorted(set(vals))[:15]}")
            print(f"distinct traces with scores: {len(by_trace)}")
            print("sample trace→scores:")
            for tid, ss in list(by_trace.items())[:5]:
                print(f"  {tid}: {ss}")
    except Exception as exc:  # noqa: BLE001 — recon script, surface anything
        print(f"score list failed: {type(exc).__name__}: {exc}")

# --- one fully-hydrated trace ----------------------------------------------
print("\n--- api.trace.get(first id) full shape ---")
try:
    full = api.trace.get(sample.id)
    print(f"full trace attrs: {sorted(a for a in dir(full) if not a.startswith('_'))}")
    fscores = getattr(full, "scores", None)
    print(f"full.scores: {len(fscores) if fscores else 0} "
          f"{[(getattr(s,'name',None), getattr(s,'value',None)) for s in (fscores or [])][:8]}")
    inp = getattr(full, "input", None)
    print(f"input type={type(inp).__name__} preview={str(inp)[:160]!r}")
    outp = getattr(full, "output", None)
    print(f"output type={type(outp).__name__} preview={str(outp)[:160]!r}")
except Exception as exc:  # noqa: BLE001
    print(f"trace.get failed: {type(exc).__name__}: {exc}")
