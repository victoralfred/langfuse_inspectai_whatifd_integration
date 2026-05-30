"""Dump full evaluator output JSON for a sample of traces, so we can see
the verdict field the auto-scorer emits (score / hallucination / pass-fail)
and design the cohort classifier + real delta around it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

_env = json.loads(Path("/home/voseghale/projects/trading/.claude/settings.json").read_text())["env"]
from langfuse.api import LangfuseAPI

api = LangfuseAPI(
    base_url=_env["LANGFUSE_BASE_URL"],
    username=_env["LANGFUSE_PUBLIC_KEY"],
    password=_env["LANGFUSE_SECRET_KEY"],
)

traces = list(api.trace.list(page=1, limit=50).data)
print(f"{len(traces)} traces\n")

# What method does ScoresClient actually expose?
sc = getattr(api, "scores", None)
if sc is not None:
    print("ScoresClient methods:", [m for m in dir(sc) if not m.startswith("_")])
print()

# Collect the structure of every output. Most are dicts — record their keys.
key_counter: Counter[str] = Counter()
name_counter: Counter[str] = Counter()
for t in traces:
    name_counter[getattr(t, "name", None) or "?"] += 1
    out = getattr(t, "output", None)
    if isinstance(out, dict):
        for k in out:
            key_counter[k] += 1

print("trace.name distribution:", dict(name_counter))
print("output dict keys across traces:", dict(key_counter))
print()

# Full dump of the first 4 outputs.
for i, t in enumerate(traces[:4]):
    print(f"===== trace {i} id={t.id} name={getattr(t,'name',None)} tags={getattr(t,'tags',None)} =====")
    out = getattr(t, "output", None)
    print("OUTPUT:")
    print(json.dumps(out, indent=2, default=str)[:1800])
    print()
