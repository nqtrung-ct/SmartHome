"""Timer scheduler: checks due timers once per second, announces in the room."""
from __future__ import annotations

import asyncio
import json
import time

from server.db import Database, log_event
from server.util import now_iso, parse_iso


async def _poll_tuya_sensors(app) -> None:
    """Refresh tuya sensor/switch states for the dashboard.

    Battery-powered Tuya sensors sleep between reports and refuse connections;
    on failure keep the last known state quietly.
    """
    driver = app.state.drivers.get("tuya")
    if not driver:
        return
    db: Database = app.state.db
    rows = db.query(
        """SELECT e.id AS eid, e.entity_type AS etype, e.driver_data, d.id AS did, d.host, d.extra
           FROM entities e JOIN devices d ON d.id = e.device_id
           WHERE e.entity_type IN ('sensor', 'switch') AND d.driver='tuya'"""
    )
    for row in rows:
        entity = {"id": row["eid"], "driver_data": row["driver_data"]}
        device = {"id": row["did"], "host": row["host"], "extra": row["extra"]}
        try:
            state = await asyncio.to_thread(driver.get_state, entity, device)
        except Exception:
            # an unreachable breaker is not powering anything -> show it off
            if row["etype"] == "switch":
                db.execute(
                    "UPDATE entities SET state=?, updated_at=? WHERE id=?",
                    ('{"state": "off"}', now_iso(), row["eid"]),
                )
            continue
        db.execute(
            "UPDATE entities SET state=?, updated_at=? WHERE id=?",
            (json.dumps(state, ensure_ascii=False), now_iso(), row["eid"]),
        )


async def scheduler_loop(app) -> None:
    db: Database = app.state.db
    from server.db import cleanup_old_rows

    last_cleanup_day = None
    last_sensor_poll = 0.0
    while True:
        try:
            today = now_iso()[:10]
            if today != last_cleanup_day:  # first pass = startup, then once a day
                last_cleanup_day = today
                deleted = cleanup_old_rows(db, int(app.state.cfg.get("retention.days", 30)))
                if deleted:
                    parts = ", ".join(f"{t} {n}" for t, n in deleted.items())
                    log_event(db, "scheduler", f"Đã dọn dữ liệu cũ: {parts}")
            await asyncio.sleep(1)
            due = db.query(
                "SELECT * FROM timers WHERE done=0 AND target_at <= ?",
                (now_iso(),),
            )
            for timer in due:
                db.execute("UPDATE timers SET done=1 WHERE id=?", (timer["id"],))
                message = f"Đã đến giờ hẹn: {timer['message']}"
                log_event(db, "scheduler", f"Hẹn giờ đến: {timer['message']} (timer #{timer['id']})")
                connections = getattr(app.state, "voice_connections", None)
                pipeline = getattr(app.state, "pipeline", None)
                if connections:
                    await connections.announce_room(timer["room_id"], message, pipeline)
            # battery sensors surface briefly (~seconds every 15 min): poll often
            if time.time() - last_sensor_poll >= int(app.state.cfg.get("tuya.poll_seconds", 60)):
                last_sensor_poll = time.time()
                await _poll_tuya_sensors(app)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log_event(db, "scheduler", f"Lỗi scheduler: {exc}", level="warn")


def start_scheduler(app) -> asyncio.Task:
    return asyncio.create_task(scheduler_loop(app), name="timer-scheduler")
