"""Route spoken announcements to a camera, with an ESP fallback."""
from __future__ import annotations

import asyncio

from server.db import log_event
from server.drivers import imou_talk


async def announce(app, room_id, text: str, camera_id=None, source_device_id=None) -> str:
    """Return camera/esp/none; a camera result means data was sent, not heard."""
    db = app.state.db
    if camera_id is not None:
        rows = db.query("""SELECT d.host, d.mac, d.extra FROM entities e
            JOIN devices d ON d.id=e.device_id
            WHERE e.id=? AND e.entity_type='camera' AND d.host != ''""", (camera_id,))
    elif room_id is not None:
        rows = db.query("""SELECT d.host, d.mac, d.extra FROM entities e
            JOIN devices d ON d.id=e.device_id
            WHERE e.entity_type='camera' AND COALESCE(e.room_id,d.room_id)=?
              AND d.host != '' ORDER BY e.id""", (room_id,))
    else:
        rows = []
    pipeline = getattr(app.state, "pipeline", None)
    if rows and pipeline:
        try:
            audio = await pipeline.tts_audio(text)
            if audio:
                # Choose a single camera in a room, unless a specific camera was named.
                await asyncio.to_thread(imou_talk.speak, app.state.cfg, dict(rows[0]), audio[0])
                log_event(db, "camera", "Đã gửi âm thanh tới camera")
                return "camera"
        except Exception as exc:
            log_event(db, "camera", f"Không gửi được âm thanh camera: {type(exc).__name__}", level="warn")
    from server.api.xiaozhi import connections

    if source_device_id is not None:
        target = db.one("SELECT id FROM xiaozhi_devices WHERE id=? AND approved=1", (source_device_id,))
        if target:
            connections.enqueue_announce(target["id"], text)
            log_event(db, "xiaozhi", "Đã xếp thông báo cho thiết bị nhận lệnh")
            return "esp"
    if room_id is not None:
        targets = db.query("SELECT id FROM xiaozhi_devices WHERE room_id=? AND approved=1", (room_id,))
        if targets:
            for target in targets:
                connections.enqueue_announce(target["id"], text)
            log_event(db, "xiaozhi", "Đã xếp thông báo cho thiết bị trong phòng")
            return "esp"
    log_event(db, "camera", "Không có camera hoặc thiết bị phòng để phát thông báo", level="warn")
    return "none"
