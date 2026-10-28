"""Console + shared read endpoints: overview, text command, timers, events."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from server.api.deps import require_admin
from server.core.text_command import handle_command
from server.db import log_event

router = APIRouter()


@router.get("/overview")
def overview(request: Request, _: None = Depends(require_admin)):
    db = request.app.state.db
    rooms = []
    for room in db.query("SELECT * FROM rooms ORDER BY name"):
        entities = db.query(
            """SELECT e.*, d.name AS device_name, d.driver FROM entities e
               JOIN devices d ON d.id=e.device_id WHERE d.room_id=? ORDER BY e.id""",
            (room["id"],),
        )
        rooms.append({
            **dict(room),
            "entities": [
                {
                    "id": e["id"], "name": e["name"], "entity_type": e["entity_type"],
                    "state": e["state"], "device_name": e["device_name"], "driver": e["driver"],
                    "updated_at": e["updated_at"],
                }
                for e in entities
            ],
        })
    unassigned = db.query(
        """SELECT e.*, d.name AS device_name, d.driver FROM entities e
           JOIN devices d ON d.id=e.device_id WHERE d.room_id IS NULL ORDER BY e.id"""
    )
    return {
        "rooms": rooms,
        "unassigned": [
            {"id": e["id"], "name": e["name"], "entity_type": e["entity_type"],
             "device_name": e["device_name"], "state": e["state"]}
            for e in unassigned
        ],
        "counts": {
            "devices": db.one("SELECT COUNT(*) AS n FROM devices")["n"],
            "entities": db.one("SELECT COUNT(*) AS n FROM entities")["n"],
            "voice_clients": db.one("SELECT COUNT(*) AS n FROM voice_clients")["n"],
            "timers_pending": db.one("SELECT COUNT(*) AS n FROM timers WHERE done=0")["n"],
        },
    }


class CommandIn(BaseModel):
    text: str
    room_id: int | None = None


@router.post("/command")
async def command(request: Request, body: CommandIn, _: None = Depends(require_admin)):
    """Admin console: send a text command as if spoken in a room."""
    app = request.app.state
    result = await handle_command(app.db, app.drivers, app.cfg, app.weather, body.text, body.room_id)
    log_event(
        app.db, "console",
        f"[{result.get('engine', '?')}] '{body.text}' -> {result['reply']}",
    )
    return result


@router.get("/timers")
def timers(request: Request, _: None = Depends(require_admin)):
    db = request.app.state.db
    pending = db.query(
        """SELECT t.*, r.name AS room_name FROM timers t
           LEFT JOIN rooms r ON r.id=t.room_id WHERE t.done=0 ORDER BY t.target_at"""
    )
    recent = db.query(
        """SELECT t.*, r.name AS room_name FROM timers t
           LEFT JOIN rooms r ON r.id=t.room_id WHERE t.done=1
           ORDER BY t.id DESC LIMIT 20"""
    )
    return {"pending": [dict(t) for t in pending], "recent": [dict(t) for t in recent]}


@router.delete("/timers/{timer_id}")
def cancel_timer(request: Request, timer_id: int, _: None = Depends(require_admin)):
    db = request.app.state.db
    if not db.one("SELECT id FROM timers WHERE id=? AND done=0", (timer_id,)):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Không có hẹn giờ này")
    db.execute("UPDATE timers SET done=1 WHERE id=?", (timer_id,))
    return {"ok": True}


@router.get("/events")
def events(request: Request, limit: int = 200, _: None = Depends(require_admin)):
    rows = request.app.state.db.query(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(limit, 1000),)
    )
    return [dict(r) for r in rows]
