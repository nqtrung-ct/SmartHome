"""Devices, entities, IR learning, direct commands, demo seeding."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from server.api.deps import require_admin
from server.db import log_event
from server.drivers.base import DriverError, loads
from server.util import now_iso

router = APIRouter()


def _device_with_entities(request: Request, row) -> dict:
    db = request.app.state.db
    room = db.one("SELECT name FROM rooms WHERE id=?", (row["room_id"],)) if row["room_id"] else None
    entities = [dict(e) for e in db.query(
        "SELECT * FROM entities WHERE device_id=? ORDER BY id", (row["id"],)
    )]
    return {**dict(row), "room_name": room["name"] if room else None, "entities": entities}


# --- devices ----------------------------------------------------------------


class DeviceIn(BaseModel):
    name: str
    driver: str = "generic"
    host: str = ""
    mac: str = ""
    room_id: int | None = None
    extra: dict = {}


class DevicePatch(BaseModel):
    name: str | None = None
    host: str | None = None
    room_id: int | None = None


@router.get("/devices")
def list_devices(request: Request, _: None = Depends(require_admin)):
    return [
        _device_with_entities(request, d)
        for d in request.app.state.db.query("SELECT * FROM devices ORDER BY id")
    ]


@router.post("/devices", status_code=201)
async def create_device(request: Request, body: DeviceIn, _: None = Depends(require_admin)):
    app = request.app
    db = app.state.db
    if body.driver not in app.state.drivers:
        raise HTTPException(status_code=400, detail=f"Không có driver '{body.driver}'")
    extra = dict(body.extra)
    if body.driver == "broadlink" and body.host and not extra.get("mac"):
        # try to fill mac/devtype from the LAN automatically
        try:
            info = await asyncio.to_thread(app.state.drivers["broadlink"].probe, {
                "host": body.host, "mac": "", "extra": "{}"
            })
            extra.update(info)
        except Exception as exc:
            log_event(db, "broadlink", f"Tự nhận diện {body.host} thất bại: {exc}", level="warn")
    device_id = db.execute(
        "INSERT INTO devices(name, driver, host, mac, extra, room_id, created_at) VALUES (?,?,?,?,?,?,?)",
        (body.name.strip(), body.driver, body.host.strip(), body.mac.strip(),
         json.dumps(extra, ensure_ascii=False), body.room_id, now_iso()),
    )
    log_event(db, "admin", f"Đã thêm thiết bị '{body.name}' ({body.driver})")
    return {"id": device_id}


@router.patch("/devices/{device_id}")
def patch_device(request: Request, device_id: int, body: DevicePatch, _: None = Depends(require_admin)):
    db = request.app.state.db
    if not db.one("SELECT id FROM devices WHERE id=?", (device_id,)):
        raise HTTPException(status_code=404, detail="Không có thiết bị")
    if body.name is not None:
        db.execute("UPDATE devices SET name=? WHERE id=?", (body.name.strip(), device_id))
    if body.host is not None:
        db.execute("UPDATE devices SET host=? WHERE id=?", (body.host.strip(), device_id))
    if body.room_id is not None:
        db.execute("UPDATE devices SET room_id=? WHERE id=?", (body.room_id, device_id))
    return {"ok": True}


@router.delete("/devices/{device_id}")
def delete_device(request: Request, device_id: int, _: None = Depends(require_admin)):
    request.app.state.db.execute("DELETE FROM devices WHERE id=?", (device_id,))
    return {"ok": True}


# --- entities ---------------------------------------------------------------


class EntityIn(BaseModel):
    name: str
    entity_type: str = "switch"
    aliases: str = ""
    driver_data: dict = {}


class EntityPatch(BaseModel):
    name: str | None = None
    aliases: str | None = None
    driver_data: dict | None = None


@router.post("/devices/{device_id}/entities", status_code=201)
def create_entity(request: Request, device_id: int, body: EntityIn, _: None = Depends(require_admin)):
    db = request.app.state.db
    if not db.one("SELECT id FROM devices WHERE id=?", (device_id,)):
        raise HTTPException(status_code=404, detail="Không có thiết bị")
    if body.entity_type not in ("switch", "light", "climate", "sensor", "camera"):
        raise HTTPException(status_code=400, detail="entity_type không hợp lệ")
    entity_id = db.execute(
        "INSERT INTO entities(device_id, name, entity_type, aliases, driver_data) VALUES (?,?,?,?,?)",
        (device_id, body.name.strip(), body.entity_type, body.aliases.strip(),
         json.dumps(body.driver_data, ensure_ascii=False)),
    )
    return {"id": entity_id}


@router.patch("/entities/{entity_id}")
def patch_entity(request: Request, entity_id: int, body: EntityPatch, _: None = Depends(require_admin)):
    db = request.app.state.db
    row = db.one("SELECT * FROM entities WHERE id=?", (entity_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Không có entity")
    if body.name is not None:
        db.execute("UPDATE entities SET name=? WHERE id=?", (body.name.strip(), entity_id))
    if body.aliases is not None:
        db.execute("UPDATE entities SET aliases=? WHERE id=?", (body.aliases.strip(), entity_id))
    if body.driver_data is not None:
        db.execute(
            "UPDATE entities SET driver_data=? WHERE id=?",
            (json.dumps(body.driver_data, ensure_ascii=False), entity_id),
        )
    return {"ok": True}


@router.delete("/entities/{entity_id}")
def delete_entity(request: Request, entity_id: int, _: None = Depends(require_admin)):
    request.app.state.db.execute("DELETE FROM entities WHERE id=?", (entity_id,))
    return {"ok": True}


# --- control ----------------------------------------------------------------


class CommandIn(BaseModel):
    command: str  # on | off | toggle | set
    params: dict = {}


@router.post("/entities/{entity_id}/command")
async def entity_command(request: Request, entity_id: int, body: CommandIn, _: None = Depends(require_admin)):
    app = request.app
    db = app.state.db
    entity = db.one("SELECT * FROM entities WHERE id=?", (entity_id,))
    if not entity:
        raise HTTPException(status_code=404, detail="Không có entity")
    device = db.one("SELECT * FROM devices WHERE id=?", (entity["device_id"],))
    driver = app.state.drivers.get(device["driver"])
    if not driver:
        raise HTTPException(status_code=400, detail=f"Không có driver '{device['driver']}'")
    try:
        state = await asyncio.to_thread(driver.set_state, entity, device, body.command, body.params)
    except DriverError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.execute(
        "UPDATE entities SET state=?, updated_at=? WHERE id=?",
        (json.dumps(state, ensure_ascii=False), now_iso(), entity_id),
    )
    return {"state": state}


@router.get("/entities/{entity_id}/state")
async def entity_state(request: Request, entity_id: int, _: None = Depends(require_admin)):
    app = request.app.state
    entity = app.db.one("SELECT * FROM entities WHERE id=?", (entity_id,))
    if not entity:
        raise HTTPException(status_code=404, detail="Không có entity")
    device = app.db.one("SELECT * FROM devices WHERE id=?", (entity["device_id"],))
    try:
        state = await asyncio.to_thread(app.drivers[device["driver"]].get_state, entity, device)
        return {"state": state, "updated_at": entity["updated_at"]}
    except DriverError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --- IR learning --------------------------------------------------------------


class LearnIn(BaseModel):
    button: str  # code name, e.g. "off", "cool_26", "toggle"


@router.post("/entities/{entity_id}/learn")
async def start_learn(request: Request, entity_id: int, body: LearnIn,
                      _: None = Depends(require_admin)):
    """Learn one IR code into the entity's code map. Poll GET for the result."""
    app = request.app.state
    entity = app.db.one("SELECT * FROM entities WHERE id=?", (entity_id,))
    if not entity:
        raise HTTPException(status_code=404, detail="Không có entity")
    device = app.db.one("SELECT * FROM devices WHERE id=?", (entity["device_id"],))
    driver = app.drivers.get(device["driver"])
    if not driver or not hasattr(driver, "learn_ir"):
        raise HTTPException(status_code=400, detail="Thiết bị này không hỗ trợ học IR")

    jobs: dict = app.learn_jobs
    jobs[entity_id] = {"status": "learning", "button": body.button}

    async def run():
        try:
            code_hex = await asyncio.to_thread(driver.learn_ir, device, 30)
            if not code_hex:
                jobs[entity_id] = {**jobs[entity_id], "status": "timeout"}
                return
            data = loads(entity["driver_data"])
            data.setdefault("codes", {})[body.button] = code_hex
            app.db.execute(
                "UPDATE entities SET driver_data=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), entity_id),
            )
            log_event(app.db, "broadlink", f"Đã học mã '{body.button}' cho '{entity['name']}'")
            jobs[entity_id] = {**jobs[entity_id], "status": "done", "code": code_hex}
        except Exception as exc:
            jobs[entity_id] = {**jobs[entity_id], "status": "error", "error": str(exc)}

    asyncio.create_task(run())
    return {"status": "learning"}


