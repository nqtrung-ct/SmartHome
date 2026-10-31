"""Timer scheduler: checks due timers once per second, announces in the room."""
from __future__ import annotations

import asyncio
import json
import time

from server.db import Database, log_event
from server.util import next_repeat, now, now_iso, parse_iso


async def _run_timer_action(app, action: dict) -> str:
    """Thực thi lệnh thiết bị của hẹn giờ; trả về câu nói báo kết quả."""
    from server.core.text_command import _set_state
    from server.drivers.base import loads

    db: Database = app.state.db
    ent = db.one("SELECT * FROM entities WHERE id=?", (action.get("entity_id"),))
    name = (ent["name"] if ent else action.get("name") or "thiết bị").lower()
    if not ent:
        return f"Hẹn giờ không chạy được: không còn thiết bị {name}."
    if ent["entity_type"] == "controller" or loads(ent["driver_data"]).get("readonly"):
        return f"Hẹn giờ không chạy được: {name} không bật/tắt được."
    try:
        await _set_state(db, app.state.drivers, ent,
                         action.get("command", ""), action.get("params") or {})
    except Exception as exc:
        return f"Hẹn giờ không chạy được: lỗi khi bật/tắt {name} ({exc})."
    if action.get("command") == "set":
        temp = (action.get("params") or {}).get("temperature")
        return f"Đã bật {name} {temp} độ theo hẹn giờ."
    verb = {"on": "bật", "off": "tắt"}.get(action.get("command"), "chỉnh")
    return f"Đã {verb} {name} theo hẹn giờ."


def sync_meter_with_switch(db: Database, switch_id: int, on: bool) -> None:
    """Cầu dao tắt -> Điện năng cùng thiết bị ghi đè ngay 0V/0W, không giữ số cũ
    gây hiểu lầm còn điện. Bật lại thì cloud poll (tối đa 3 phút) sẽ ghi số thật."""
    dev = db.one("SELECT device_id FROM entities WHERE id=?", (switch_id,))
    if not dev:
        return
    for row in db.query(
        "SELECT id, driver_data FROM entities WHERE device_id=? AND entity_type='sensor'",
        (dev["device_id"],),
    ):
        if not json.loads(row["driver_data"] or "{}").get("cloud_meter"):
            continue
        if on:
            break
        db.execute(
            "UPDATE entities SET state=?, updated_at=? WHERE id=?",
            (json.dumps({"voltage": 0, "power": 0, "current": 0}), now_iso(), row["id"]),
        )


