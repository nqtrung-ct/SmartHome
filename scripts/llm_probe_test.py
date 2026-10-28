"""Measure Ollama tool-call latency for one or more models.

  python scripts/llm_probe_test.py qwen3:4b qwen3:1.7b
"""
import json
import sys
import time

import httpx

url = "http://127.0.0.1:11434/api/chat"
base = {
    "stream": False,
    "think": False,
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

for model in sys.argv[1:] or ["qwen3:4b"]:
    for attempt in (1, 2, 3):  # later runs = warm model
        t0 = time.perf_counter()
        try:
            r = httpx.post(url, json={**base, "model": model}, timeout=300)
            body = r.json()
            msg = body.get("message", {})
            calls = msg.get("tool_calls") or []
            detail = f"args={calls[0]['function']['arguments']}" if calls else (
                f"content={msg.get('content', '')[:60]!r}"
            )
            print(f"{model} (run {attempt}): {time.perf_counter() - t0:.1f}s "
                  f"tool_calls={len(calls)} {detail}")
        except Exception as exc:
            print(f"{model} (run {attempt}): LOI {exc}")
