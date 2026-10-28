"""Text command understanding: fast path (regex table) then LLM (Ollama tools).

handle_command() is shared by the admin console, the WebSocket voice gateway
and the timer scheduler, so all entry points behave the same.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta

import httpx

from server.config import Config
from server.db import Database
from server.drivers.base import DriverError, loads
from server.util import (
    hhmm_adjusted,
    next_hhmm,
    normalize,
    now_iso,
    trailing_number,
)

TYPE_ALIASES = {
    "light": ["đèn", "bóng đèn", "đèn trần", "đèn ngủ"],
    "climate": ["điều hòa", "máy lạnh", "dieu hoa", "ac"],
    "switch": ["công tắc", "ổ cắm", "ổ cắm thông minh"],
    "sensor": ["cảm biến", "nhiệt độ", "độ ẩm"],
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "switch",
            "description": "Bật hoặc tắt một thiết bị (đèn, quạt, ổ cắm, công tắc)",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Tên thiết bị, ví dụ: quạt, đèn trần"},
                    "state": {"type": "string", "enum": ["on", "off"]},
                    "room": {"type": "string", "description": "Phòng nếu khác phòng người nói"},
                },
                "required": ["entity", "state"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_climate",
            "description": "Chỉnh điều hòa: bật/tắt, chế độ và nhiệt độ",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "mode": {"type": "string", "enum": ["cool", "dry", "fan", "heat", "auto", "off", "on"]},
                    "temperature": {"type": "integer", "minimum": 16, "maximum": 30},
                    "room": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sensor",
            "description": "Đọc nhiệt độ/độ ẩm từ cảm biến trong phòng",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "enum": ["temperature", "humidity"]},
                    "room": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Lấy thời tiết hiện tại và dự báo hôm nay",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": "Hẹn giờ nhắc nhở. Cho một trong hai: minutes (số phút tới) hoặc time (giờ dạng HH:MM)",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "number", "description": "Số phút kể từ bây giờ"},
                    "time": {"type": "string", "description": "Giờ cụ thể HH:MM (24h)"},
                    "message": {"type": "string", "description": "Việc cần nhắc"},
                },
                "required": ["message"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Entity / room resolution


def _room_name(db: Database, room_id) -> str:
    if not room_id:
        return ""
    row = db.one("SELECT name FROM rooms WHERE id=?", (room_id,))
    return row["name"] if row else ""


def _room_from_text(db: Database, norm: str) -> int | None:
    """Explicit room mentioned in the text? Longest alias wins."""
    best: tuple[int, int] | None = None
    for room in db.query("SELECT * FROM rooms"):
        for alias in [room["name"]] + room["aliases"].split(","):
            alias_n = normalize(alias)
            if alias_n and alias_n in norm:
                if best is None or len(alias_n) > best[1]:
                    best = (room["id"], len(alias_n))
    return best[0] if best else None


def _entity_aliases(entity) -> list[str]:
    aliases = [entity["name"], *entity["aliases"].split(",")]
    aliases += TYPE_ALIASES.get(entity["entity_type"], [])
    return [normalize(a) for a in aliases if normalize(a)]


def resolve_entity(db: Database, text: str, room_id=None, entity_type=None):
    """Longest alias match anywhere in the (normalized) text; prefer the given room."""
    norm = normalize(text)
    scope_room = room_id
    mentioned = _room_from_text(db, norm)
    if mentioned:
        scope_room = mentioned
    ranked: list[tuple[int, dict]] = []
    for entity in db.query("SELECT * FROM entities"):
        if entity_type and entity["entity_type"] != entity_type:
            continue
        if entity["entity_type"] == "camera":
            continue
        device = db.one("SELECT room_id FROM devices WHERE id=?", (entity["device_id"],))
        in_scope = device and scope_room and device["room_id"] == scope_room
        for alias in _entity_aliases(entity):
            if alias in norm:
                ranked.append((len(alias) + (100 if in_scope else 0), dict(entity)))
                break
    if not ranked:
        return None
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return ranked[0][1]


# ---------------------------------------------------------------------------
# Action execution (shared by fast path, LLM tools and the admin UI)


async def _set_stateless_read(db, drivers, entity, device) -> dict:
    driver = drivers.get(device["driver"])
    if not driver:
        raise DriverError(f"Không có driver '{device['driver']}'")
    state = await asyncio.to_thread(driver.get_state, entity, device)
    db.execute(
        "UPDATE entities SET state=?, updated_at=? WHERE id=?",
        (json.dumps(state, ensure_ascii=False), now_iso(), entity["id"]),
    )
    return state


async def _set_state(db, drivers, entity, command, params) -> dict:
    device = db.one("SELECT * FROM devices WHERE id=?", (entity["device_id"],))
    driver = drivers.get(device["driver"])
    if not driver:
        raise DriverError(f"Không có driver '{device['driver']}'")
    state = await asyncio.to_thread(driver.set_state, entity, device, command, params)
    db.execute(
        "UPDATE entities SET state=?, updated_at=? WHERE id=?",
        (json.dumps(state, ensure_ascii=False), now_iso(), entity["id"]),
    )
    return state


def _entity_room(db: Database, entity) -> int | None:
    row = db.one("SELECT room_id FROM devices WHERE id=?", (entity["device_id"],))
    return row["room_id"] if row else None


async def act_switch(db, drivers, entity, on: bool) -> dict:
    await _set_state(db, drivers, entity, "on" if on else "off", {})
    verb = "bật" if on else "tắt"
    room = _entity_room(db, entity)
    where = f" phòng {_room_name(db, room).lower()}" if room else ""
    return {"reply": f"Đã {verb} {entity['name'].lower()}{where}.",
            "action": {"tool": "switch", "entity": entity["name"], "state": "on" if on else "off", "ok": True}}


async def act_climate(db, drivers, entity, mode=None, temperature=None) -> dict:
    if mode == "off":
        command, params = "off", {}
    elif mode == "on":
        command, params = "on", {}
    else:
        command, params = "set", {"mode": mode or "cool", "temperature": int(temperature or 26)}
    state = await _set_state(db, drivers, entity, command, params)
    room = _entity_room(db, entity)
    where = f" phòng {_room_name(db, room).lower()}" if room else ""
    if mode == "off":
        reply = f"Đã tắt {entity['name'].lower()}{where}."
    else:
        temp = state.get("temperature", params.get("temperature"))
        mode_vi = {"cool": "lạnh", "dry": "khô", "fan": "gió", "heat": "ấm", "auto": "tự động"}.get(
            state.get("mode", params.get("mode", "cool")), "lạnh")
        reply = f"Đã chỉnh {entity['name'].lower()}{where} {mode_vi} {temp} độ."
    return {"reply": reply, "action": {"tool": "set_climate", "entity": entity["name"],
                                       "mode": mode, "temperature": temperature, "ok": True}}


async def act_sensor(db, drivers, entity, room_id=None) -> dict:
    device = db.one("SELECT * FROM devices WHERE id=?", (entity["device_id"],))
    state = loads(entity["state"])
    if not state:
        # dummy/virtual sensors compute values on read; zigbee returns the cached state
        try:
            state = await _set_stateless_read(db, drivers, entity, device)
        except Exception:
            state = {}
    room = _room_name(db, _entity_room(db, entity) or room_id)
    where = f" ở {room.lower()}" if room else ""
    if not state:
        state = {"temperature": None, "humidity": None}
    if state.get("temperature") is None and state.get("humidity") is None:
        return {"reply": f"Cảm biến{where} chưa có số liệu.",
                "action": {"tool": "get_sensor", "ok": False}}
    bits = []
    if state.get("temperature") is not None:
        bits.append(f"nhiệt độ {state['temperature']:.1f} độ C")
    if state.get("humidity") is not None:
        bits.append(f"độ ẩm {state['humidity']:.0f}%")
    return {"reply": f"Phòng {room.lower()}: {', '.join(bits)}." if room else ", ".join(bits).capitalize() + ".",
            "action": {"tool": "get_sensor", "ok": True, "state": state}}


def act_weather(weather) -> dict:
    try:
        return {"reply": weather.summary(), "action": {"tool": "get_weather", "ok": True}}
    except Exception as exc:
        return {"reply": f"Mình không lấy được thời tiết: {exc}",
                "action": {"tool": "get_weather", "ok": False}}


def act_timer(db, room_id, message: str, minutes=None, time_str=None) -> dict:
    if time_str:
        hh, mm = time_str.split(":")[:2]
        target = next_hhmm(int(hh), int(mm))
    elif minutes:
        target = datetime.now() + timedelta(minutes=float(minutes))
    else:
        return {"reply": "Mình chưa rõ thời gian hẹn.", "action": {"tool": "set_timer", "ok": False}}
    message = message.strip() or "hẹn giờ"
    db.execute(
        "INSERT INTO timers(target_at, room_id, message, created_at) VALUES (?,?,?,?)",
        (target.isoformat(timespec="seconds"), room_id, message, now_iso()),
    )
    clock = target.strftime("%H:%M")
    in_ = f" (sau {int(minutes)} phút nữa)" if minutes and not time_str else ""
    return {"reply": f"Ok, {clock}{in_} mình nhắc bạn: {message}.",
            "action": {"tool": "set_timer", "ok": True, "at": clock}}


# ---------------------------------------------------------------------------
# Fast path


def _temp_from_text(norm: str):
    m = re.search(r"\b(\d{2})\s*(?:do c|do|°)\b", norm)
    if m:
        return int(m.group(1))
    tokens = norm.split()
    for i, tok in enumerate(tokens):
        if tok in ("do", "do c", "°c") and i > 0:
            got = trailing_number(tokens[:i])
            if got and 16 <= got[0] <= 32:
                return int(got[0])
    return None


def _parse_timer(norm: str, raw: str):
    """Return (minutes|None, time_str|None, message)."""
    tokens = norm.split()

    # clock time: "15:30" or "3 giờ chiều"
    hhmm = re.search(r"\b(\d{1,2}):(\d{2})\b", norm)
    if hhmm:
        hour, minute = int(hhmm.group(1)), int(hhmm.group(2))
        tail = norm[hhmm.end():]
        part = re.match(r"\s*(chieu|toi|dem)", tail)
        hour = hhmm_adjusted(hour, minute, part.group(1) if part else None)
    else:
        word_clock = re.search(r"\b(\d{1,2})\s*gio\s*(chieu|toi|sang|dem)\b", norm)
        if not word_clock:
            word_clock = re.search(r"\b(\d{1,2})\s*gio\s+(ruoi)\b", norm)  # "3 giờ rưỡi"
        if word_clock:
            hour = hhmm_adjusted(int(word_clock.group(1)), 30, word_clock.group(2) if word_clock.group(2) != "ruoi" else None)
            minute = 30 if word_clock.group(2) == "ruoi" else 0
        else:
            hour = None
    if hour is not None:
        message = re.split(r"(?:nhắc|hẹn|báo)\s*(?:tôi|em|mình|tui)?", raw, flags=re.I)
        return None, f"{hour:02d}:{minute:02d}", message[-1].strip() if len(message) > 1 else "hẹn giờ"

    # "<số> phút/giờ nữa" duration
    for i, tok in enumerate(tokens):
        if tok in ("phut", "gio", "tieng") or tok.startswith("gio:"):
            got = trailing_number(tokens[:i])
            if not got:
                continue
            minutes = got[0] if tok == "phut" else got[0] * 60
            message = re.split(r"(?:nhắc|hẹn|báo)\s*(?:tôi|em|mình|tui)?", raw, flags=re.I)
            return minutes, None, message[-1].strip() if len(message) > 1 else "hẹn giờ"
    return None, None, None


async def fast_path(db, drivers, weather, text: str, room_id: int | None):
    norm = normalize(text)
    tokens = norm.split()
    has_on = bool(re.search(r"\b(bat|mo|bat len)\b", norm))
    has_off = bool(re.search(r"\b(tat|tat di)\b", norm))

    # multi-part sentences -> let the LLM handle them
    if (has_on and has_off) or re.search(r"\broi\b|\bsau do\b", norm):
        return None

    # timers: fast shortcut unless the sentence is clearly multi-intent
    # ("bật đèn và 15 phút nữa nhắc tôi..." needs the LLM to do both)
    timerish = re.search(r"\b(nhac|hen|bao)\b", norm) or re.search(
        r"\d+\s*(phut|gio|tieng)|\d{1,2}:\d{2}", norm
    )
    multi_intent = re.search(r"\broi\b|\bsau do\b|\bva\b|\bva ca\b|\bthem\b|\bngoai ra\b", norm)
    if timerish and not multi_intent:
        minutes, time_str, message = _parse_timer(norm, text)
        if minutes or time_str:
            return act_timer(db, room_id, message or "hẹn giờ", minutes=minutes, time_str=time_str)

    # weather
    if "thoi tiet" in norm or "du bao" in norm:
        return act_weather(weather)

    # temperature question without a verb -> sensor
    if not (has_on or has_off) and ("nhiet do" in norm or "do am" in norm):
        entity = resolve_entity(db, norm, room_id, "sensor")
        if entity:
            return await act_sensor(db, drivers, entity, room_id)

    # climate with/without temperature
    if has_on or has_off:
        temp = _temp_from_text(norm)
        entity = resolve_entity(db, norm, room_id, "climate")
        if entity:
            if has_off:
                return await act_climate(db, drivers, entity, mode="off")
            return await act_climate(db, drivers, entity, mode="cool" if temp else "on", temperature=temp)
        entity = resolve_entity(db, norm, room_id, "light")
        if entity:
            return await act_switch(db, drivers, entity, on=has_on)
        entity = resolve_entity(db, norm, room_id, "switch")
        if entity:
            return await act_switch(db, drivers, entity, on=has_on)
        return None

    return None


# ---------------------------------------------------------------------------
# LLM path (Ollama)


def _llm_context(db: Database, room_id) -> str:
    rooms = {r["id"]: r["name"] for r in db.query("SELECT * FROM rooms")}
    lines = ["Các phòng: " + (", ".join(rooms.values()) or "(chưa có)")]
    if room_id and room_id in rooms:
        lines.append(f"Người nói đang ở: {rooms[room_id]}")
    for e in db.query("SELECT * FROM entities"):
        device = db.one("SELECT room_id FROM devices WHERE id=?", (e["device_id"],))
        room = rooms.get(device["room_id"], "chưa gán phòng") if device else "?"
        lines.append(f"- {e['name']} ({e['entity_type']}, phòng {room})")
    return "\n".join(lines)


async def _call_ollama(cfg: Config, messages: list) -> dict:
    payload = {
        "model": cfg.get("llm.model", "qwen3:4b"),
        "messages": messages,
        "tools": TOOLS,
        "stream": False,
        # qwen3 "thinks" by default: slow on small GPUs and tool calls stop
        # coming back; older Ollama builds reject the flag, so retry without
        "think": False,
    }
    url = cfg.get("llm.base_url", "http://127.0.0.1:11434").rstrip("/") + "/api/chat"
    async with httpx.AsyncClient(timeout=float(cfg.get("llm.timeout", 30))) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code == 400:
            payload.pop("think", None)
            resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


async def execute_tool(db, drivers, weather, name: str, args: dict, room_id) -> dict:
    try:
        if name == "switch":
            entity = resolve_entity(db, args.get("entity", ""), room_id)
            if not entity:
                return {"reply": f"Mình không tìm thấy thiết bị '{args.get('entity')}'.",
                        "action": {"tool": name, "ok": False}}
            return await act_switch(db, drivers, entity, args.get("state") == "on")

        if name == "set_climate":
            entity = resolve_entity(db, args.get("entity") or "điều hòa", room_id, "climate")
            if not entity:
                entity = resolve_entity(db, args.get("entity") or "điều hòa", room_id)
            if not entity:
                return {"reply": "Mình không tìm thấy điều hòa nào.", "action": {"tool": name, "ok": False}}
            return await act_climate(
                db, drivers, entity,
                mode=args.get("mode"), temperature=args.get("temperature"),
            )

        if name == "get_sensor":
            entity = resolve_entity(db, args.get("metric", "") or "cảm biến", room_id, "sensor")
            if not entity:
                return {"reply": "Phòng này chưa có cảm biến.", "action": {"tool": name, "ok": False}}
            return await act_sensor(db, drivers, entity, room_id)

        if name == "get_weather":
            return act_weather(weather)

        if name == "set_timer":
            return act_timer(
                db, room_id, args.get("message", "hẹn giờ"),
                minutes=args.get("minutes"), time_str=args.get("time"),
            )

        return {"reply": f"Công cụ '{name}' chưa được hỗ trợ.", "action": {"tool": name, "ok": False}}
    except DriverError as exc:
        return {"reply": str(exc), "action": {"tool": name, "ok": False}}
    except Exception as exc:
        return {"reply": f"Lỗi khi thực hiện: {exc}", "action": {"tool": name, "ok": False}}


async def llm_path(db, drivers, cfg, weather, text: str, room_id) -> dict:
    system = (
        "Bạn là trợ lý nhà thông minh tiếng Việt. "
        f"Thời gian hiện tại: {datetime.now().strftime('%H:%M, %d/%m/%Y')}.\n"
        f"{_llm_context(db, room_id)}\n"
        "Ưu tiên gọi công cụ để điều khiển thiết bị. Chỉ trả lời văn bản khi không có công cụ phù hợp. "
        "Nếu câu có nhiều việc, hãy gọi TẤT CẢ các công cụ cần thiết trong cùng một lượt trả lời. "
        "Trả lời ngắn gọn, thân thiện."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]

    # Tool-calling loop: execute each round of calls, feed the results back and
    # let the model continue ("mở máy lạnh VÀ cho tôi biết thời tiết" often
    # arrives as one call per turn). Replies from every round are combined.
    replies, actions = [], []
    content = ""
    for _round in range(4):
        try:
            result = await _call_ollama(cfg, messages)
        except Exception as exc:
            if replies:
                break  # already did something useful; answer with what we have
            return {
                "reply": f"Mình chưa hiểu câu này, và không gọi được mô hình ngôn ngữ ({exc}).",
                "actions": [], "engine": "llm-error",
            }

        message = result.get("message", {})
        content = (message.get("content") or "").strip()
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            break

        # echo the assistant turn so the tool results pair up correctly
        messages.append({"role": "assistant", "content": message.get("content") or "",
                         "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            outcome = await execute_tool(db, drivers, weather, name, args, room_id)
            replies.append(outcome["reply"])
            actions.append(outcome.get("action", {"tool": name}))
            messages.append({
                "role": "tool",
                "name": name,
                "tool_name": name,  # ollama builds vary on the field name
                "content": json.dumps(
                    {"reply": outcome["reply"], "ok": outcome.get("action", {}).get("ok", True)},
                    ensure_ascii=False,
                ),
            })

    # Deterministic safety net: small models sometimes answer only the first
    # half of a multi-part sentence. If the text asked about weather/sensors
    # and nothing answered that part yet, do it here.
    norm = normalize(text)
    executed = {a.get("tool") for a in actions if isinstance(a, dict)}
    if re.search(r"thoi tiet|du bao|kha nang mua|mua (khong|hay khong|roi|nhieu)", norm) \
            and "get_weather" not in executed:
        extra = act_weather(weather)
        replies.append(extra["reply"])
        actions.append(extra["action"])
    if re.search(r"nhiet do|do am", norm) and "get_sensor" not in executed:
        sensor = resolve_entity(db, norm, room_id, "sensor")
        if sensor:
            extra = await act_sensor(db, drivers, sensor, room_id)
            replies.append(extra["reply"])
            actions.append(extra.get("action", {"tool": "get_sensor"}))

    if replies:
        return {"reply": " ".join(replies), "actions": actions, "engine": "llm"}
    if content:
        return {"reply": content, "actions": [], "engine": "llm"}
    return {"reply": "Mình chưa hiểu ý bạn.", "actions": [], "engine": "llm"}


# ---------------------------------------------------------------------------


async def handle_command(db: Database, drivers, cfg: Config, weather, text: str, room_id) -> dict:
    """Main entry: returns {reply, actions, engine}. Never raises for user mistakes."""
    text = text.strip()
    if not text:
        return {"reply": "Mình không nghe thấy gì cả.", "actions": [], "engine": "none"}

    try:
        outcome = await fast_path(db, drivers, weather, text, room_id)
        if outcome:
            return {"reply": outcome["reply"], "actions": [outcome["action"]], "engine": "fast"}
    except DriverError as exc:
        return {"reply": str(exc), "actions": [], "engine": "fast"}
    except Exception as exc:
        return {"reply": f"Lỗi khi thực hiện: {exc}", "actions": [], "engine": "fast"}

    return await llm_path(db, drivers, cfg, weather, text, room_id)
