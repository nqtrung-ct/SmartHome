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
from server.core.lunar import can_chi_year, solar_to_lunar
from server.db import Database
from server.drivers.base import DriverError, loads
from server.util import (
    hhmm_adjusted,
    next_hhmm,
    next_repeat,
    normalize,
    now,
    now_iso,
    parse_iso,
    trailing_number,
)

TYPE_ALIASES = {
    "light": ["đèn", "bóng đèn", "đèn trần", "đèn ngủ"],
    "climate": ["điều hòa", "máy lạnh", "dieu hoa", "ac"],
    "switch": ["công tắc"],  # "cầu dao" là alias riêng của thiết bị đo điện
    "fan": ["quạt"],
    "media": ["loa"],
    # sensor chỉ giữ alias đúng với MỌI sensor; "nhiệt độ/độ ẩm" là alias riêng
    # của từng cảm biến — nếu để type-alias thì công tơ điện cũng trúng "nhiệt độ"
    "sensor": ["cảm biến"],
}

FAN_VI = {"quiet": "yên lặng", "low": "thấp", "mid": "vừa", "high": "cao", "auto": "tự động"}

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
            "name": "camera_preset",
            "description": "Quay camera quay được (PTZ) về một góc đã lưu theo tên, ví dụ: quay camera về 'bếp'",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Tên camera nếu nói rõ"},
                    "preset": {"type": "string", "description": "Tên góc đã lưu, ví dụ: bàn ăn, bếp"},
                    "room": {"type": "string", "description": "Phòng nếu khác phòng người nói"},
                },
                "required": ["preset"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "camera_person",
            "description": "Kiểm tra camera có nhìn thấy người không (qua Frigate). Có thể quay tới góc đã lưu hoặc quét tất cả các góc rồi báo lại",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Tên camera nếu nói rõ"},
                    "preset": {"type": "string", "description": "Tên góc đã lưu để quay tới trước khi kiểm tra; 'all' = quét tất cả các góc; bỏ trống = kiểm tra tầm nhìn hiện tại"},
                    "room": {"type": "string", "description": "Phòng nếu khác phòng người nói"},
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
            "name": "get_datetime",
            "description": "Xem giờ hiện tại, thứ/ngày dương lịch, hoặc ngày âm lịch kèm can chi",
            "parameters": {"type": "object", "properties": {
                "part": {"type": "string", "enum": ["time", "date", "lunar"],
                         "description": "time=giờ, date=thứ/ngày/tháng, lunar=ngày âm lịch"},
                "day": {"type": "string", "enum": ["today", "tomorrow", "day_after"],
                        "description": "today=hôm nay, tomorrow=ngày mai, day_after=ngày mốt"},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": "Hẹn giờ: nhắc nhở một việc, hoặc bật/tắt thiết bị khi đến giờ "
                           "(thiết bị đặt qua entity + state). Cho một trong hai: minutes (số phút tới) hoặc time (giờ HH:MM 24h)",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "number", "description": "Số phút kể từ bây giờ"},
                    "time": {"type": "string", "description": "Giờ cụ thể HH:MM (24h)"},
                    "message": {"type": "string", "description": "Việc cần nhắc (bỏ qua nếu có entity + state)"},
                    "repeat": {"type": "string",
                               "enum": ["none", "daily", "weekdays", "weekend", "mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                               "description": "none=một lần; daily=mỗi ngày; weekdays=thứ Hai đến thứ Sáu; "
                                              "weekend=thứ Bảy và Chủ Nhật; mon..sun=mỗi tuần vào thứ đó"},
                    "day": {"type": "string", "enum": ["today", "tomorrow", "day_after"],
                            "description": "today=hôm nay, tomorrow=ngày mai, day_after=ngày mốt"},
                    "entity": {"type": "string", "description": "Thiết bị cần bật/tắt khi đến giờ (nếu hẹn thao tác, không phải nhắc)"},
                    "state": {"type": "string", "enum": ["on", "off"]},
                    "temperature": {"type": "integer", "minimum": 16, "maximum": 30,
                                    "description": "Nhiệt độ nếu thiết bị là điều hòa và state=on"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_timers",
            "description": "Liệt kê các hẹn giờ đang chờ (nhắc nhở lẫn hẹn bật/tắt thiết bị)",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_timer",
            "description": "Hủy hẹn giờ đang chờ. Trợ lý sẽ hỏi lại xác nhận, chỉ hủy khi người dùng đồng ý",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Từ khóa trong lời nhắc của hẹn giờ cần hủy, ví dụ: tắt bếp. Bỏ trống hoặc 'all' = tất cả"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_timer",
            "description": "Dời giờ hẹn đang chờ: cộng/thêm phút (shift_minutes, âm = sớm hơn) hoặc đặt lại giờ tuyệt đối (time HH:MM)",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Từ khóa trong lời nhắc của hẹn giờ cần dời, ví dụ: tắt máy lạnh"},
                    "shift_minutes": {"type": "number", "description": "Dời thêm bao nhiêu phút (âm = sớm lại)"},
                    "time": {"type": "string", "description": "Giờ mới HH:MM (24h)"},
                    "day": {"type": "string", "enum": ["today", "tomorrow", "day_after"]},
                },
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


def resolve_camera(db: Database, text: str, room_id=None):
    """Chọn camera: đúng tên trong câu > camera trong phòng người nói > camera duy nhất.
    Camera bị resolve_entity bỏ qua để không cuốn vào lệnh bật/tắt nên cần hàm riêng."""
    norm = normalize(text)
    cams = db.query("SELECT * FROM entities WHERE entity_type='camera'")
    if not cams:
        return None
    scope = _room_from_text(db, norm) or room_id
    ranked: list[tuple[int, dict]] = []
    for entity in cams:
        in_scope = scope and _entity_room(db, entity) == scope
        for alias in _entity_aliases(entity):
            if _alias_in(alias, norm):
                ranked.append((len(alias) + (100 if in_scope else 0), dict(entity)))
                break
    if ranked:
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return ranked[0][1]
    in_room = [e for e in cams if _entity_room(db, e) == scope]
    if len(in_room) == 1:
        return dict(in_room[0])
    if len(cams) == 1:
        return dict(cams[0])
    return None


def _person_question(norm: str) -> bool:
    """Câu hỏi có người/ai không? ("kiểm tra ... có người không", "có ai ... không")"""
    return bool(re.search(r"\b(nguoi|ai)\b", norm)
                and (re.search(r"\bkiem tra\b", norm) or re.search(r"\bco (nguoi|ai)\b", norm)))


def camera_multi_intent(norm: str) -> bool:
    """"Kiểm tra camera và cho tôi biết..." là MỘT việc (làm rồi báo lại) — chỉ
    phần còn lại của câu mới xét nối từ ghép (và/rồi/sau đó) như bình thường."""
    stripped = re.sub(r"\bva (cho|bao) (toi|minh|anh|chi|em)?\s*biet\b", " ", norm)
    return bool(re.search(r"\bva\b|\broi\b|\bsau do\b|\bthem\b|\bngoai ra\b", stripped))


def camera_ptz_gate(db: Database, norm: str) -> bool:
    """Câu này là lệnh camera (PTZ/kiểm tra người) không? Phải nhắc camera (từ
    "camera" hoặc đúng tên camera) kèm động từ quay/xoay hoặc hỏi về "góc"/người
    — bắt chung động từ "xoay" sẽ cuốn theo lệnh quạt."""
    rotate = re.search(r"\b(quay|xoay)\b", norm)
    ask_angles = re.search(r"\bgoc\b", norm) and re.search(r"\b(gi|nao|nhung)\b", norm)
    ask_person = _person_question(norm)
    if not (rotate or ask_angles or ask_person):
        return False
    if re.search(r"\bcamera\b", norm):
        return True
    # "kiểm tra góc quay bếp có ai không" — chỉ camera mới có góc quay + người
    if ask_person and re.search(r"\bgoc\b", norm):
        return True
    return any(
        _alias_in(alias, norm)
        for entity in db.query("SELECT * FROM entities WHERE entity_type='camera'")
        for alias in _entity_aliases(entity)
    )


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


async def act_camera_ptz(db: Database, entity, norm: str) -> dict:
    """Quay camera PTZ về góc đã lưu theo tên ("quay camera về phía bếp").

    Giọng nói giống wall display: chỉ được quay về góc đã lưu, không xoay tự do.
    Preset là tọa độ tự lưu trong driver_data (Imou bỏ qua preset trên máy).
    """
    from server.config import Config
    from server.drivers import dahua_ptz as ptz

    cam = entity["name"].lower()
    presets = loads(entity["driver_data"]).get("presets", [])
    tokens = norm.split()
    matched = [p for p in presets
               if normalize(p.get("name", "")).split()
               and all(w in tokens for w in normalize(p["name"]).split())]
    # hỏi danh sách góc ("camera có góc nào") thắng khớp tên: camera tên
    # "camera bếp" và preset "bếp" cùng chứa từ "bếp" trong câu hỏi
    asking_list = (re.search(r"\bgoc\b", norm) and re.search(r"\b(gi|nao|nhung)\b", norm)
                   and not re.search(r"\b(quay|xoay)\b", norm))

    if matched and not asking_list:
        device = db.one("SELECT * FROM devices WHERE id=?", (entity["device_id"],))
        if not device or not device["host"]:
            return {"reply": f"Không tìm thấy kết nối của {cam}.",
                    "action": {"tool": "camera_preset", "ok": False}}
        # tên dài nhất thắng: preset "bếp" và "bếp cửa sau" cùng khớp thì chọn cụ thể hơn
        preset = max(matched, key=lambda p: len(normalize(p["name"])))
        try:
            await asyncio.to_thread(ptz.fast_goto, Config(), device,
                                    preset["pan"], preset["tilt"], preset.get("zoom", 1))
        except ptz.PtzError as exc:
            return {"reply": f"Không quay được camera ({exc}).",
                    "action": {"tool": "camera_preset", "ok": False}}
        return {"reply": f"Đã quay {cam} về góc {preset['name']}.",
                "action": {"tool": "camera_preset", "entity": entity["name"],
                           "preset": preset["name"], "ok": True}}

    angles = ", ".join(p["name"] for p in presets) or "chưa có góc nào"
    # "camera đang ở góc nào" — trả lời bằng góc đã lưu gần vị trí hiện tại
    if "dang" in tokens:
        device = db.one("SELECT * FROM devices WHERE id=?", (entity["device_id"],))
        if device and device["host"] and presets:
            try:
                pos = await asyncio.to_thread(ptz.read_position, Config(), device)
            except ptz.PtzError as exc:
                return {"reply": f"Không đọc được vị trí camera ({exc}).",
                        "action": {"tool": "camera_preset", "ok": False}}

            def _dist(p) -> int:
                pan = min((pos["pan"] - p["pan"]) % 3600, (p["pan"] - pos["pan"]) % 3600)
                return pan + abs(pos["tilt"] - p.get("tilt", 0))

            near = min(presets, key=_dist)
            return {"reply": f"{cam} đang ở gần góc {near['name']} nhất.",
                    "action": {"tool": "camera_preset", "entity": entity["name"], "ok": True}}
        return {"reply": f"{cam} chưa lưu góc nào nên mình không biết đang quay về đâu.",
                "action": {"tool": "camera_preset", "ok": False}}
    return {"reply": f"{cam} có các góc đã lưu: {angles}.",
            "action": {"tool": "camera_preset", "ok": False}}


async def act_camera_person(db: Database, entity, norm: str) -> dict | None:
    """Kiểm tra camera có nhìn thấy người không (person detection của Frigate).

    None = câu không phải hỏi về người (để act_camera_ptz xử lý tiếp). Ba dạng:
    hỏi góc nào đó ("kiểm tra góc bếp có ai không" — quay tới rồi xem), quét tất
    cả góc ("kiểm tra các góc quay camera..."), hoặc nhìn thẳng vị trí hiện tại
    ("camera có người không")."""
    from server.config import Config
    from server.core import frigate as frg
    from server.drivers import dahua_ptz as ptz

    if not _person_question(norm):
        return None

    cam = entity["name"].lower()
    presets = loads(entity["driver_data"]).get("presets", [])
    tokens = norm.split()
    matched = [p for p in presets
               if normalize(p.get("name", "")).split()
               and all(w in tokens for w in normalize(p["name"]).split())]
    sweep = not matched and bool(re.search(r"\bcac goc\b|\btat ca\b|\bgoc\b", norm))

    fname = loads(entity["driver_data"]).get("frigate") or await frg.sole_camera()
    if not fname:
        return {"reply": "Mình không kết nối được hệ thống ghi hình (Frigate).",
                "action": {"tool": "camera_person", "ok": False}}
    device = db.one("SELECT * FROM devices WHERE id=?", (entity["device_id"],))

    async def _check() -> bool | None:
        return await frg.person_present(fname)

    def _answer(seen: bool | None) -> str:
        return "không rõ" if seen is None else ("có người" if seen else "không có ai")

    # quét tất cả các góc: tới từng góc -> chờ motor + detection -> đọc Frigate
    if sweep and presets and device and device["host"]:
        try:
            home = await asyncio.to_thread(ptz.read_position, Config(), device)
        except ptz.PtzError:
            home = None
        results = []
        for p in presets:
            try:
                await asyncio.to_thread(ptz.fast_goto, Config(), device,
                                        p["pan"], p["tilt"], p.get("zoom", 1))
            except ptz.PtzError as exc:
                return {"reply": f"Không quay được camera ({exc}).",
                        "action": {"tool": "camera_person", "ok": False}}
            # motor mất 2-3 giây + Frigate cần ~2 giây để xác nhận người mới
            await asyncio.sleep(4.5)
            results.append(f"{p['name']} — {_answer(await _check())}")
        if home:
            try:
                await asyncio.to_thread(ptz.fast_goto, Config(), device,
                                        home["pan"], home["tilt"], home.get("zoom", 1))
            except ptz.PtzError:
                pass
        return {"reply": f"Đã kiểm tra các góc: {'; '.join(results)}. Camera quay về vị trí ban đầu.",
                "action": {"tool": "camera_person", "entity": entity["name"], "ok": True}}

    # một góc nêu tên: quay tới rồi kiểm tra
    if matched and device and device["host"]:
        preset = max(matched, key=lambda p: len(normalize(p["name"])))
        try:
            await asyncio.to_thread(ptz.fast_goto, Config(), device,
                                    preset["pan"], preset["tilt"], preset.get("zoom", 1))
        except ptz.PtzError as exc:
            return {"reply": f"Không quay được camera ({exc}).",
                    "action": {"tool": "camera_person", "ok": False}}
        await asyncio.sleep(4.5)
        seen = await _check()
        if seen is None:
            return {"reply": "Mình không kết nối được hệ thống ghi hình (Frigate).",
                    "action": {"tool": "camera_person", "ok": False}}
        return {"reply": f"{'Có người' if seen else 'Không có ai'} ở góc {preset['name']}.",
                "action": {"tool": "camera_person", "entity": entity["name"],
                           "preset": preset["name"], "person": seen, "ok": True}}

    # không nêu góc: kiểm tra thẳng tầm nhìn hiện tại
    seen = await _check()
    if seen is None:
        return {"reply": "Mình không kết nối được hệ thống ghi hình (Frigate).",
                "action": {"tool": "camera_person", "ok": False}}
    return {"reply": f"{'Có người' if seen else 'Không có ai'} trong hình {cam}.",
            "action": {"tool": "camera_person", "entity": entity["name"],
                       "person": seen, "ok": True}}


async def act_climate(db, drivers, entity, mode=None, temperature=None, mentioned_room=None) -> dict:
    if mode == "off":
        command, params = "off", {}
    elif mode == "on":
        # "bật" không kèm thông tin: dùng lại đúng như lần trước (IR một chiều
        # nên không đọc được từ máy — state trong DB là lần gửi cuối). Mức quạt
        # lần trước do driver tự inject. Lệnh nói rõ nhiệt độ thì thắng.
        last = loads(entity["state"]) if entity["state"] else {}
        command, params = "set", {"mode": last.get("mode") or "cool",
                                  "temperature": int(temperature or last.get("temperature") or 26)}
    else:
        command, params = "set", {"mode": mode or "cool", "temperature": int(temperature or 26)}
    state = await _set_state(db, drivers, entity, command, params)
    where = _room_where(db, _entity_room(db, entity), mentioned_room)
    if mode == "off":
        reply = f"Đã tắt {entity['name'].lower()}{where}."
    elif mode == "on":
        # "bật" không kèm thông tin: đọc lại trạng thái đang dùng để người nghe
        # biết có cần tăng/giảm tiếp hay không
        temp = state.get("temperature", params.get("temperature"))
        fan_vi = FAN_VI.get(state.get("fan"), str(state.get("fan") or ""))
        fan_txt = f", quạt mức {fan_vi}" if fan_vi else ""
        reply = f"Đã bật {entity['name'].lower()}{where}, nhiệt độ {temp} độ{fan_txt}."
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
    if updated and (now() - updated).total_seconds() > 3600:
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


def datetime_question(norm: str) -> tuple[bool, bool, bool]:
    """Câu hỏi về giờ / ngày tháng / âm lịch? Trả về (giờ, ngày, âm lịch).
    "Bao giờ" (= khi nào) không tính — câu đó để LLM trả lời tự do."""
    lunar = bool(re.search(r"\bam lich\b|\blich am\b|\bmung may\b", norm))
    ask_time = bool(re.search(r"\bmay gio\b|\bgio hien tai\b", norm))
    ask_date = lunar or bool(re.search(
        r"\bthu may\b|\bngay (may|bao nhieu|gi)\b|\bngay thang\b", norm))
    return ask_time, ask_date, lunar


def _timer_intent(norm: str) -> bool:
    """Câu mang ý định hẹn giờ? Bắt 'nhắc/hẹn' và 'báo' — nhưng 'bao nhiêu',
    'bao giờ' là từ hỏi nên không tính."""
    return bool(re.search(r"\b(nhac|hen)\b", norm)
                or re.search(r"\bbao\b(?!\s*(nhieu|gio))", norm))


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


WEEKDAYS_VI = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật")
DAY_LABELS = {0: "Hôm nay", 1: "Ngày mai", 2: "Ngày mốt"}


def _vi_clock(hour: int, minute: int) -> str:
    return f"{hour} giờ đúng" if minute == 0 else f"{hour} giờ {minute} phút"


def act_datetime(ask_time=False, ask_date=False, ask_lunar=False, day=0,
                 delta_minutes=None) -> dict:
    """Hỏi giờ / ngày tháng / âm lịch — trả lời ngay, không cần thiết bị."""
    now_dt = now()
    label = DAY_LABELS.get(day, "Hôm nay")
    parts = []
    if ask_time:
        if delta_minutes:
            target = now_dt + timedelta(minutes=delta_minutes)
            span = (f"{delta_minutes // 60:g} tiếng" if delta_minutes % 60 == 0
                    else f"{delta_minutes:g} phút")
            parts.append(f"sau {span} nữa là {_vi_clock(target.hour, target.minute)}")
        else:
            parts.append(f"bây giờ là {_vi_clock(now_dt.hour, now_dt.minute)}")
    if ask_date:
        when = now_dt + timedelta(days=day)
        if ask_lunar:
            # hỏi âm thì chỉ đọc âm — ngày dương nghe thêm thành thừa
            ld, lm, ly, leap = solar_to_lunar(when.day, when.month, when.year)
            day_word = f"mùng {ld}" if ld <= 10 else str(ld)
            parts.append(f"âm lịch {label.lower()} là ngày {day_word} tháng {lm}"
                         f"{' nhuận' if leap else ''} năm {can_chi_year(ly)}")
        else:
            parts.append(f"{label.lower()} là {WEEKDAYS_VI[when.weekday()]}, "
                         f"ngày {when.day} tháng {when.month} năm {when.year}")
    text = ", ".join(parts)
    return {"reply": text[0].upper() + text[1:] + ".",
            "action": {"tool": "get_datetime", "ok": True, "day": day}}


def act_timer(db, room_id, message=None, minutes=None, time_str=None,
              repeat="", day_offset=None, weekday=None, action=None) -> dict:
    """Tạo hẹn giờ: nhắc nhở (message) hoặc thao tác thiết bị (action JSON)."""
    if minutes:
        target = now() + timedelta(minutes=float(minutes))
        repeat, day_offset, weekday = "", None, None  # "X phút nữa" không lặp được
    elif time_str:
        hh, mm = (int(p) for p in time_str.split(":")[:2])
        if repeat:
            target = next_repeat(repeat, hh, mm)
        elif day_offset:
            target = (now() + timedelta(days=day_offset)).replace(
                hour=hh, minute=mm, second=0, microsecond=0)
        elif weekday is not None:
            target = next_repeat(str(weekday), hh, mm)
        else:
            target = next_hhmm(hh, mm)
    else:
        return {"reply": "Mình chưa rõ thời gian hẹn.", "action": {"tool": "set_timer", "ok": False}}
    message = (message or "").strip() or (action or {}).get("message") or "hẹn giờ"
    db.execute(
        "INSERT INTO timers(target_at, room_id, message, created_at, repeat, action) "
        "VALUES (?,?,?,?,?,?)",
        (target.isoformat(timespec="seconds"), room_id, message, now_iso(), repeat,
         json.dumps(action, ensure_ascii=False) if action else ""),
    )
    clock = target.strftime("%H:%M")
    when = _when_label(repeat, day_offset, weekday)
    in_ = f" (sau {int(minutes)} phút nữa)" if minutes else ""
    verb = f"mình sẽ {message}." if action else f"mình nhắc bạn: {message}."
    return {"reply": f"Ok, {when}{clock}{in_} {verb}",
            "action": {"tool": "set_timer", "ok": True, "at": clock, "repeat": repeat or None}}


# ---------------------------------------------------------------------------
# Hẹn giờ: parse giờ/lặp/ngày + liệt kê / hủy (xác nhận) / dời

_WD_ALT = r"thu\s*(?:hai|ba|tu|nam|sau|bay|[2-8])|chu\s*nhat|\bcn\b"


def _wd_value(text: str) -> int | None:
    """'thứ hai/thứ 2' -> 0 ... 'chủ nhật/thứ 8' -> 6 (thứ Python)."""
    t = re.sub(r"\s+", " ", text.strip().lower())
    words = {"hai": 0, "ba": 1, "tu": 2, "nam": 3, "sau": 4, "bay": 5, "nhat": 6}
    m = re.fullmatch(r"(?:thu\s*)?([2-8])", t)
    if m:
        n = int(m.group(1))
        return n - 2 if n <= 7 else 6  # "thứ 8" là cách gọi dân dã của Chủ nhật
    m = re.fullmatch(r"(?:thu|chu)\s*(\w+)", t)
    if m and m.group(1) in words:
        return words[m.group(1)]
    if t == "cn":
        return 6
    return None


def _parse_when(norm: str):
    """(repeat, day_offset, weekday): 'mỗi ngày', 'T2 đến T6', 'cuối tuần',
    'ngày mai', 'thứ N' (có mỗi/hàng = lặp tuần, không = một lần tới)."""
    repeat, day_offset, weekday = "", None, None
    if re.search(r"\b(moi|hang)\s+(ngay|buoi|sang|trua|chieu|toi|dem)\b", norm):
        repeat = "daily"
    if re.search(r"\bcuoi tuan\b|\bthu bay va chu nhat\b", norm):
        repeat = "5,6"
    m = re.search(rf"({_WD_ALT})\s+(?:den|toi)\s+({_WD_ALT})", norm)
    if m:
        a, b = _wd_value(m.group(1)), _wd_value(m.group(2))
        if a is not None and b is not None:
            span = list(range(a, b + 1)) if a <= b else list(range(a, 7)) + list(range(0, b + 1))
            repeat = ",".join(map(str, span))
    if not repeat:
        for m in re.finditer(_WD_ALT, norm):
            found = _wd_value(m.group(0))
            if found is None:
                continue
            if re.search(r"\b(moi|hang)\b", norm):
                repeat = str(found)  # "mỗi thứ hai" = lặp hàng tuần
            else:
                weekday = found     # "thứ năm lúc 5h" = lần tới thôi
            break
    if re.search(r"\bmai\b", norm):        # "ngày mai" / "6 giờ sáng mai"
        day_offset = 1
    elif re.search(r"\bngay mot\b", norm):
        day_offset = 2
    return repeat, day_offset, weekday


def _clock_from_norm(norm: str):
    """Giờ trong câu: '15:30', '3 giờ chiều', '3 giờ rưỡi', '6 giờ 30 phút',
    'tới 1 giờ'. Trả về (hour, minute) hoặc None."""
    hhmm = re.search(r"\b(\d{1,2}):(\d{2})\b", norm)
    if hhmm:
        hour, minute = int(hhmm.group(1)), int(hhmm.group(2))
        part = re.match(r"\s*(chieu|toi|dem|sang|trua)", norm[hhmm.end():])
        return hhmm_adjusted(hour, minute, part.group(1) if part else None), minute
    m = re.search(r"\b(\d{1,2})\s*gio\s+ruoi\b", norm)
    if m:
        return hhmm_adjusted(int(m.group(1)), 30, None), 30
    # "6 giờ 30 [phút] [tối]" — chữ 'phút' và buổi đều có thể bị lược
    m = re.search(r"\b(\d{1,2})\s*gio\s+(\d{1,2})(?:\s*phut)?\s*(?:\b(chieu|toi|sang|dem|trua)\b)?", norm)
    if m and m.group(2):
        hour, minute = int(m.group(1)), min(int(m.group(2)), 59)
        return hhmm_adjusted(hour, minute, m.group(3)), minute
    m = re.search(r"\b(\d{1,2})\s*gio\s*(?:chieu|toi|sang|dem)\b", norm)
    if m:
        part = m.group(0).split()[-1]
        return hhmm_adjusted(int(m.group(1)), 0, part), 0
    # "tới/về 1 giờ" (không kèm sáng/chiều): giờ tuyệt đối — phân biệt với
    # "thêm 1 giờ" (khoảng thời gian) bằng từ đi trước
    m = re.search(r"\b(?:toi|ve|vao|luc)\s+(\d{1,2})\s*gio\b", norm)
    if m:
        return int(m.group(1)) % 24, 0
    return None


# từ chỉ thời gian/lệnh — bỏ đi rồi phần còn lại của câu mới là lời nhắc.
# KHÔNG đưa "den" vào đây: "đèn" và "đến" cùng normalize thành "den", bỏ nó
# sẽ cắt mất từ "đèn" trong lời nhắc.
_WHEN_DROP = {
    "vao", "luc", "khoang", "nua", "thi", "roi", "moi", "hang", "ngay", "mai",
    "hom", "nay", "cuoi", "tuan", "tu", "sang", "trua", "chieu", "toi",
    "dem", "ruoi", "gio", "phut", "tieng", "hen", "cai", "ca",
}


def _strip_when(text: str) -> str:
    """Bỏ các từ chỉ thời gian để lại đúng nội dung việc cần nhắc."""
    keep = []
    for tok in text.split():
        n = normalize(tok)
        if n in _WHEN_DROP or n.isdigit() or re.fullmatch(r"\d{1,2}:\d{2}", n) \
                or _wd_value(n) is not None:
            continue
        keep.append(tok)
    return " ".join(keep).strip()


def _timer_message(raw: str) -> str:
    """Lời nhắc = phần sau 'nhắc/hẹn/báo [tôi]', bỏ từ chỉ giờ; không có từ
    đó thì lấy cả câu bỏ từ chỉ giờ."""
    parts = re.split(r"(?:nhắc|hẹn|báo)\s*(?:tôi|em|mình|tui)?", raw, flags=re.I)
    tail = parts[-1].strip() if len(parts) > 1 else raw
    if not tail:
        return "hẹn giờ"
    return _strip_when(tail) or "hẹn giờ"


def _repeat_label(repeat: str) -> str:
    if repeat == "daily":
        return "mỗi ngày"
    days = sorted(int(d) for d in repeat.split(",") if d.strip().isdigit())
    if days == [5, 6]:
        return "cuối tuần"
    if days == [0, 1, 2, 3, 4]:
        return "thứ Hai đến thứ Sáu"
    if len(days) == 1:
        return f"mỗi {WEEKDAYS_VI[days[0]].lower()}"
    return " và ".join(WEEKDAYS_VI[d].lower() for d in days)


def _when_label(repeat: str, day_offset, weekday) -> str:
    """Đầu câu trả lời: 'mỗi ngày ', 'ngày mai ', 'Thứ Năm ', '' (hôm nay)."""
    if repeat:
        return f"{_repeat_label(repeat).lower()} "
    if day_offset == 1:
        return "ngày mai "
    if day_offset == 2:
        return "ngày mốt "
    if weekday is not None:
        return f"{WEEKDAYS_VI[weekday].lower()} "
    return ""


def _when_short(timer_row) -> str:
    at = parse_iso(timer_row["target_at"])
    clock = at.strftime("%H:%M")
    if timer_row["repeat"] == "daily":
        return f"mỗi ngày {clock}"
    if timer_row["repeat"]:
        return f"{_repeat_label(timer_row['repeat']).lower()} {clock}"
    day, today = at.date(), now().date()
    if day == today:
        return clock
    if day == today + timedelta(days=1):
        return f"ngày mai {clock}"
    return f"ngày {at.day}/{at.month} {clock}"


def _timers_brief(rows) -> str:
    return "; ".join(f"{t['message'].lower()} ({_when_short(t)})" for t in rows)


def act_list_timers(db) -> dict:
    rows = db.query(
        """SELECT t.*, r.name AS room_name FROM timers t
           LEFT JOIN rooms r ON r.id=t.room_id WHERE t.done=0 ORDER BY t.target_at""")
    if not rows:
        return {"reply": "Hiện không có hẹn giờ nào đang chờ.",
                "action": {"tool": "list_timers", "ok": True}}
    head = "Mình đang giữ" if len(rows) > 1 else "Đang có"
    return {"reply": f"{head} {len(rows)} hẹn giờ: {_timers_brief(rows)}.",
            "action": {"tool": "list_timers", "ok": True, "count": len(rows)}}


# xác nhận hủy đang chờ lời đáp, theo từng phòng (0 = console không phòng)
_PENDING_CANCEL: dict[int, dict] = {}

_CONFIRM_NO = (r"^(khong|khong roi|khong phai|khong can|thoi|bo qua|khoan|de sau|"
               r"chua|chua chac|de|dung hen)(\s+(nhe|nha|di|roi|nua))*[\s,.!?-]*$")
_CONFIRM_YES = (r"^(co|co roi|dung|dung roi|ok|ok roi|ur|um|u|chac chan|vay|di|"
                r"huy di|lam di|nghe)(\s+(nhe|nha|di|roi|nua))*[\s,.!?-]*$")


def _confirm_answer(norm: str) -> str | None:
    if re.match(_CONFIRM_NO, norm):
        return "no"
    if re.match(_CONFIRM_YES, norm):
        return "yes"
    return None


# từ đệm của câu quản lý hẹn giờ (từ lệnh + từ chỉ thời gian) — bỏ đi rồi
# phần còn lại mới khớp với lời nhắc đã lưu
_TIMER_STOP = {
    "huy", "bo", "xoa", "tat", "hen", "gio", "gi", "cai", "nhung", "di", "het",
    "nhe", "nua", "cua", "toi", "minh", "em", "giup", "phut", "con", "chua",
    "dang", "lam", "hay", "cho", "ve", "den", "va", "roi", "doi", "lui", "dich",
    "chuyen", "sua", "chinh", "them", "keo", "dai", "don", "lai", "som", "tre",
    "hon", "thoi", "gian", "chut", "ca", "voi", "mot", "that",
    "tieng", "giay", "sang", "trua", "chieu", "dem", "ruoi", "vao", "khoang",
    "hai", "ba", "bon", "nam", "sau", "bay", "tam", "chin", "muoi", "lam",
    "ngay", "mai", "hom", "nay",
}


def _timer_query_words(norm: str) -> list[str]:
    return [w for w in norm.split()
            if w not in _TIMER_STOP and not w.isdigit() and _wd_value(w) is None]


def act_cancel_timer(db, room_id, query: str = "") -> dict:
    """Hủy hẹn giờ — Luôn hỏi lại xác nhận; lời đáp 'có/không' xử lý ở
    handle_command (xem _PENDING_CANCEL)."""
    rows = db.query("SELECT * FROM timers WHERE done=0 ORDER BY target_at")
    if not rows:
        return {"reply": "Hiện không có hẹn giờ nào để hủy.",
                "action": {"tool": "cancel_timer", "ok": False}}
    norm = normalize(query)
    want_all = bool(re.search(r"\b(het|tat ca|moi)\b", norm))
    words = _timer_query_words(norm)
    if want_all:
        matches = list(rows)
    elif words:
        matches = [t for t in rows
                   if all(w in normalize(t["message"]).split() for w in words)]
    else:
        matches = list(rows) if len(rows) == 1 else []
    if not matches:
        if not words and len(rows) > 1:
            return {"reply": "Bạn muốn hủy hẹn giờ nào? " + _timers_brief(rows),
                    "action": {"tool": "cancel_timer", "ok": False}}
        return {"reply": "Mình không tìm thấy hẹn giờ nào như vậy.",
                "action": {"tool": "cancel_timer", "ok": False}}
    if len(matches) > 1 and not want_all:
        return {"reply": "Có mấy hẹn giờ trùng ý, bạn nêu rõ hơn nhé: " + _timers_brief(matches),
                "action": {"tool": "cancel_timer", "ok": False}}
    if len(matches) == 1:
        label = f"hẹn giờ '{matches[0]['message']}' ({_when_short(matches[0])})"
    else:
        label = f"{len(matches)} hẹn giờ"
    key = room_id if room_id is not None else 0
    _PENDING_CANCEL[key] = {"ids": [t["id"] for t in matches], "label": label,
                            "expires": now() + timedelta(seconds=60)}
    return {"reply": f"Bạn có chắc muốn hủy {label} không? Nói 'có' để hủy, 'không' để giữ nguyên.",
            "action": {"tool": "cancel_timer", "ok": True, "confirming": True}}


def _shift_minutes(norm: str):
    """Số phút dời trong 'dời/lùi ... thêm X phút/giờ' (dương = muộn hơn,
    'sớm hơn/sớm lại' = âm). None = câu không có khoảng dời."""
    tokens = norm.split()
    for i, tok in enumerate(tokens):
        if tok in ("phut", "gio", "tieng"):
            got = trailing_number(tokens[:i])
            if not got:
                continue
            minutes = got[0] if tok == "phut" else got[0] * 60
            if re.search(r"\b(som|som hon|som lai|keo len|truoc hon)\b", norm):
                minutes = -minutes
            return minutes
    return None


def act_move_timer(db, room_id, text: str) -> dict | None:
    """"Dời/đổi/lùi hẹn giờ...": None = câu không phải lệnh dời (nhường nhánh khác)."""
    norm = normalize(text)
    if not re.search(r"\b(doi|lui|dich|chuyen|sua|chinh|keo|don)\b", norm):
        return None
    rows = db.query("SELECT * FROM timers WHERE done=0 ORDER BY target_at")
    if not rows:
        return None
    words = _timer_query_words(norm)
    if words:
        matches = [t for t in rows
                   if all(w in normalize(t["message"]).split() for w in words)]
    else:
        matches = list(rows) if len(rows) == 1 else []
    if not matches:
        if "hen gio" not in norm:
            return None  # không nhắc hẹn giờ nào -> không phải câu dời
        return {"reply": "Mình không tìm thấy hẹn giờ nào như vậy.",
                "action": {"tool": "move_timer", "ok": False}}
    if len(matches) > 1:
        return {"reply": "Có mấy hẹn giờ trùng ý, bạn nêu rõ hơn nhé: " + _timers_brief(matches),
                "action": {"tool": "move_timer", "ok": False}}
    timer = matches[0]
    clock = _clock_from_norm(norm)
    delta = _shift_minutes(norm) if clock is None else None
    if clock is None and delta is None:
        return None  # có động từ dời nhưng không có giờ mới -> nhường nhánh thường
    if clock is not None:
        hh, mm = clock
        _, day_offset, _ = _parse_when(norm)
        if timer["repeat"]:
            target = next_repeat(timer["repeat"], hh, mm)
        elif day_offset:
            target = (now() + timedelta(days=day_offset)).replace(
                hour=hh, minute=mm, second=0, microsecond=0)
        else:
            target = next_hhmm(hh, mm)
    else:
        target = parse_iso(timer["target_at"]) + timedelta(minutes=delta)
        if target <= now():
            return {"reply": "Giờ dời tới đã qua rồi, mình không dời được.",
                    "action": {"tool": "move_timer", "ok": False}}
    db.execute("UPDATE timers SET target_at=? WHERE id=?",
               (target.isoformat(timespec="seconds"), timer["id"]))
    moved = {"target_at": target.isoformat(timespec="seconds"), "repeat": timer["repeat"]}
    if clock is not None:
        change = f"tới {target.strftime('%H:%M')}"
    else:
        change = f"{'sớm' if delta < 0 else 'muộn'} hơn {abs(delta):g} phút"
    return {"reply": f"Ok, mình dời hẹn giờ '{timer['message']}' {change}, "
                     f"giờ là {_when_short(moved)}.",
            "action": {"tool": "move_timer", "ok": True, "at": target.strftime("%H:%M")}}


def _timer_action(db, ent, on: bool, temperature=None, mentioned=None) -> dict:
    """Lệnh thiết bị sẽ chạy khi hẹn giờ đến, kèm câu mô tả làm lời nhắc."""
    where = _room_where(db, _entity_room(db, ent), mentioned)
    if ent["entity_type"] == "climate":
        if on:
            last = loads(ent["state"]) if ent["state"] else {}
            params = {"mode": last.get("mode") or "cool",
                      "temperature": int(temperature or last.get("temperature") or 26)}
            return {"entity_id": ent["id"], "name": ent["name"], "command": "set",
                    "params": params,
                    "message": f"bật {ent['name'].lower()}{where} {params['temperature']} độ"}
        return {"entity_id": ent["id"], "name": ent["name"], "command": "off",
                "params": {}, "message": f"tắt {ent['name'].lower()}{where}"}
    return {"entity_id": ent["id"], "name": ent["name"],
            "command": "on" if on else "off", "params": {},
            "message": f"{'bật' if on else 'tắt'} {ent['name'].lower()}{where}"}


def _device_action_from_text(db, norm, room_id):
    """Câu 'lúc X giờ bật/tắt Y' không có từ nhắc/hẹn → lệnh thiết bị cho timer.
    None = không ra thiết bị nào (coi như nhắc nhở thường)."""
    has_on = bool(re.search(r"\b(bat|mo|bat len)\b", norm))
    has_off = bool(re.search(r"\b(tat|tat di)\b", norm))
    if not (has_on or has_off):
        return None
    mentioned = _room_from_text(db, norm)
    temp = _temp_from_text(norm)
    ent = resolve_entity(db, norm, room_id, "climate")
    if not ent:
        for etype in ("light", "switch", "fan", "media"):
            cand = resolve_entity(db, norm, room_id, etype)
            if cand and cand["entity_type"] == etype:  # bỏ qua controller (hub)
                ent = cand
                break
    if not ent:
        return None
    if ent["entity_type"] == "controller" or loads(ent["driver_data"]).get("readonly"):
        return None
    return _timer_action(db, ent, on=has_on, temperature=temp, mentioned=mentioned)


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


def _parse_timer(norm: str, raw: str) -> dict:
    """Giờ hẹn + nội dung từ câu đã normalize. Trả về dict: minutes/time_str
    (một trong hai), message, repeat, day_offset, weekday."""
    clock = _clock_from_norm(norm)
    if clock is not None:
        hour, minute = clock
        repeat, day_offset, weekday = _parse_when(norm)
        return {"minutes": None, "time_str": f"{hour:02d}:{minute:02d}",
                "message": _timer_message(raw), "repeat": repeat,
                "day_offset": day_offset, "weekday": weekday}

    # "<số> phút/giờ/tiếng nữa" duration
    tokens = norm.split()
    for i, tok in enumerate(tokens):
        if tok in ("phut", "gio", "tieng") or tok.startswith("gio:"):
            got = trailing_number(tokens[:i])
            if not got:
                continue
            minutes = got[0] if tok == "phut" else got[0] * 60
            return {"minutes": minutes, "time_str": None, "message": _timer_message(raw),
                    "repeat": "", "day_offset": None, "weekday": None}
    return {"minutes": None, "time_str": None, "message": "", "repeat": "",
            "day_offset": None, "weekday": None}


def _duration_minutes(norm: str):
    """Số phút trong cụm 'X phút/giờ nữa' của câu hỏi giờ ('2 tiếng nữa là mấy giờ')."""
    if not re.search(r"\bnua\b", norm):
        return None
    tokens = norm.split()
    for i, tok in enumerate(tokens):
        if tok in ("phut", "gio", "tieng"):
            got = trailing_number(tokens[:i])
            if got:
                return got[0] if tok == "phut" else got[0] * 60
    return None


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
           WHERE e.entity_type IN ('light','switch','climate','fan','media')
             AND d.driver!='broadlink'"""
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

    # dời hẹn giờ ("dời tắt máy lạnh thêm 1 tiếng") — phải đứng TRƯỚC mọi nhánh
    # khác vì câu chứa động từ bật/tắt + giờ, lọt xuống nhánh thường sẽ tắt
    # nhầm thiết bị ngay lập tức
    moved = act_move_timer(db, room_id, text)
    if moved:
        return moved

    # quản lý hẹn giờ đang chờ: hủy (trợ lý hỏi lại xác nhận), hỏi danh sách
    if "hen gio" in norm or re.search(r"\b(da|vua|con)\s+hen\b", norm):
        if re.search(r"\b(huy|bo|xoa|tat)\b.*\bhen gio\b", norm) and not re.search(r"\d", norm):
            return act_cancel_timer(db, room_id, query=text)
        if not re.search(r"\d", norm) and not (has_on or has_off) \
                and re.search(r"\b(gi|nao|nhung|dang|con)\b", norm):
            return act_list_timers(db)

    # timers: fast shortcut unless the sentence is clearly multi-intent
    # ("bật đèn và 15 phút nữa nhắc tôi..." needs the LLM to do both)
    timerish = re.search(r"\b(nhac|hen|bao)\b", norm) or re.search(
        r"\d+\s*(phut|gio|tieng)|\d{1,2}:\d{2}", norm
    ) or re.search(r"\b(mot|hai|ba|bon|nam|sau|bay|tam|chin|muoi|lam)\s+(phut|gio|tieng)\b", norm)
    multi_intent = re.search(r"\broi\b|\bsau do\b|\bva\b|\bva ca\b|\bthem\b|\bngoai ra\b", norm)

    # hỏi giờ / ngày tháng / âm lịch — phải đứng TRƯỚC nhánh hẹn giờ vì câu hỏi
    # "mấy giờ" cũng chứa từ "giờ". Câu có ý định hẹn (nhắc/hẹn/báo), động từ
    # bật/tắt, hay ghép nhiều việc thì nhường LLM. ("Rồi" trong "mấy giờ rồi"
    # là trợ từ chứ không phải từ nối — xem multi-part ở trên nên không dùng
    # multi_intent ở đây.)
    if not (has_on or has_off) and not _timer_intent(norm) \
            and not re.search(r"\bsau do\b|\bva\b|\bva ca\b|\bthem\b|\bngoai ra\b", norm):
        ask_time, ask_date, ask_lunar = datetime_question(norm)
        if ask_time or ask_date:
            return act_datetime(
                ask_time=ask_time, ask_date=ask_date, ask_lunar=ask_lunar,
                day=weather_day(norm) if ask_date else 0,
                delta_minutes=_duration_minutes(norm) if ask_time else None,
            )

    if timerish and not multi_intent:
        parsed = _parse_timer(norm, text)
        if parsed["minutes"] or parsed["time_str"]:
            # không có từ nhắc/hẹn/báo mà nói tên thiết bị + bật/tắt: hẹn giờ
            # THAO TÁC thiết bị, không phải lời nhắc
            action = None
            if not _timer_intent(norm):
                action = _device_action_from_text(db, norm, room_id)
            return act_timer(db, room_id, message=None if action else parsed["message"],
                             minutes=parsed["minutes"], time_str=parsed["time_str"],
                             repeat=parsed["repeat"], day_offset=parsed["day_offset"],
                             weekday=parsed["weekday"], action=action)

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

    # camera PTZ theo tên góc đã lưu ("quay camera về phía bếp") + kiểm tra
    # người qua Frigate ("camera có người không", "kiểm tra góc bếp có ai không").
    # Đặt TRƯỚC nhánh âm lượng/xoay quạt vì "xoay" cũng là từ khóa quạt; câu ghép
    # thật sự ("quay camera và bật đèn") nhường LLM để gọi đủ công cụ.
    if camera_ptz_gate(db, norm) and not camera_multi_intent(norm):
        cam = resolve_camera(db, norm, room_id)
        if cam:
            # hỏi người đặt trước PTZ: "quay camera về bếp và cho biết có
            # người không" chứa động từ quay + preset nhưng ý là kiểm tra
            person = await act_camera_person(db, cam, norm)
            if person:
                return person
            return await act_camera_ptz(db, cam, norm)
        return {"reply": "Mình không tìm thấy camera nào.",
                "action": {"tool": "camera_preset", "ok": False}}

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

    # "chỉnh máy lạnh 25 độ" — set nhiệt độ không cần động từ bật/tắt
    if not (has_on or has_off):
        temp = _temp_from_text(norm)
        if temp:
            entity = resolve_entity(db, norm, room_id, "climate")
            if entity:
                return await act_climate(db, drivers, entity, mode="cool", temperature=temp,
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
        for etype in ("light", "switch", "fan", "media"):
            entity = resolve_entity(db, norm, room_id, etype)
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
        extra = ""
        if e["entity_type"] == "camera":
            names = [p["name"] for p in loads(e["driver_data"]).get("presets", []) if p.get("name")]
            extra = f"; góc đã lưu: {', '.join(names)}" if names else ""
            extra += "; hỏi có người không -> camera_person (preset 'all' = quét mọi góc)"
        lines.append(f"- {e['name']} ({e['entity_type']}, phòng {room}{extra})")
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

        if name == "camera_preset":
            hint = normalize(f"{args.get('entity', '')} {args.get('room') or ''}")
            cam = resolve_camera(db, hint, room_id)
            if not cam:
                return {"reply": "Mình không tìm thấy camera nào.",
                        "action": {"tool": name, "ok": False}}
            # chỉ khớp tên góc từ tham số preset (không gộp tên camera — camera
            # "camera bếp" sẽ khớp nhầm preset "bếp"); sai tên thì act liệt kê lại
            return await act_camera_ptz(db, cam, normalize(str(args.get("preset", ""))))

        if name == "camera_person":
            hint = normalize(f"{args.get('entity', '')} {args.get('room') or ''}")
            cam = resolve_camera(db, hint, room_id)
            if not cam:
                return {"reply": "Mình không tìm thấy camera nào.",
                        "action": {"tool": name, "ok": False}}
            # dựng lại câu hỏi từ tham số: all = quét mọi góc, tên = góc đó,
            # bỏ trống = kiểm tra tầm nhìn hiện tại
            arg = normalize(str(args.get("preset", "") or ""))
            if arg in ("all", "tat ca"):
                text = "kiểm tra các góc có người không"
            elif arg:
                text = f"kiểm tra góc {arg} có người không"
            else:
                text = "camera có người không"
            outcome = await act_camera_person(db, cam, normalize(text))
            return outcome or {"reply": "Mình chưa hiểu câu này.",
                               "action": {"tool": name, "ok": False}}

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

        if name == "get_datetime":
            part = str(args.get("part", "date"))
            day = day_hint or {"tomorrow": 1, "day_after": 2}.get(str(args.get("day", "")), 0)
            if part == "time":
                return act_datetime(ask_time=True)
            return act_datetime(ask_date=True, ask_lunar=part == "lunar", day=day)

        if name == "set_timer":
            rep = {"none": "", "daily": "daily", "weekdays": "0,1,2,3,4",
                   "weekend": "5,6", "mon": "0", "tue": "1", "wed": "2",
                   "thu": "3", "fri": "4", "sat": "5", "sun": "6"}.get(
                str(args.get("repeat") or "none").lower(), "")
            day_off = {"today": 0, "tomorrow": 1, "day_after": 2}.get(str(args.get("day") or ""))
            action = None
            if args.get("entity") and args.get("state"):
                ent = resolve_entity(db, str(args["entity"]), room_id)
                if ent and ent["entity_type"] != "controller" \
                        and not loads(ent["driver_data"]).get("readonly"):
                    mentioned = _room_from_text(
                        db, normalize(f"{args['entity']} {args.get('room') or ''}"))
                    action = _timer_action(db, ent, args["state"] == "on",
                                           args.get("temperature"), mentioned)
            return act_timer(db, room_id, args.get("message"),
                             minutes=args.get("minutes"), time_str=args.get("time"),
                             repeat=rep, day_offset=day_off, action=action)

        if name == "list_timers":
            return act_list_timers(db)

        if name == "cancel_timer":
            q = str(args.get("query") or "").strip().lower()
            return act_cancel_timer(
                db, room_id,
                query="hủy hết hẹn giờ" if q in ("all", "tat ca", "tất cả")
                else f"hủy hẹn giờ {q}".strip())

        if name == "move_timer":
            parts = ["dời hẹn giờ", str(args.get("query") or "")]
            shift = args.get("shift_minutes")
            if shift is not None:
                parts.append(f"{'sớm hơn' if shift < 0 else 'thêm'} {abs(int(shift))} phút")
            if args.get("time"):
                parts.append(f"tới {args['time']}")
                if args.get("day") == "tomorrow":
                    parts.append("ngày mai")
                elif args.get("day") == "day_after":
                    parts.append("ngày mốt")
            return act_move_timer(db, room_id, " ".join(parts))

        return {"reply": f"Công cụ '{name}' chưa được hỗ trợ.", "action": {"tool": name, "ok": False}}
    except DriverError as exc:
        return {"reply": str(exc), "action": {"tool": name, "ok": False}}
    except Exception as exc:
        return {"reply": f"Lỗi khi thực hiện: {exc}", "action": {"tool": name, "ok": False}}


async def llm_path(db, drivers, cfg, weather, text: str, room_id) -> dict:
    system = (
        "Bạn là trợ lý nhà thông minh tiếng Việt. "
        f"Thời gian hiện tại: {now().strftime('%H:%M, %d/%m/%Y')}.\n"
        f"{_llm_context(db, room_id)}\n"
        "Ưu tiên gọi công cụ để điều khiển thiết bị. Chỉ trả lời văn bản khi không có công cụ phù hợp. "
        "Khi hỏi thời tiết ngày mai/mốt, gọi get_weather với day tương ứng. "
        "Khi hỏi giờ, ngày/thứ/âm lịch, gọi get_datetime với part và day tương ứng. "
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

    ask_time, ask_date, ask_lunar = datetime_question(norm)
    if (ask_time or ask_date) and "get_datetime" not in executed \
            and not _timer_intent(norm) and not re.search(r"\b(bat|mo|tat)\b", norm):
        extra = act_datetime(ask_time=ask_time, ask_date=ask_date, ask_lunar=ask_lunar,
                             day=weather_day(norm) if ask_date else 0)
        replies.append(extra["reply"])
        actions.append(extra["action"])

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

    # lời đáp xác nhận hủy hẹn giờ đang chờ (theo phòng; hết hạn hoặc câu
    # chuyện khác = tự bỏ, câu mới xử lý bình thường)
    key = room_id if room_id is not None else 0
    pending = _PENDING_CANCEL.get(key)
    if pending:
        answer = _confirm_answer(normalize(text))
        if now() > pending["expires"] or answer:
            _PENDING_CANCEL.pop(key, None)
        if answer == "yes":
            for tid in pending["ids"]:
                db.execute("UPDATE timers SET done=1 WHERE id=? AND done=0", (tid,))
            return {"reply": f"Đã hủy {pending['label']}.",
                    "actions": [{"tool": "cancel_timer", "ok": True}], "engine": "fast"}
        if answer == "no":
            return {"reply": "Ok, mình giữ nguyên hẹn giờ.",
                    "actions": [{"tool": "cancel_timer", "ok": False}], "engine": "fast"}

    try:
        outcome = await fast_path(db, drivers, weather, text, room_id)
        if outcome:
            return {"reply": outcome["reply"], "actions": [outcome["action"]], "engine": "fast"}
    except DriverError as exc:
        return {"reply": str(exc), "actions": [], "engine": "fast"}
    except Exception as exc:
        return {"reply": f"Lỗi khi thực hiện: {exc}", "actions": [], "engine": "fast"}

    return await llm_path(db, drivers, cfg, weather, text, room_id)
