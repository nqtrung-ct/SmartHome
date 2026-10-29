"""Public endpoints: health, pairing, client self-info; plus rooms/floors CRUD."""
from __future__ import annotations

import asyncio
import secrets
import sqlite3
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from server.api.deps import require_admin, voice_identity
from server.db import log_event
from server.util import now_iso

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/display")
def display(request: Request):
    """Snapshot cho màn hình treo tường. Public (server LAN-only, không có secret).
    Chỉ trả các phòng có cảm biến nhiệt độ; kèm thiết bị điều khiển của phòng
    (sensor/breaker không hiện — chỉ dùng để lọc phòng và lấy nhiệt độ)."""
    import json as _json

    app = request.app
    db = app.state.db

    weather = None
    try:
        weather = app.state.weather.snapshot()
    except Exception:
        weather = None

    rooms = []
    for room in db.query("SELECT * FROM rooms ORDER BY name"):
        entities = db.query(
            """SELECT e.id, e.name, e.entity_type, e.state, e.driver_data, d.driver
               FROM entities e JOIN devices d ON d.id=e.device_id
               WHERE COALESCE(e.room_id, d.room_id)=? AND e.entity_type NOT IN ('controller','sensor')""",
            (room["id"],),
        )
        temp = humidity = None
        # nhiệt độ/độ ẩm phòng lấy từ sensor trong phòng (nếu có nhiều cái, số mới nhất Wins)
        for s in db.query(
            """SELECT e.state, e.updated_at FROM entities e JOIN devices d ON d.id=e.device_id
               WHERE COALESCE(e.room_id, d.room_id)=? AND e.entity_type='sensor'
               ORDER BY e.updated_at DESC""",
            (room["id"],),
        ):
            try:
                st = _json.loads(s["state"] or "{}")
            except ValueError:
                continue
            if temp is None and st.get("temperature") is not None:
                temp = st["temperature"]
            if humidity is None and st.get("humidity") is not None:
                humidity = st["humidity"]
            if temp is not None and humidity is not None:
                break
        # cảm biến nhiệt/ẩm là phần cộng thêm — phòng không có vẫn lên màn hình
        # nếu có thiết bị; chỉ ẩn phòng trống hoàn toàn
        if not entities and temp is None and humidity is None:
            continue

        ents = []
        for e in entities:
            try:
                state = _json.loads(e["state"] or "{}")
            except ValueError:
                state = {}
            try:
                dd = _json.loads(e["driver_data"] or "{}")
            except ValueError:
                dd = {}
            ent = {
                "id": e["id"], "name": e["name"], "type": e["entity_type"],
                "driver": e["driver"], "state": state,
                # màn hình treo tường chỉ điều khiển thiết bị IR (remote);
                # Tuya/zigbee hiện trạng thái thôi, bấm không có gì xảy ra
                "controllable": e["driver"] == "broadlink"
                                and e["entity_type"] in ("light", "switch", "climate", "fan", "media")
                                and "readonly" not in dd,
            }
            if e["driver"] == "broadlink" and e["entity_type"] != "climate":
                # on/off/power = Bật/Tắt lớn, không lặp lại trong nút phụ
                ent["buttons"] = sorted(
                    k for k in dd.get("codes", {})
                    if k not in ("on", "off", "toggle", "power"))
            ents.append(ent)
        rooms.append({
            "id": room["id"], "name": room["name"], "floor": room["floor"],
            "temperature": temp, "humidity": humidity, "entities": ents,
        })

    timers = [
        {"target_at": t["target_at"], "message": t["message"], "room": t["room_name"]}
        for t in db.query(
            """SELECT t.target_at, t.message, r.name AS room_name FROM timers t
               LEFT JOIN rooms r ON r.id=t.room_id
               WHERE t.done=0 ORDER BY t.target_at LIMIT 5"""
        )
    ]

    # điện lưới: cầu dao Tuya (đầu vào lưới) + số Volt từ cảm biến cloud_meter.
    # Tắt cầu dao = mất điện lưới (nhà có điện mặt trời nên server vẫn chạy).
    # Khi cầu dao tắt, meter không đo được và DB giữ số Vol cũ -> số Vol chỉ
    # có nghĩa khi cầu dao đang bật; lúc đó Vol < 200 = sụt áp (cảnh báo vàng).
    grid = None
    sw = db.one(
        """SELECT e.state FROM entities e JOIN devices d ON d.id=e.device_id
           WHERE d.driver='tuya' AND e.entity_type='switch' ORDER BY e.id LIMIT 1"""
    )
    if sw:
        try:
            sw_state = _json.loads(sw["state"] or "{}")
        except ValueError:
            sw_state = {}
        voltage = None
        for m in db.query(
            """SELECT e.state, e.driver_data FROM entities e
               JOIN devices d ON d.id=e.device_id
               WHERE d.driver='tuya' AND e.entity_type='sensor' ORDER BY e.id"""
        ):
            if not _json.loads(m["driver_data"] or "{}").get("cloud_meter"):
                continue
            try:
                mv = _json.loads(m["state"] or "{}").get("voltage")
            except ValueError:
                mv = None
            if mv is not None:
                voltage = mv
                break
        on = sw_state.get("state") == "on"
        # Vol = 0 khi công tắc bật: khả năng cao vừa có điện lại, số cloud chưa
        # kịp về — không cảnh báo vàng; chỉ cảnh báo khi thật sự sụt áp (0 < Vol < 200)
        grid = {
            "on": on,
            "voltage": voltage,
            "low_voltage": on and voltage is not None and 0 < voltage < 200,
        }

    # chỉ để tương thích client cũ — màn hình mới không dùng các số này nữa
    counts = {
        "rooms": len(rooms),
        "devices": db.one("SELECT COUNT(*) AS n FROM devices")["n"],
        "sensors": db.one(
            "SELECT COUNT(*) AS n FROM entities WHERE entity_type='sensor'"
        )["n"],
        "cameras": db.one(
            "SELECT COUNT(*) AS n FROM entities WHERE entity_type='camera'"
        )["n"],
        "climates": db.one(
            "SELECT COUNT(*) AS n FROM entities WHERE entity_type='climate'"
        )["n"],
    }
    return {
        "weather": weather,
        "rooms": rooms,
        "timers": timers,
        "counts": counts,
        "grid": grid,
        "updated_at": now_iso(),
    }


