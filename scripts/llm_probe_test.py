"""Measure Ollama tool-call latency; try think:false then think:true per model.

  python scripts/llm_probe_test.py qwen3:1.7b
"""
import sys
import time

import httpx

url = "http://127.0.0.1:11434/api/chat"
base = {
    "stream": False,
    "messages": [{"role": "user", "content": "bật đèn phòng khách giúp tôi"}],
    "tools": [{
        "type": "function",
        "function": {
            "name": "switch",
            "parameters": {
                "type": "object",
                "properties": {"entity": {"type": "string"},
                               "state": {"type": "string", "enum": ["on", "off"]}},
                "required": ["entity", "state"],
            },
        },
    }],
}

for model in sys.argv[1:] or ["qwen3:1.7b"]:
    for label, extra in [("think=false", {"think": False}), ("think=true", {"think": True})]:
        t0 = time.perf_counter()
        try:
            r = httpx.post(url, json={**base, **extra, "model": model}, timeout=300)
            msg = r.json().get("message", {})
            calls = msg.get("tool_calls") or []
            detail = f"args={calls[0]['function']['arguments']}" if calls else (
                f"content={msg.get('content', '')[:70]!r} thinking={len(msg.get('thinking') or '')}ch"
            )
            print(f"{model} [{label}]: {time.perf_counter() - t0:.1f}s tool_calls={len(calls)} {detail}")
        except Exception as exc:
            print(f"{model} [{label}]: LOI {exc}")
