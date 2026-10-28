"""Which tool-message format does Ollama accept for round 2? (30s timeout each)"""
import json

import httpx

url = "http://127.0.0.1:11434/api/chat"

TOOLS = [
    {"type": "function", "function": {
        "name": "set_climate", "description": "Bật/tắt/chỉnh điều hòa",
        "parameters": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["cool", "off"]},
            "temperature": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "get_weather", "description": "Lấy thời tiết",
        "parameters": {"type": "object", "properties": {}}}},
]

messages = [
    {"role": "system", "content": "Bạn là trợ lý nhà thông minh tiếng Việt. Trả lời ngắn gọn."},
    {"role": "user", "content": "mở máy lạnh và cho tôi biết có mưa không"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"function": {"name": "set_climate", "arguments": {"mode": "cool"}}}]},
]

variants = {
    "name": {"role": "tool", "name": "set_climate", "content": "Không tìm thấy điều hòa."},
    "tool_name": {"role": "tool", "tool_name": "set_climate", "content": "Không tìm thấy điều hòa."},
    "content_only": {"role": "tool", "content": "Không tìm thấy điều hòa."},
}

for label, tool_msg in variants.items():
    try:
        r = httpx.post(url, json={"model": "qwen2.5:3b-instruct",
                                  "messages": messages + [tool_msg],
                                  "tools": TOOLS, "stream": False, "think": False},
                       timeout=45)
        msg = r.json().get("message", {})
        calls = msg.get("tool_calls") or []
        print(f"{label:14} -> {r.status_code} tool_calls={len(calls)} "
              f"content={msg.get('content', '')[:60]!r}")
    except Exception as exc:
        print(f"{label:14} -> TIMEOUT/LOI: {type(exc).__name__}")
