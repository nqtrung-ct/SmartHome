"""Public endpoints: health, pairing, client self-info; plus rooms/floors CRUD."""
from __future__ import annotations

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
    """Read-only snapshot for wall displays. Public (LAN-only server, no secrets)."""
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
            """SELECT e.name, e.entity_type, e.state FROM entities e
               JOIN devices d ON d.id=e.device_id WHERE d.room_id=?""",
            (room["id"],),
        )
        ents = []
        lights_on = active = 0
        temp = humidity = None
        for e in entities:
            try:
                state = _json.loads(e["state"] or "{}")
            except ValueError:
                state = {}
            ents.append({"name": e["name"], "type": e["entity_type"], "state": state})
            if e["entity_type"] in ("light", "switch"):
                if state.get("state") == "on":
                    lights_on += 1
                    active += 1
            elif e["entity_type"] == "climate" and state.get("state") not in (None, "off"):
                active += 1
            elif e["entity_type"] == "sensor":
                temp = state.get("temperature", temp)
                humidity = state.get("humidity", humidity)
        rooms.append({
            "id": room["id"], "name": room["name"], "floor": room["floor"],
            "active": active > 0, "lights_on": lights_on,
            "temperature": temp, "humidity": humidity,
            "entity_count": len(ents),
        })

    timers = [
        {"target_at": t["target_at"], "message": t["message"], "room": t["room_name"]}
        for t in db.query(
            """SELECT t.target_at, t.message, r.name AS room_name FROM timers t
               LEFT JOIN rooms r ON r.id=t.room_id
               WHERE t.done=0 ORDER BY t.target_at LIMIT 5"""
        )
    ]

    counts = {
        "rooms": len(rooms),
        "devices": db.one("SELECT COUNT(*) AS n FROM devices")["n"],
        "on_now": sum(r["active"] for r in rooms),
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
        "updated_at": now_iso(),
    }


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
    if body.weather_latitude is not None:
        apply("weather", "latitude", float(body.weather_latitude))
    if body.weather_longitude is not None:
        apply("weather", "longitude", float(body.weather_longitude))
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

    # live effects
    zigbee = app.state.drivers.get("zigbee")
    if zigbee is not None and body.mqtt_enabled is not None:
        if body.mqtt_enabled:
            zigbee.start()
        else:
            zigbee.stop()

    log_event(app.state.db, "settings", "Đã lưu cài đặt: " + ("; ".join(changed) or "không đổi"))
    return {"ok": True, "changed": changed, "admin_token_changed": token_changed}