@router.get("/entities/{entity_id}/learn")
def learn_status(request: Request, entity_id: int, _: None = Depends(require_admin)):
    job = request.app.state.learn_jobs.get(entity_id)
    if not job:
        raise HTTPException(status_code=404, detail="Chưa có phiên học nào")
    return job


# --- demo seeding --------------------------------------------------------------


@router.post("/demo", status_code=201)
def seed_demo(request: Request, _: None = Depends(require_admin)):
    """Rooms + a virtual device so the whole stack can be tested without hardware."""
    app = request.app.state
    db = app.db
    if not db.one("SELECT id FROM rooms WHERE name='Phòng khách'"):
        db.execute("INSERT INTO rooms(name, aliases) VALUES ('Phòng khách','khách,khach')")
        db.execute("INSERT INTO rooms(name, aliases) VALUES ('Phòng ngủ','ngủ,ngu')")
    living = db.one("SELECT id FROM rooms WHERE name='Phòng khách'")
    device_id = db.execute(
        "INSERT INTO devices(name, driver, host, room_id, created_at) VALUES (?,?,?,?,?)",
        ("Thiết bị demo", "dummy", "", living["id"], now_iso()),
    )
    for name, etype, aliases in [
        ("Đèn trần", "light", "đèn,đèn trần"),
        ("Quạt", "switch", "quạt"),
        ("Điều hòa", "climate", "điều hòa,máy lạnh,ac"),
        ("Cảm biến", "sensor", "cảm biến,nhiệt độ,độ ẩm"),
    ]:
        db.execute(
            "INSERT INTO entities(device_id, name, entity_type, aliases, driver_data) VALUES (?,?,?,?,?)",
            (device_id, name, etype, aliases, "{}"),
        )
    log_event(db, "admin", "Đã tạo dữ liệu demo (phòng + thiết bị ảo)")
    return {"ok": True, "device_id": device_id}
