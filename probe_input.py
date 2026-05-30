"""Dump the full INPUT of one evaluator trace so the replay runner can
reconstruct the evaluation prompt exactly (messages list shape, roles,
system vs user split)."""

from __future__ import annotations

import json
from pathlib import Path

_env = json.loads(Path("/home/voseghale/projects/trading/.claude/settings.json").read_text())["env"]
from langfuse.api import LangfuseAPI

api = LangfuseAPI(
    base_url=_env["LANGFUSE_BASE_URL"],
    username=_env["LANGFUSE_PUBLIC_KEY"],
    password=_env["LANGFUSE_SECRET_KEY"],
)

traces = list(api.trace.list(page=1, limit=50).data)
ev = [t for t in traces if isinstance(getattr(t, "output", None), dict)
      and t.output.get("score") is not None][0]

print(f"trace id={ev.id} name={ev.name}")
inp = ev.input
print(f"\ninput type: {type(inp).__name__}")
if isinstance(inp, list):
    print(f"messages: {len(inp)}")
    for i, m in enumerate(inp):
        if isinstance(m, dict):
            role = m.get("role", "?")
            content = m.get("content", "")
            preview = (content if isinstance(content, str) else json.dumps(content, default=str))
            print(f"\n--- msg[{i}] role={role} len={len(preview)} ---")
            print(preview[:1400])
else:
    print(json.dumps(inp, indent=2, default=str)[:2500])

print(f"\n\nOUTPUT: {json.dumps(ev.output, indent=2, default=str)[:600]}")
print(f"metadata keys: {list((ev.metadata or {}).keys())}")
print(f"metadata: {json.dumps(ev.metadata, default=str)[:500]}")