# --- wall display: PIN unlock + control ---------------------------------------
# Màn hình treo tường không có token admin; nhập đúng mã PIN 4 số (đặt trong
# tab Thiết đặt) sẽ nhận token tạm hiệu lực 5 phút để gửi lệnh điều khiển.


DISPLAY_UNLOCK_SECONDS = 300


def _display_tokens(app) -> dict:
    if not hasattr(app.state, "display_tokens"):
        app.state.display_tokens = {}
    return app.state.display_tokens


class DisplayUnlockIn(BaseModel):
    pin: str


@router.post("/display/unlock")
async def display_unlock(request: Request, body: DisplayUnlockIn):
    pin = str(body.pin or "").strip()
    expected = str(request.app.state.cfg.get("display.pin", "") or "").strip()
    if not expected:
        raise HTTPException(status_code=400, detail="Chưa bật điều khiển trên màn hình")
    if pin != expected:
        await asyncio.sleep(1)  # làm chậm dò mã
        raise HTTPException(status_code=401, detail="Mã PIN không đúng")
    token = secrets.token_hex(16)
    tokens = _display_tokens(request.app)
    now = datetime.now()
    for k in [k for k, exp in tokens.items() if exp < now]:
        del tokens[k]  # dọn token hết hạn luôn
    tokens[token] = now + timedelta(seconds=DISPLAY_UNLOCK_SECONDS)
    log_event(request.app.state.db, "display", "Màn hình mở khóa điều khiển")
    return {"token": token, "expires_in": DISPLAY_UNLOCK_SECONDS}


class DisplayCommandIn(BaseModel):
    token: str
    entity: int
    command: str  # on | off | set
    params: dict = {}


@router.post("/display/command")
async def display_command(request: Request, body: DisplayCommandIn):
    tokens = _display_tokens(request.app)
    expiry = tokens.get(str(body.token or ""))
    if not expiry or expiry < datetime.now():
        raise HTTPException(status_code=401, detail="Phiên mở khóa hết hạn, nhập lại mã PIN")
    from server.api.devices import run_entity_command
    state = await run_entity_command(request.app, body.entity, body.command, body.params)
    return {"state": state}


