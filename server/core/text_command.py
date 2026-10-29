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
    parse_iso,
    trailing_number,
)

TYPE_ALIASES = {
    "light": ["đèn", "bóng đèn", "đèn trần", "đèn ngủ"],
    "climate": ["điều hòa", "máy lạnh", "dieu hoa", "ac"],
    "switch": ["công tắc"],  # "cầu dao" là alias riêng của thiết bị đo điện
    # sensor chỉ giữ alias đúng với MỌI sensor; "nhiệt độ/độ ẩm" là alias riêng
    # của từng cảm biến — nếu để type-alias thì công tơ điện cũng trúng "nhiệt độ"
    "sensor": ["cảm biến"],
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "switch",
            "description": "Bật hoặc tắt một thiết bị (đèn, quạt, cầu dao, công tắc)",
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
            "name": "press_button",
            "description": "Bấm một nút trên thiết bị điều khiển từ xa (VD: tăng/giảm âm lượng loa, mute, cho quạt xoay)",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "button": {"type": "string", "enum": ["volume_up", "volume_down", "mute", "unmute", "swing_on", "swing_off"]},
                    "room": {"type": "string"},
                },
                "required": ["entity", "button"],
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
            "name": "list_active",
            "description": "Liệt kê các thiết bị đang bật (đèn, công tắc, điều hòa có phản hồi trạng thái thật; không gồm thiết bị điều khiển bằng IR)",
            "parameters": {"type": "object", "properties": {
                "room": {"type": "string", "description": "Phòng nếu hỏi cụ thể, bỏ trống nếu cả nhà"},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Lấy thời tiết hiện tại, hoặc dự báo ngày mai/ngày mốt",
            "parameters": {"type": "object", "properties": {
                "day": {"type": "string", "enum": ["today", "tomorrow", "day_after"],
                        "description": "today=hôm nay, tomorrow=ngày mai, day_after=ngày mốt"},
            }},
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


def _alias_in(alias: str, norm: str) -> bool:
    """Alias phải đứng thành từ riêng trong câu — tránh 'ac' lọt vào 'khách'."""
    return re.search(rf"(?:^|\s){re.escape(alias)}(?:\s|$)", norm) is not None


def _room_from_text(db: Database, norm: str) -> int | None:
    """Explicit room mentioned in the text? Longest alias wins."""
    best: tuple[int, int] | None = None
    for room in db.query("SELECT * FROM rooms"):
        for alias in [room["name"]] + room["aliases"].split(","):
            alias_n = normalize(alias)
            if alias_n and _alias_in(alias_n, norm):
                if best is None or len(alias_n) > best[1]:
                    best = (room["id"], len(alias_n))
    return best[0] if best else None


def _room_where(db: Database, room_id, mentioned_room) -> str:
    """Địa danh đuôi câu trả lời. Mic nằm ở phòng nào thì thiết bị phòng đó là mặc
    định, chỉ nêu phòng khi người nói nói rõ phòng trong lệnh (tên phòng đã sẵn
    chữ "Phòng ..." nên không thêm tiền tố nữa)."""
    if mentioned_room is None or not room_id:
        return ""
    name = _room_name(db, room_id)
    return f" {name.lower()}" if name else ""


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
        # controller = hub luôn bật: khớp như công tắc để act_switch từ chối đúng câu
        if entity_type and entity["entity_type"] != entity_type and not (
            entity_type == "switch" and entity["entity_type"] == "controller"
        ):
            continue
        if entity["entity_type"] == "camera":
            continue
        in_scope = scope_room and _entity_room(db, entity) == scope_room
        for alias in _entity_aliases(entity):
            if _alias_in(alias, norm):
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
    if entity["entity_type"] == "switch":
        # cầu dao tắt -> xóa ngay số đo cũ của Điện năng cùng thiết bị
        from server.core.scheduler import sync_meter_with_switch
        sync_meter_with_switch(db, entity["id"], on=state.get("state") == "on")
    return state


def _entity_room(db: Database, entity) -> int | None:
    """Effective room: the entity's own if set, else its device's."""
    row = db.one(
        """SELECT e.room_id AS own, d.room_id AS dev FROM entities e
           JOIN devices d ON d.id=e.device_id WHERE e.id=?""",
        (entity["id"],),
    )
    if not row:
        return None
    return row["own"] if row["own"] is not None else row["dev"]


async def act_switch(db, drivers, entity, on: bool, mentioned_room=None) -> dict:
    if entity["entity_type"] == "controller" or loads(entity["driver_data"]).get("readonly"):
        return {"reply": "Đây là bộ điều khiển hồng ngoại, không bật/tắt trực tiếp được.",
                "action": {"tool": "switch", "entity": entity["name"], "ok": False}}
    await _set_state(db, drivers, entity, "on" if on else "off", {})
    verb = "bật" if on else "tắt"
    where = _room_where(db, _entity_room(db, entity), mentioned_room)
    return {"reply": f"Đã {verb} {entity['name'].lower()}{where}.",
            "action": {"tool": "switch", "entity": entity["name"], "state": "on" if on else "off", "ok": True}}


BUTTON_PHRASES = {
    "volume_up": "Đã tăng âm lượng",
    "volume_down": "Đã giảm âm lượng",
    "mute": "Đã tắt tiếng",
    "unmute": "Đã mở tiếng",
    "swing_on": "Đã bật chế độ xoay",
    "swing_off": "Đã tắt chế độ xoay",
}

# lệnh và mã IR khác tên: nút mute là toggle nên "mở tiếng" bấm lại chính nó,
# swing của quạt cũng toggle nên bật/tắt xoay cùng bấm một nút
BUTTON_CODES = {"unmute": "mute", "swing_on": "swing", "swing_off": "swing"}


async def act_button(db, drivers, entity, button: str, mentioned_room=None) -> dict:
    """Bấm một nút IR (volume, mute, xoay quạt...) — nút bấm là tương đối, mỗi lệnh = 1 lần bấm."""
    await _set_state(db, drivers, entity, BUTTON_CODES.get(button, button), {})
    where = _room_where(db, _entity_room(db, entity), mentioned_room)
    phrase = BUTTON_PHRASES.get(button)
    reply = f"{phrase} {entity['name'].lower()}{where}." if phrase else f"Đã bấm {button} {entity['name'].lower()}{where}."
    return {"reply": reply, "action": {"tool": "press_button", "entity": entity["name"], "button": button, "ok": True}}


async def act_climate(db, drivers, entity, mode=None, temperature=None, mentioned_room=None) -> dict:
    if mode == "off":
        command, params = "off", {}
    elif mode == "on":
        # "bật" không kèm nhiệt độ: lệnh không nói -> nhiệt mặc định (driver_data)
        # -> lần gần nhất -> 26. Nói rõ nhiệt độ trong lệnh thì luôn thắng.
        dd = loads(entity["driver_data"])
        last = loads(entity["state"]).get("temperature")
        command, params = "set", {"mode": "cool",
                                  "temperature": int(temperature or dd.get("default_temp") or last or 26)}
    else:
        command, params = "set", {"mode": mode or "cool", "temperature": int(temperature or 26)}
    state = await _set_state(db, drivers, entity, command, params)
    where = _room_where(db, _entity_room(db, entity), mentioned_room)
    if mode == "off":
        reply = f"Đã tắt {entity['name'].lower()}{where}."
    else:
        temp = state.get("temperature", params.get("temperature"))
        mode_vi = {"cool": "lạnh", "dry": "khô", "fan": "gió", "heat": "ấm", "auto": "tự động"}.get(
            state.get("mode", params.get("mode", "cool")), "lạnh")
        reply = f"Đã chỉnh {entity['name'].lower()}{where} {mode_vi} {temp} độ."
    return {"reply": reply, "action": {"tool": "set_climate", "entity": entity["name"],
                                       "mode": mode, "temperature": temperature, "ok": True}}


async def act_sensor(db, drivers, entity, room_id=None, metric=None) -> dict:
    # a sensor that has not reported for an hour is treated as unreachable:
    # say so instead of repeating hours-old numbers as if they were current
    updated = parse_iso(entity["updated_at"]) if entity["updated_at"] else None
    if updated and (datetime.now() - updated).total_seconds() > 3600:
        room = _room_name(db, _entity_room(db, entity) or room_id)
        where = f" ở {room.lower()}" if room else ""
        return {"reply": f"Mình không lấy được thông tin từ cảm biến{where} — đã hơn một tiếng không có số liệu mới.",
                "action": {"tool": "get_sensor", "ok": False, "stale": True}}
    device = db.one("SELECT * FROM devices WHERE id=?", (entity["device_id"],))
    state = loads(entity["state"])
    if not state:
        # dummy/virtual sensors compute values on read; zigbee returns the cached state
        try:
            state = await _set_stateless_read(db, drivers, entity, device)
        except Exception:
            state = {}
    room = _room_name(db, _entity_room(db, entity) or room_id)
    if room:
        room = room.removeprefix("Phòng ").removeprefix("phòng ").strip()
    if not state:
        state = {"temperature": None, "humidity": None}
    bits = []
    if state.get("temperature") is not None and metric in (None, "temperature"):
        bits.append(f"nhiệt độ {state['temperature']:.1f} độ C")
    if state.get("humidity") is not None and metric in (None, "humidity"):
        bits.append(f"độ ẩm {state['humidity']:.0f}%")
    if state.get("voltage") is not None and metric in (None, "voltage"):
        bits.append(f"điện áp {state['voltage']:.0f} volt")
    # power stays UI-only: solar homes read near-zero from the grid and
    # answering "0 oát" is more confusing than helpful
    if state.get("energy") is not None and metric in (None, "energy"):
        bits.append(f"tổng dùng {state['energy']:.1f} số điện (kWh)")
    if room:
        reply = f"Phòng {room.lower()}: {', '.join(bits)}." if bits else f"Phòng {room.lower()} chưa có số liệu."
    elif bits:
        body = ", ".join(bits)
        reply = body[0].upper() + body[1:] + "."
    else:
        missing = {"temperature": "nhiệt độ", "humidity": "độ ẩm"}.get(metric)
        reply = f"Cảm biến chưa có số liệu {missing}." if missing else "Cảm biến chưa có số liệu."
    return {"reply": reply,
            "action": {"tool": "get_sensor", "ok": bool(bits), "state": state}}


def weather_day(norm: str) -> int:
    """Day offset the text asks about: 0 today (default), 1 tomorrow, 2 day after.
    ("mốt" normalizes to "mot"; bare "mot" collides with số một so require "ngay mot".)"""
    if re.search(r"\b(ngay mai|ngay kia|mai)\b", norm):
        return 1
    if re.search(r"\bngay mot\b", norm):
        return 2
    return 0


def rain_question(norm: str) -> bool:
    """Asking specifically about rain ("có mưa không", "mưa nhiều không"...)."""
    return bool(re.search(r"co mua|kha nang mua|mua (khong|hay khong|roi|nhieu)", norm))


def evening_question(norm: str) -> bool:
    """Asking about tonight ("tối nay", "buổi tối", "đêm") - not bare "tối"
    because tone-stripped "tôi" normalizes to the same "toi"."""
    return bool(re.search(r"\b(toi nay|buoi toi|dem)\b", norm))


def sensor_metric(norm: str) -> str | None:
    """Which metric the text asked about; None = both / unspecified."""
    has_temp, has_hum = "nhiet do" in norm, "do am" in norm
    if has_temp and not has_hum:
        return "temperature"
    if has_hum and not has_temp:
        return "humidity"
    if re.search(r"\bdien ap\b|\bvol\b", norm):
        return "voltage"
    if re.search(r"\bso dien\b|\bdien nang\b|\bkwh\b", norm):
        return "energy"
    return None


RAIN_GRADES = ((70, "rất cao"), (40, "khá cao"), (20, "thấp"), (0, "rất thấp"))


def act_weather(weather, day: int = 0, rain: bool = False, evening: bool = False) -> dict:
    try:
        if rain:
            label = {0: "Tối nay" if evening else "Hôm nay", 1: "Ngày mai", 2: "Ngày mốt"}.get(day, "")
            prob = weather.rain(day, evening)
            if prob is not None:
                grade = next(text for floor, text in RAIN_GRADES if prob >= floor)
                only = "chỉ " if prob < 20 else ""
                reply = f"{label} khả năng mưa {grade}, {only}{prob:.0f}%."
                if prob == 0:
                    reply = f"{label} không mưa, 0%."
                return {"reply": reply,
                        "action": {"tool": "get_weather", "ok": True, "day": day, "rain": prob}}
        return {"reply": weather.summary(day),
                "action": {"tool": "get_weather", "ok": True, "day": day}}
    except Exception as exc:
        return {"reply": f"Mình không lấy được thời tiết: {exc}",
                "action": {"tool": "get_weather", "ok": False, "day": day}}


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


def volume_button(norm: str) -> str | None:
    """Lệnh chỉnh âm lượng loa? Trả về volume_up/volume_down/mute hay None.
    Chỉ nhận khi câu nhắc đến loa/nhạc/âm lượng để tránh hiểu nhầm "quạt to lên".
    Nút mute của remote là toggle nên "mở tiếng" cũng bấm mute lần nữa."""
    if not re.search(r"\b(am luong|volume|loa|kanto|nhac|mut|mute)\b|(tat|mo|bat) tieng", norm):
        return None
    if re.search(r"\b(mo|bat) tieng\b|\bunmut\b|bo mut", norm):
        return "mute"  # toggle lại
    if re.search(r"tat tieng|\bmut\b|\bmute\b|tat am luong", norm):
        return "mute"
    if re.search(r"\b(tang|to len|to hon|to ra)\b|tang am luong|am luong to", norm):
        return "volume_up"
    if re.search(r"\b(giam|nho di|nho hon|nho xuong|nho lai)\b|giam am luong|am luong nho", norm):
        return "volume_down"
    return None


def act_list_active(db: Database, room_id=None, mentioned_room=None) -> dict:
    """"Cái gì đang bật?" — chỉ liệt kê thiết bị có phản hồi trạng thái thật;
    thiết bị IR (broadlink) không biết mình bật/tắt nên không bao giờ xuất hiện."""
    rooms = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM rooms")}
    rows = db.query(
        """SELECT e.name, e.room_id, e.state, d.room_id AS device_room
           FROM entities e JOIN devices d ON d.id=e.device_id
           WHERE e.entity_type IN ('light','switch','climate') AND d.driver!='broadlink'"""
    )
    on = []
    for e in rows:
        try:
            state = json.loads(e["state"] or "{}")
        except ValueError:
            state = {}
        if state.get("state") != "on":
            continue
        eff = e["room_id"] or e["device_room"]
        if room_id and eff != room_id:
            continue
        on.append((e["name"], rooms.get(eff, "")))
    where = _room_where(db, room_id, mentioned_room)
    if not on:
        return {"reply": f"Hiện không có gì đang bật{where}.",
                "action": {"tool": "list_active", "ok": True}}
    if room_id:  # đã scoped theo phòng — tên thiết bị là đủ
        names = ", ".join(n.lower() for n, _ in on)
        return {"reply": f"Đang bật{where}: {names}.",
                "action": {"tool": "list_active", "ok": True}}
    items = ", ".join(f"{n.lower()} ({r.lower()})" if r else n.lower() for n, r in on)
    return {"reply": f"Đang bật: {items}.", "action": {"tool": "list_active", "ok": True}}


def swing_button(norm: str) -> str | None:
    """Lệnh quạt xoay qua lại / đứng yên? Nút swing trên remote là toggle.
    "Xoay" là từ riêng của quạt trong nhà này nên không cần cửa từ khóa."""
    if re.search(r"co dinh|dung xoay|dung quay|khong xoay|khong quay|dung yen", norm):
        return "swing_off"
    if re.search(r"\bxoay\b|quay qua lai", norm):
        return "swing_on"
    return None


async def fast_path(db, drivers, weather, text: str, room_id: int | None):
    norm = normalize(text)
    tokens = norm.split()
    has_on = bool(re.search(r"\b(bat|mo|bat len)\b", norm))
    has_off = bool(re.search(r"\b(tat|tat di)\b", norm))

    # multi-part sentences -> let the LLM handle them. "Rồi" as an aspect
    # particle ("đã dùng bao nhiêu số điện rồi") is not a connector: with no
    # on/off verb the sentence is still a single question.
    if (has_on and has_off) or re.search(r"\bsau do\b", norm) or (
            re.search(r"\broi\b", norm) and (has_on or has_off)):
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

    # weather (rain questions included: "ngày mai có mưa không") — single-intent
    # only, multi-part sentences go to the LLM so no half gets dropped
    if not multi_intent and re.search(
        r"thoi tiet|du bao|kha nang mua|co mua|mua (khong|hay khong|roi|nhieu)", norm
    ):
        return act_weather(weather, weather_day(norm),
                           rain=rain_question(norm), evening=evening_question(norm))

    # power questions: solar home, grid draw is ~0 W and misleading - trợ lý
    # không đọc số điện bằng giọng nói; Vol/kW nằm trên tab Tổng quan
    if not (has_on or has_off) and re.search(
        r"\bcong suat\b|\bkw\b|tieu thu|\bso dien\b|\bdien nang\b|\bkwh\b|\bdien ap\b|\bvol\b", norm
    ):
        return {"reply": "Nhà mình dùng điện mặt trời nên các số đo từ lưới thường rất thấp "
                         "và không nói lên nhiều.",
                "action": None}

    # temperature/humidity question without a verb -> sensor
    if not (has_on or has_off) and re.search(r"nhiet do|do am", norm):
        entity = resolve_entity(db, norm, room_id, "sensor")
        if entity:
            return await act_sensor(db, drivers, entity, room_id, sensor_metric(norm))

    # "cái gì đang bật?" — câu hỏi liệt kê, đặt TRƯỚC nhánh on/off vì chứa từ "bật";
    # thiết bị IR không có trạng thái thật nên act_list_active tự loại khỏi danh sách
    if re.search(r"\b(dang bat|dang mo|dang chay)\b", norm) and re.search(r"\b(gi|nao)\b", norm):
        mentioned = _room_from_text(db, norm)
        return act_list_active(db, mentioned or room_id, mentioned_room=mentioned)

    # âm lượng loa: đặt TRƯỚC nhánh on/off vì "tắt tiếng" chứa từ "tắt" —
    # không tắt nguồn loa; up/down chỉ nhận khi câu không có bật/tắt
    vol = volume_button(norm)
    if vol and (vol == "mute" or not (has_on or has_off)):
        entity = resolve_entity(db, norm, room_id) or resolve_entity(db, "loa", room_id)
        if entity:
            return await act_button(db, drivers, entity, vol,
                                    mentioned_room=_room_from_text(db, norm))

    # quạt xoay/cố định: nút swing là toggle — "cho quạt xoay", "quạt cố định";
    # động từ bật/tắt thắng thế ("tắt xoay quạt" = khóa xoay, không tắt quạt).
    # Câu hỏi ("quạt có xoay không?") không bấm gì cả.
    sw = swing_button(norm)
    if sw and not re.search(r"\b(khong|chua|chua)\b\s*[?.!]?\s*$", norm):
        if has_off:
            sw = "swing_off"
        elif has_on:
            sw = "swing_on"
        entity = resolve_entity(db, norm, room_id) or resolve_entity(db, "quạt", room_id)
        if entity:
            return await act_button(db, drivers, entity, sw,
                                    mentioned_room=_room_from_text(db, norm))

    # climate with/without temperature
    if has_on or has_off:
        temp = _temp_from_text(norm)
        mentioned = _room_from_text(db, norm)
        entity = resolve_entity(db, norm, room_id, "climate")
        if entity:
            if has_off:
                return await act_climate(db, drivers, entity, mode="off", mentioned_room=mentioned)
            return await act_climate(db, drivers, entity, mode="cool" if temp else "on",
                                     temperature=temp, mentioned_room=mentioned)
        entity = resolve_entity(db, norm, room_id, "light")
        if entity:
            return await act_switch(db, drivers, entity, on=has_on, mentioned_room=mentioned)
        entity = resolve_entity(db, norm, room_id, "switch")
        if entity:
            return await act_switch(db, drivers, entity, on=has_on, mentioned_room=mentioned)
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
        room = rooms.get(_entity_room(db, e), "chưa gán phòng")
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


async def execute_tool(db, drivers, weather, name: str, args: dict, room_id,
                       day_hint: int = 0) -> dict:
    try:
        if name == "switch":
            entity = resolve_entity(db, args.get("entity", ""), room_id)
            if not entity:
                return {"reply": f"Mình không tìm thấy thiết bị '{args.get('entity')}'.",
                        "action": {"tool": name, "ok": False}}
            # LLM nêu phòng (param room hoặc lồng trong entity) = người nói nói rõ phòng
            mentioned = _room_from_text(db, normalize(f"{args.get('entity', '')} {args.get('room') or ''}"))
            return await act_switch(db, drivers, entity, args.get("state") == "on", mentioned_room=mentioned)

        if name == "press_button":
            entity = resolve_entity(db, args.get("entity", ""), room_id)
            if not entity:
                return {"reply": f"Mình không tìm thấy thiết bị '{args.get('entity')}'.",
                        "action": {"tool": name, "ok": False}}
            mentioned = _room_from_text(db, normalize(f"{args.get('entity', '')} {args.get('room') or ''}"))
            return await act_button(db, drivers, entity, args.get("button", ""), mentioned_room=mentioned)

        if name == "set_climate":
            entity = resolve_entity(db, args.get("entity") or "điều hòa", room_id, "climate")
            if not entity:
                entity = resolve_entity(db, args.get("entity") or "điều hòa", room_id)
            if not entity:
                return {"reply": "Mình không tìm thấy điều hòa nào.", "action": {"tool": name, "ok": False}}
            mentioned = _room_from_text(db, normalize(f"{args.get('entity', '')} {args.get('room') or ''}"))
            return await act_climate(
                db, drivers, entity,
                mode=args.get("mode"), temperature=args.get("temperature"), mentioned_room=mentioned,
            )

        if name == "list_active":
            room_arg = str(args.get("room") or "").strip()
            scope = _room_from_text(db, normalize(room_arg)) if room_arg else room_id
            return act_list_active(db, scope, mentioned_room=True if room_arg else None)

        if name == "get_sensor":
            metric_arg = str(args.get("metric", ""))
            metric = ("temperature" if "temp" in metric_arg else
                      "humidity" if "hum" in metric_arg else
                      "voltage" if "volt" in metric_arg else
                      "energy" if "energy" in metric_arg or "kwh" in metric_arg else None)
            # English metric names never match Vietnamese aliases - fall back
            entity = resolve_entity(db, metric_arg or "cảm biến", room_id, "sensor") \
                or resolve_entity(db, "cảm biến", room_id, "sensor")
            if not entity:
                return {"reply": "Phòng này chưa có cảm biến.", "action": {"tool": name, "ok": False}}
            return await act_sensor(db, drivers, entity, room_id, metric)

        if name == "get_weather":
            # an explicit day in the user's text outranks what the model passed
            day = day_hint or {"tomorrow": 1, "day_after": 2}.get(str(args.get("day", "")), 0)
            return act_weather(weather, day)

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
        "Khi hỏi thời tiết ngày mai/mốt, gọi get_weather với day tương ứng. "
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
            outcome = await execute_tool(db, drivers, weather, name, args, room_id,
                                         day_hint=weather_day(normalize(text)))
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
        extra = act_weather(weather, weather_day(norm),
                            rain=rain_question(norm), evening=evening_question(norm))
        replies.append(extra["reply"])
        actions.append(extra["action"])
    if re.search(r"nhiet do|do am", norm) and "get_sensor" not in executed:
        sensor = resolve_entity(db, norm, room_id, "sensor")
        if sensor:
            extra = await act_sensor(db, drivers, sensor, room_id, sensor_metric(norm))
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
