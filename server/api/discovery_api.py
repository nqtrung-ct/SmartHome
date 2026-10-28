"""Discovery endpoints: scan (background job) + list + adopt."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from server.api.deps import require_admin
from server.core.discovery import run_scan
from server.db import log_event
from server.drivers.base import loads
from server.util import now_iso, store_secret

router = APIRouter()


@router.get("/discovery")
def list_discovered(request: Request, all: bool = False, _: None = Depends(require_admin)):
    """By default only supported (or already added) devices - the ping sweep
    finds the whole network and drowns the interesting rows."""
    where = "" if all else " WHERE supported=1 OR adopted_device_id IS NOT NULL"
    rows = request.app.state.db.query(
        "SELECT * FROM discovered" + where + " ORDER BY supported DESC, method, ip"
    )
    return [dict(r) for r in rows]


@router.post("/discovery/scan")
async def start_scan(request: Request, _: None = Depends(require_admin)):
    app = request.app
    job = getattr(app.state, "scan_job", None)
    if job and job.get("running"):
        return {"status": "already-running", "done": False, "methods": job.get("methods", [])}

    app.state.scan_job = {"running": True, "done": False, "methods": [], "started_at": now_iso()}
    log_event(app.state.db, "discovery", "Bắt đầu quét mạng")

    def progress(done: list[str]) -> None:
        app.state.scan_job["methods"] = done

    async def run():
        try:
            methods = await asyncio.to_thread(run_scan, app.state.db, app.state.cfg, progress)
            app.state.scan_job.update(running=False, done=True, methods=methods)
        except Exception as exc:
            app.state.scan_job.update(running=False, done=True, error=str(exc))

    asyncio.create_task(run())
    return {"status": "started", "done": False, "methods": []}


@router.get("/discovery/scan")
def scan_status(request: Request, _: None = Depends(require_admin)):
    job = getattr(request.app.state, "scan_job", None) or {"running": False, "done": True, "methods": []}
    return job


class DiscoveredPatch(BaseModel):
    adopted_device_id: int | None = None


@router.patch("/discovery/{discovered_id}")
def patch_discovered(request: Request, discovered_id: int, body: DiscoveredPatch, _: None = Depends(require_admin)):
    """Link a discovered row to a device (used after manual add from a scan row)."""
    request.app.state.db.execute(
        "UPDATE discovered SET adopted_device_id=? WHERE id=?",
        (body.adopted_device_id, discovered_id),
    )
    return {"ok": True}


class AdoptIn(BaseModel):
    name: str | None = None
    room_id: int | None = None
    local_key: str | None = None     # Tuya
    rtsp_password: str | None = None  # cameras - stored in data/secrets.json
    force: bool = False              # adopt unsupported device as "generic" (record only)


@router.post("/discovery/{discovered_id}/adopt", status_code=201)
async def adopt(request: Request, discovered_id: int, body: AdoptIn, _: None = Depends(require_admin)):
    app = request.app
    db = app.state.db
    row = db.one("SELECT * FROM discovered WHERE id=?", (discovered_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy thiết bị")
    if row["adopted_device_id"]:
        raise HTTPException(status_code=400, detail="Thiết bị đã được thêm rồi")

    raw = loads(row["raw"])
    method = row["method"]

    if method == "broadlink":
        driver_name = "broadlink"
        name = body.name or row["name"] or f"Broadlink {row['ip']}"
        extra = {"devtype": raw.get("devtype", 0)}
    elif method == "tuya":
        driver_name = "tuya"
        name = body.name or row["name"] or "Thiết bị Tuya"
        extra = {"id": row["ident"], "ver": raw.get("ver") or "3.3"}
        if not (body.local_key or raw.get("key")):
            raise HTTPException(
                status_code=400,
                detail="Thiết bị Tuya cần local key (lấy qua tài khoản Tuya IoT / "
                       "tinytuya wizard). Nhập key vào ô 🔑 bên cạnh rồi bấm lại.",
            )
    elif method == "zigbee":
        driver_name = "zigbee"
        name = body.name or row["name"] or row["ident"]
        extra = {"friendly_name": raw.get("friendly_name") or row["ident"]}
    elif method == "wsdiscovery":
        driver_name = "onvif"
        name = body.name or row["name"] or f"Camera {row['ip']}"
        extra = {"endpoint": raw.get("endpoint", "")}
    elif body.force:
        driver_name = "generic"
        name = body.name or row["name"] or f"Thiết bị {row['ip']}"
        extra = {"method": method}
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Thiết bị '{row['brand'] or row['name']}' chưa có driver. Bấm 'Thêm tay' nếu chỉ muốn ghi lại.",
        )

    device_id = db.execute(
        "INSERT INTO devices(name, driver, host, mac, extra, room_id, created_at) VALUES (?,?,?,?,?,?,?)",
        (name, driver_name, row["ip"], row["mac"], json.dumps(extra, ensure_ascii=False),
         body.room_id, now_iso()),
    )

    # cameras: keep the RTSP password in data/secrets.json, not in the DB/git
    if driver_name == "onvif" and body.rtsp_password and row["ip"]:
        store_secret(app.cfg, "rtsp", row["ip"], body.rtsp_password)

    driver = app.state.drivers.get(driver_name, app.state.drivers.get("generic"))
    entities = []
    error = None
    try:
        device_row = db.one("SELECT * FROM devices WHERE id=?", (device_id,))
        for spec in await asyncio.to_thread(driver.adopt, device_row, body.model_dump()):
            entities.append(
                db.execute(
                    "INSERT INTO entities(device_id, name, entity_type, aliases, driver_data) VALUES (?,?,?,?,?)",
                    (device_id, spec["name"], spec["entity_type"],
                     spec.get("aliases", ""), json.dumps(spec.get("driver_data", {}), ensure_ascii=False)),
                )
            )
    except Exception as exc:
        error = str(exc)

    db.execute("UPDATE discovered SET adopted_device_id=? WHERE id=?", (device_id, discovered_id))
    log_event(
        db, "discovery",
        f"Đã thêm '{name}' ({driver_name})" + (f", cảnh báo: {error}" if error else ""),
        level="warn" if error else "info",
    )
    return {"device_id": device_id, "entities": entities, "warning": error}