@router.post("/pair", status_code=201)
def pair(request: Request, body: dict):
    """Pair an Android phone with a one-time code shown in the admin UI."""
    db = request.app.state.db
    code = str(body.get("code", "")).strip()
    name = str(body.get("client_name", "")).strip() or "Android"
    row = db.one("SELECT * FROM pairing_codes WHERE code=? AND used=0", (code,))
    if not row or datetime.fromisoformat(row["expires_at"]) < datetime.now():
        raise HTTPException(status_code=400, detail="Mã ghép nối không đúng hoặc đã hết hạn")
    db.execute("UPDATE pairing_codes SET used=1 WHERE id=?", (row["id"],))
    token = secrets.token_hex(24)
    client_id = db.execute(
        "INSERT INTO voice_clients(name, token, created_at) VALUES (?,?,?)",
        (name, token, now_iso()),
    )
    log_event(db, "voice", f"Đã ghép nối máy '{name}' (client #{client_id})")
    return {"client_id": client_id, "token": token, "room": None}


@router.get("/me")
def me(request: Request, token: str = Query("")):
    identity = voice_identity(request.app, token)
    if not identity:
        raise HTTPException(status_code=401, detail="Sai token")
    db = request.app.state.db
    result = {"name": identity["name"], "role": identity["kind"], "room": None, "entities": []}
    room_id = identity.get("room_id")
    if room_id:
        room = db.one("SELECT * FROM rooms WHERE id=?", (room_id,))
        if room:
            result["room"] = room["name"]
            entities = db.query(
                """SELECT e.* FROM entities e JOIN devices d ON d.id=e.device_id
                   WHERE d.room_id=?""",
                (room_id,),
            )
            result["entities"] = [
                {"id": e["id"], "name": e["name"], "type": e["entity_type"]}
                for e in entities
            ]
    return result


# --- rooms -----------------------------------------------------------------


class RoomIn(BaseModel):
    name: str
    floor: str = ""
    aliases: str = ""


class RoomPatch(BaseModel):
    name: str | None = None
    floor: str | None = None
    aliases: str | None = None


@router.get("/rooms")
def list_rooms(request: Request, _: None = Depends(require_admin)):
    db = request.app.state.db
    rooms = []
    for r in db.query("SELECT * FROM rooms ORDER BY name"):
        count = db.one(
            "SELECT COUNT(*) AS n FROM entities e JOIN devices d ON d.id=e.device_id WHERE d.room_id=?",
            (r["id"],),
        )["n"]
        rooms.append({**dict(r), "entity_count": count})
    return rooms


