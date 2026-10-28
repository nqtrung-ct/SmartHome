"""Timer scheduler: checks due timers once per second, announces in the room."""
from __future__ import annotations

import asyncio

from server.db import Database, log_event
from server.util import now_iso, parse_iso


async def scheduler_loop(app) -> None:
    db: Database = app.state.db
    from server.db import cleanup_old_rows

    last_cleanup_day = None
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
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log_event(db, "scheduler", f"Lỗi scheduler: {exc}", level="warn")


def start_scheduler(app) -> asyncio.Task:
    return asyncio.create_task(scheduler_loop(app), name="timer-scheduler")