async def _poll_tuya_sensors(app) -> None:
    """Refresh tuya sensor/switch states for the dashboard.

    Battery-powered Tuya sensors sleep between reports and refuse connections;
    on failure keep the last known state quietly. Entities flagged cloud_meter
    are skipped here - their state comes from _poll_tuya_cloud.
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
        if json.loads(row["driver_data"] or "{}").get("cloud_meter"):
            continue
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
                sync_meter_with_switch(db, row["eid"], on=False)
            continue
        if not any(v is not None for v in state.values()):
            continue  # device answered but sent nothing - keep last good values
        db.execute(
            "UPDATE entities SET state=?, updated_at=? WHERE id=?",
            (json.dumps(state, ensure_ascii=False), now_iso(), row["eid"]),
        )
        if row["etype"] == "switch":
            sync_meter_with_switch(db, row["eid"], on=state.get("state") == "on")


async def _poll_tuya_cloud(app) -> None:
    """Voltage/power/energy of metering breakers via the Tuya cloud - this
    firmware hides the metering dps over LAN. Credentials (api_key, api_secret,
    region) live in data/secrets.json under group 'tuya_cloud'."""
    from server.util import load_secret

    cfg = app.state.cfg
    api_key = load_secret(cfg, "tuya_cloud", "api_key")
    api_secret = load_secret(cfg, "tuya_cloud", "api_secret")
    if not api_key or not api_secret:
        return
    region = load_secret(cfg, "tuya_cloud", "region") or "sg"
    driver = app.state.drivers.get("tuya")
    db: Database = app.state.db
    if not driver:
        return
    targets: dict[int, str] = {}
    # thiết bị có cầu dao đang tắt: bỏ qua — cloud có thể trả số Vol cũ, ghi đè
    # lên 0V/0W sẽ gây hiểu lầm là còn điện
    switch_off_devs = {
        row["did"] for row in db.query(
            """SELECT d.id AS did, e.state FROM entities e
               JOIN devices d ON d.id = e.device_id
               WHERE d.driver='tuya' AND e.entity_type='switch'"""
        ) if json.loads(row["state"] or "{}").get("state") != "on"
    }
    for row in db.query(
        """SELECT e.id, e.driver_data, d.id AS dev, d.extra FROM entities e
           JOIN devices d ON d.id = e.device_id WHERE d.driver='tuya'"""
    ):
        if not json.loads(row["driver_data"] or "{}").get("cloud_meter"):
            continue
        if row["dev"] in switch_off_devs:
            continue
        dev_id = json.loads(row["extra"] or "{}").get("id")
        if dev_id:
            targets[row["id"]] = dev_id
    if not targets:
        return
    try:
        meters = await asyncio.to_thread(
            driver.cloud_meter, api_key, api_secret, region,
            list(dict.fromkeys(targets.values())),
        )
    except Exception:
        return
    for eid, dev_id in targets.items():
        meter = meters.get(dev_id)
        if meter:
            meter.pop("energy", None)  # tổng kWh đã tiêu thụ: không dùng nữa
            db.execute(
                "UPDATE entities SET state=?, updated_at=? WHERE id=?",
                (json.dumps(meter, ensure_ascii=False), now_iso(), eid),
            )


async def _poll_solar(app) -> None:
    """Inverter Senergy (Modbus TCP): PV/pin/tải cho card điện năng màn hình tường.
    Dongle CPU yếu - chỉ 2 yêu cầu đọc mỗi nhịp, chu kỳ mặc định 3 phút."""
    driver = app.state.drivers.get("senergy")
    if not driver:
        return
    db: Database = app.state.db
    rows = db.query(
        """SELECT e.id AS eid, d.id AS did, d.host FROM entities e
           JOIN devices d ON d.id=e.device_id WHERE d.driver='senergy'"""
    )
    for row in rows:
        entity = {"id": row["eid"]}
        device = {"id": row["did"], "host": row["host"]}
        try:
            state = await asyncio.to_thread(driver.get_state, entity, device)
        except Exception:
            continue  # dongle nghỉ/mất WiFi: giữ số cũ, UI tự hiện "—" khi quá 1 tiếng
        db.execute(
            "UPDATE entities SET state=?, updated_at=? WHERE id=?",
            (json.dumps(state, ensure_ascii=False), now_iso(), row["eid"]),
        )


async def _poll_cameras(app) -> None:
    """Camera ONVIF: online = mở được socket RTSP (:554, dự phòng :80).
    Trạng thái này cho UI (chip 'đang bật/tắt', quyền mở popup video)."""
    import socket

    db: Database = app.state.db
    rows = db.query(
        """SELECT e.id AS eid, d.host FROM entities e
           JOIN devices d ON d.id=e.device_id
           WHERE d.driver='onvif' AND e.entity_type='camera'"""
    )
    for row in rows:

        def probe(host: str) -> bool:
            for port in (554, 80):
                try:
                    with socket.create_connection((host, port), timeout=2):
                        return True
                except OSError:
                    continue
            return False

        on = await asyncio.to_thread(probe, row["host"])
        db.execute(
            "UPDATE entities SET state=?, updated_at=? WHERE id=?",
            ('{"state": "on"}' if on else '{"state": "off"}', now_iso(), row["eid"]),
        )


async def scheduler_loop(app) -> None:
    db: Database = app.state.db
    from server.db import cleanup_old_rows

    last_cleanup_day = None
    last_sensor_poll = 0.0
    last_cloud_poll = 0.0
    last_camera_poll = 0.0
    last_solar_poll = 0.0
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
                action = json.loads(timer["action"]) if timer["action"] else None
                repeat = timer["repeat"] or ""
                # cập nhật TRƯỚC khi chạy để khỏi bắn 2 lần nếu chạy lỗi giữa chừng;
                # hẹn lặp thì nhảy sang lần kế, một lần thì đóng
                if repeat:
                    at = parse_iso(timer["target_at"])
                    nxt = next_repeat(repeat, at.hour, at.minute, after=now())
                    db.execute("UPDATE timers SET target_at=? WHERE id=?",
                               (nxt.isoformat(timespec="seconds"), timer["id"]))
                else:
                    db.execute("UPDATE timers SET done=1 WHERE id=?", (timer["id"],))
                if action:
                    message = await _run_timer_action(app, action)
                    log_event(db, "scheduler",
                              f"Hẹn giờ chạy: {action.get('message', '')} (timer #{timer['id']})")
                else:
                    message = f"Đã đến giờ hẹn: {timer['message']}"
                    log_event(db, "scheduler",
                              f"Hẹn giờ đến: {timer['message']} (timer #{timer['id']})")
                connections = getattr(app.state, "voice_connections", None)
                pipeline = getattr(app.state, "pipeline", None)
                if connections and message:
                    await connections.announce_room(timer["room_id"], message, pipeline)
            # battery sensors surface briefly (~seconds every 15 min): poll often
            if time.time() - last_sensor_poll >= int(app.state.cfg.get("tuya.poll_seconds", 60)):
                last_sensor_poll = time.time()
                await _poll_tuya_sensors(app)
            if time.time() - last_cloud_poll >= int(app.state.cfg.get("tuya.cloud_seconds", 180)):
                last_cloud_poll = time.time()
                await _poll_tuya_cloud(app)
            if time.time() - last_camera_poll >= int(app.state.cfg.get("camera.poll_seconds", 60)):
                last_camera_poll = time.time()
                await _poll_cameras(app)
            if time.time() - last_solar_poll >= int(app.state.cfg.get("solar.poll_seconds", 180)):
                last_solar_poll = time.time()
                await _poll_solar(app)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log_event(db, "scheduler", f"Lỗi scheduler: {exc}", level="warn")


def start_scheduler(app) -> asyncio.Task:
    return asyncio.create_task(scheduler_loop(app), name="timer-scheduler")