@router.post("/rooms", status_code=201)
def create_room(request: Request, body: RoomIn, _: None = Depends(require_admin)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Thiếu tên phòng")
    return {
        "id": request.app.state.db.execute(
            "INSERT INTO rooms(name, floor, aliases) VALUES (?,?,?)",
            (body.name.strip(), body.floor.strip(), body.aliases.strip()),
        )
    }


@router.patch("/rooms/{room_id}")
def patch_room(request: Request, room_id: int, body: RoomPatch, _: None = Depends(require_admin)):
    db = request.app.state.db
    if body.name is not None:
        db.execute("UPDATE rooms SET name=? WHERE id=?", (body.name.strip(), room_id))
    if body.floor is not None:
        db.execute("UPDATE rooms SET floor=? WHERE id=?", (body.floor.strip(), room_id))
    if body.aliases is not None:
        db.execute("UPDATE rooms SET aliases=? WHERE id=?", (body.aliases.strip(), room_id))
    return {"ok": True}


@router.delete("/rooms/{room_id}")
def delete_room(request: Request, room_id: int, _: None = Depends(require_admin)):
    db = request.app.state.db
    # explicit: entity rooms may have been added by ALTER TABLE (no FK enforcement
    # on old databases), so clear them before dropping the room
    db.execute("UPDATE entities SET room_id=NULL WHERE room_id=?", (room_id,))
    db.execute("DELETE FROM rooms WHERE id=?", (room_id,))
    return {"ok": True}


# --- floors -----------------------------------------------------------------


@router.get("/floors")
def list_floors(request: Request, _: None = Depends(require_admin)):
    return request.app.state.db.query("SELECT * FROM floors ORDER BY sort, id")


class FloorIn(BaseModel):
    name: str


@router.post("/floors", status_code=201)
def create_floor(request: Request, body: FloorIn, _: None = Depends(require_admin)):
    db = request.app.state.db
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Thiếu tên tầng")
    try:
        floor_id = db.execute(
            "INSERT INTO floors(name, sort) VALUES (?,(SELECT COALESCE(MAX(sort),-1)+1 FROM floors))",
            (name,),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail=f"Tầng '{name}' đã có rồi")
    return {"id": floor_id}


@router.patch("/floors/{floor_id}")
def rename_floor(request: Request, floor_id: int, body: FloorIn, _: None = Depends(require_admin)):
    db = request.app.state.db
    row = db.one("SELECT * FROM floors WHERE id=?", (floor_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Không có tầng này")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Thiếu tên tầng")
    try:
        with db.conn() as c:
            c.execute("UPDATE floors SET name=? WHERE id=?", (name, floor_id))
            c.execute("UPDATE rooms SET floor=? WHERE floor=?", (name, row["name"]))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail=f"Tầng '{name}' đã có rồi")
    return {"ok": True}


@router.delete("/floors/{floor_id}")
def delete_floor(request: Request, floor_id: int, _: None = Depends(require_admin)):
    db = request.app.state.db
    row = db.one("SELECT * FROM floors WHERE id=?", (floor_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Không có tầng này")
    with db.conn() as c:
        # rooms keep existing, they just lose their floor
        c.execute("UPDATE rooms SET floor='' WHERE floor=?", (row["name"],))
        c.execute("DELETE FROM floors WHERE id=?", (floor_id,))
    return {"ok": True}


# --- pairing codes -----------------------------------------------------------


@router.post("/pairing-codes", status_code=201)
def create_pairing_code(request: Request, _: None = Depends(require_admin)):
    db = request.app.state.db
    code = f"{secrets.randbelow(1000000):06d}"
    expires = (datetime.now() + timedelta(minutes=15)).isoformat(timespec="seconds")
    db.execute("INSERT INTO pairing_codes(code, expires_at) VALUES (?,?)", (code, expires))
    return {"code": code, "expires_at": expires}


@router.get("/voice-clients")
def list_voice_clients(request: Request, _: None = Depends(require_admin)):
    db = request.app.state.db
    connections = getattr(request.app.state, "voice_connections", None)
    live = set(connections._clients) if connections else set()
    out = []
    for row in db.query(
        """SELECT v.id, v.name, v.room_id, v.created_at, v.last_seen, r.name AS room_name
           FROM voice_clients v LEFT JOIN rooms r ON r.id=v.room_id ORDER BY v.id"""
    ):
        out.append({**dict(row), "online": row["id"] in live})
    return out


class ClientPatch(BaseModel):
    name: str | None = None
    room_id: int | None = None


@router.patch("/voice-clients/{client_id}")
def patch_client(request: Request, client_id: int, body: ClientPatch, _: None = Depends(require_admin)):
    db = request.app.state.db
    if body.name is not None:
        db.execute("UPDATE voice_clients SET name=? WHERE id=?", (body.name.strip(), client_id))
    if body.room_id is not None:
        db.execute("UPDATE voice_clients SET room_id=? WHERE id=?", (body.room_id, client_id))
    return {"ok": True}


@router.delete("/voice-clients/{client_id}")
def delete_client(request: Request, client_id: int, _: None = Depends(require_admin)):
    request.app.state.db.execute("DELETE FROM voice_clients WHERE id=?", (client_id,))
    return {"ok": True}


# --- settings ----------------------------------------------------------------


from server.util import lan_ip, set_yaml_value  # noqa: E402


@router.get("/settings")
def get_settings(request: Request, _: None = Depends(require_admin)):
    cfg = request.app.state.cfg
    return {
        "mqtt": {
            "enabled": bool(cfg.get("mqtt.enabled", False)),
            "host": cfg.get("mqtt.host", "127.0.0.1"),
            "port": cfg.get("mqtt.port", 1883),
            "base_topic": cfg.get("mqtt.base_topic", "zigbee2mqtt"),
        },
        "discovery": {
            "network_scan": bool(cfg.get("discovery.network_scan", True)),
            "subnet": cfg.get("discovery.subnet", "auto"),
        },
        "weather": {
            "latitude": cfg.get("weather.latitude"),
            "longitude": cfg.get("weather.longitude"),
        },
        "server": {
            "port": cfg.get("server.port"),
            "advertise_mdns": bool(cfg.get("server.advertise_mdns", True)),
            "admin_token_set": bool(cfg.get("server.admin_token")),
            "lan_ip": lan_ip(),
        },
        "retention": {
            "days": int(cfg.get("retention.days", 30)),
        },
        "display": {
            "pin_set": bool(cfg.get("display.pin", "")),
        },
    }


class SettingsIn(BaseModel):
    mqtt_enabled: bool | None = None
    mqtt_host: str | None = None
    mqtt_port: int | None = None
    mqtt_base_topic: str | None = None
    discovery_network_scan: bool | None = None
    discovery_subnet: str | None = None
    weather_latitude: float | None = None
    weather_longitude: float | None = None
    advertise_mdns: bool | None = None  # applies after restart
    retention_days: int | None = None
    admin_token: str | None = None
    display_pin: str | None = None  # None=giữ nguyên, ""=xóa, "1234"=đặt mã mới


@router.post("/settings")
def update_settings(request: Request, body: SettingsIn, _: None = Depends(require_admin)):
    """Persist settings to config.yaml and apply what can be applied live."""
    app = request.app
    cfg = app.state.cfg
    changed = []

    def apply(section, key, value, *, live=True):
        cfg._data.setdefault(section, {})[key] = value
        set_yaml_value(cfg.path, section, key, value)
        changed.append(f"{section}.{key}")

    if body.mqtt_enabled is not None:
        apply("mqtt", "enabled", body.mqtt_enabled)
    if body.mqtt_host:
        apply("mqtt", "host", body.mqtt_host.strip())
    if body.mqtt_port:
        apply("mqtt", "port", int(body.mqtt_port))
    if body.mqtt_base_topic:
        apply("mqtt", "base_topic", body.mqtt_base_topic.strip())

    if body.discovery_network_scan is not None:
        apply("discovery", "network_scan", body.discovery_network_scan)
    if body.discovery_subnet is not None:
        apply("discovery", "subnet", body.discovery_subnet.strip() or "auto")
    weather_moved = False
    if body.weather_latitude is not None:
        apply("weather", "latitude", float(body.weather_latitude))
        weather_moved = True
    if body.weather_longitude is not None:
        apply("weather", "longitude", float(body.weather_longitude))
        weather_moved = True
    if weather_moved and getattr(app.state, "weather", None):
        app.state.weather._cache = None  # đổi vị trí -> bỏ cache 10 phút, lấy số mới ngay
    if body.advertise_mdns is not None:
        apply("server", "advertise_mdns", body.advertise_mdns)
    if body.retention_days is not None:
        if body.retention_days < 1:
            raise HTTPException(status_code=400, detail="Số ngày giữ nhật ký phải ≥ 1")
        apply("retention", "days", int(body.retention_days))

    token_changed = False
    if body.admin_token and body.admin_token.strip():
        new_token = body.admin_token.strip()
        if len(new_token) < 6:
            raise HTTPException(status_code=400, detail="Token quá ngắn (tối thiểu 6 ký tự)")
        apply("server", "admin_token", new_token)
        token_changed = True

    if body.display_pin is not None:
        pin = body.display_pin.strip()
        if pin and not (len(pin) == 4 and pin.isdigit()):
            raise HTTPException(status_code=400, detail="Mã PIN màn hình phải đúng 4 chữ số")
        apply("display", "pin", pin)

    # live effects
    zigbee = app.state.drivers.get("zigbee")
    if zigbee is not None and body.mqtt_enabled is not None:
        if body.mqtt_enabled:
            zigbee.start()
        else:
            zigbee.stop()

    log_event(app.state.db, "settings", "Đã lưu cài đặt: " + ("; ".join(changed) or "không đổi"))
    return {"ok": True, "changed": changed, "admin_token_changed": token_changed}
