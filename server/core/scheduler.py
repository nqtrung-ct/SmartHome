"""Timer scheduler: checks due timers once per second, announces in the room."""
from __future__ import annotations

import asyncio
import json
import time

from server.db import Database, log_event
from server.util import next_repeat, now, now_iso, parse_iso

_last_ip_heal = 0.0  # lần cuối quét mạng để tự chữa IP (cooldown 5 phút)


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
        except Exception as first_exc:
            # The breaker firmware occasionally drops a single local request.
            # Do not turn a live breaker off in the UI because of one transient
            # read failure; retry once, then retain the last known state.
            await asyncio.sleep(1)
            try:
                state = await asyncio.to_thread(driver.get_state, entity, device)
            except Exception as exc:
                log_event(
                    db, "tuya",
                    f"Không đọc được {row['host']} sau 2 lần thử: {exc}",
                    level="warn",
                )
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
        # tinytuya uses requests without a timeout. Keep a cloud outage from
        # blocking every poll that follows it in the scheduler.
        meters = await asyncio.wait_for(
            asyncio.to_thread(
                driver.cloud_meter, api_key, api_secret, region,
                list(dict.fromkeys(targets.values())),
            ),
            timeout=15,
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


async def _heal_device_ip(app, device_id: int) -> str | None:
    """Poll thiết bị mạng thất bại: có thể router đã cấp lại IP. Quét ping+ARP
    rồi tự chữa host theo MAC (qua _cleanup_network_rows — quy tắc "cùng MAC").
    Trả về host mới nếu đổi, None nếu không/he không được. Cooldown để khỏi
    quét ồ ạt khi thiết bị chỉ đang ngủ hay mất điện."""
    global _last_ip_heal
    db: Database = app.state.db
    if time.time() - _last_ip_heal < 300:
        return None
    _last_ip_heal = time.time()
    from server.core.discovery import _cleanup_network_rows, scan_network
    old = db.one("SELECT host, mac FROM devices WHERE id=?", (device_id,))
    if not old or not old["mac"]:
        return None  # không có MAC thì không khớp được
    await asyncio.to_thread(scan_network, db, app.state.cfg)
    await asyncio.to_thread(_cleanup_network_rows, db)
    new = db.one("SELECT host FROM devices WHERE id=?", (device_id,))
    return new["host"] if new and new["host"] != old["host"] else None


async def _poll_solar(app) -> None:
    """Inverter Senergy (Modbus TCP): PV/pin/tải cho card điện năng màn hình tường.
    Wi-Fi dongle chập chờn - chỉ một lần kết nối mỗi nhịp, chu kỳ mặc định 5 phút.
    Đọc thất bại thì giữ số liệu tốt gần nhất và dò IP mới cho nhịp sau."""
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
        host = row["host"]
        interval = max(180, int(app.state.cfg.get("solar.poll_seconds", 300)))
        last = db.one("SELECT attempted_at FROM device_poll_attempts WHERE device_id=?", (row["did"],))
        if last and time.time() - last["attempted_at"] < interval:
            continue
        db.execute(
            "INSERT INTO device_poll_attempts(device_id, attempted_at) VALUES (?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET attempted_at=excluded.attempted_at",
            (row["did"], time.time()),
        )
        state = None
        try:
            state = await asyncio.to_thread(
                driver.get_state, entity, {"id": row["did"], "host": host})
        except Exception as exc:
            log_event(
                db, "senergy",
                f"Không đọc được inverter {host}: {exc}",
                level="warn",
            )
            # Heal a changed address for the next scheduled poll. Do not
            # reconnect in the same cycle; the dongle is sensitive to bursts.
            from server.drivers.network import tcp_target
            if tcp_target(app.state.cfg, host, 502) == (host, 502):
                await _heal_device_ip(app, row["did"])
        if not state:
            continue  # dongle nghỉ/mất WiFi: giữ số cũ, UI tự hiện "—" khi quá 1 tiếng
        db.execute(
            "UPDATE entities SET state=?, updated_at=? WHERE id=?",
            (json.dumps(state, ensure_ascii=False), now_iso(), row["eid"]),
        )


async def _poll_cameras(app) -> None:
    """Trạng thái camera cho UI: ưu tiên luồng Frigate, rồi thử socket RTSP.

    Trên macOS tiến trình SmartHome có thể bị quyền Local Network chặn socket
    dù Frigate trong OrbStack vẫn nhận hình; FPS của Frigate phản ánh luồng xem.
    """
    import socket
    import httpx
    from server.drivers.base import loads

    db: Database = app.state.db
    rows = db.query(
        """SELECT e.id AS eid, e.driver_data, d.host FROM entities e
           JOIN devices d ON d.id=e.device_id
           WHERE d.driver='onvif' AND e.entity_type='camera'"""
    )
    cameras: dict = {}
    try:
        base = str(app.state.cfg.get("frigate.base_url", "http://127.0.0.1:5000")).rstrip("/")
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{base}/api/stats")
            resp.raise_for_status()
            cameras = resp.json().get("cameras", {})
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    for row in rows:

        def probe(host: str) -> bool:
            for port in (554, 80):
                try:
                    with socket.create_connection((host, port), timeout=2):
                        return True
                except OSError:
                    continue
            return False

        name = loads(row["driver_data"]).get("frigate")
        stats = cameras.get(name) if name else None
        if isinstance(stats, dict):
            try:
                on = float(stats.get("camera_fps") or 0) > 0
            except (TypeError, ValueError):
                on = await asyncio.to_thread(probe, row["host"])
        else:
            on = await asyncio.to_thread(probe, row["host"])
        db.execute(
            "UPDATE entities SET state=?, updated_at=? WHERE id=?",
            ('{"state": "on"}' if on else '{"state": "off"}', now_iso(), row["eid"]),
        )


_ptz_away: dict[int, tuple[float, dict]] = {}  # eid -> (thấy khác góc lúc nào, vị trí lần trước)
_ptz_poll_errors: dict[int, float] = {}


def _ptz_near(a: dict, b: dict) -> bool:
    """Hai tọa độ coi là "cùng một góc"? pan wrap 3600; sai số nhỏ vì motor
    dừng có thể lệch vài đơn vị so với tọa độ đã ra lệnh."""
    dp = abs(int(a["pan"]) - int(b["pan"]))
    dp = min(dp, 3600 - dp)
    return (dp <= 20 and abs(int(a["tilt"]) - int(b["tilt"])) <= 20
            and abs(int(a.get("zoom") or 1) - int(b.get("zoom") or 1)) <= 2)


async def _poll_camera_home(app) -> None:
    """Camera có góc mặc định (⭐ trong admin): rời góc quá lâu thì tự quay về.

    Đọc tọa độ thay vì nhớ lệnh — bắt được cả việc xoay từ app Imou. Đồng hồ
    đếm 60s bắt đầu từ khi camera ĐỨNG YÊN ở góc khác: vị trí còn thay đổi giữa
    hai lần đọc là người đang điều khiển, đếm lại kẻo camera bị kéo về giữa
    chừng (voice quét từng góc tìm người cũng không bị phá)."""
    import server.drivers.dahua_ptz as ptz
    from server.drivers.base import loads

    db: Database = app.state.db
    ret_secs = int(app.state.cfg.get("camera.home_return_seconds", 60))
    if ret_secs <= 0:
        return
    rows = db.query(
        """SELECT e.id AS eid, e.driver_data, d.host, d.mac, d.extra FROM entities e
           JOIN devices d ON d.id = e.device_id
           WHERE e.entity_type='camera' AND d.host != ''"""
    )
    for row in rows:
        dd = loads(row["driver_data"])
        home = next((p for p in dd.get("presets", [])
                     if p.get("name") == dd.get("home_preset")), None)
        if not home:
            _ptz_away.pop(row["eid"], None)
            continue
        device = {"host": row["host"], "mac": row["mac"], "extra": row["extra"]}
        try:
            pos = await asyncio.to_thread(ptz.read_position, app.state.cfg, device)
        except Exception as exc:
            _ptz_away.pop(row["eid"], None)  # camera ngủ/mất mạng: đếm lại khi thức dậy
            vision = getattr(app.state, "vision", None)
            if vision:
                vision.invalidate_camera_position(row["eid"])
            if time.time() - _ptz_poll_errors.get(row["eid"], 0) >= 300:
                _ptz_poll_errors[row["eid"]] = time.time()
                log_event(db, "camera", f"Không đọc được góc camera #{row['eid']}: {exc}", level="warn")
            continue
        _ptz_poll_errors.pop(row["eid"], None)
        # position gate cho trạng thái thị giác (cửa/ti vi): entity chỉ hợp lệ
        # khi camera đứng đúng góc đã vẽ crop
        vision = getattr(app.state, "vision", None)
        if vision:
            vision.set_camera_position(row["eid"], pos)
        if _ptz_near(pos, home):
            _ptz_away.pop(row["eid"], None)
            continue
        prev = _ptz_away.get(row["eid"])
        if prev and _ptz_near(pos, prev[1]):
            if time.time() - prev[0] >= ret_secs:
                await asyncio.to_thread(ptz.fast_goto, app.state.cfg, device,
                                        home["pan"], home["tilt"], home.get("zoom", 1))
                log_event(db, "camera",
                          f"Camera tự quay về góc '{home['name']}' (góc mặc định)")
                _ptz_away.pop(row["eid"], None)
        else:
            _ptz_away[row["eid"]] = (time.time(), pos)


_cozy_warn: dict[str, float] = {}  # lần log warn gần nhất theo host (chống spam)


async def _poll_cozylife(app) -> None:
    """Cập nhật trạng thái công tắc CozyLife (TCP 5555, query chỉ đọc).

    Thiết bị mất nguồn thì không trả lời — công tắc ghi 'off' (không có điện
    thì chẳng thể bật); sensor đo điện giữ số cũ, không ghi đè giá trị rỗng.
    """
    driver = app.state.drivers.get("cozylife")
    if not driver:
        return
    db: Database = app.state.db
    rows = db.query(
        """SELECT e.id AS eid, e.entity_type AS etype, e.driver_data, d.id AS did, d.host, d.extra
           FROM entities e JOIN devices d ON d.id = e.device_id
           WHERE d.driver='cozylife'"""
    )
    for row in rows:
        entity = {"id": row["eid"], "driver_data": row["driver_data"]}
        device = {"id": row["did"], "host": row["host"], "extra": row["extra"]}
        try:
            state = await asyncio.to_thread(driver.get_state, entity, device)
        except Exception as exc:
            # firmware này hay tịt tiếng thoáng qua (chắc chỉ nhận 1 kết nối
            # TCP cùng lúc) — thử lại sau 2 giây, vẫn im lặng mới tính là mất
            # nguồn; ghi 'off' ngay từ lần đầu khiến DB nhấp nháy sai liên tục
            await asyncio.sleep(2)
            try:
                state = await asyncio.to_thread(driver.get_state, entity, device)
            except Exception as retry_exc:
                # A silent/overloaded CozyLife device is not proof that the
                # switch is off. Keep the last known state until a real reply
                # arrives; otherwise the admin UI falsely flips to "off".
                # lỗi đọc từng bị nuốt im lặng — log giới hạn 10 phút/host
                if time.time() - _cozy_warn.get(row["host"], 0) > 600:
                    _cozy_warn[row["host"]] = time.time()
                    log_event(db, "cozylife",
                              f"Không đọc được {row['host']} sau 2 lần thử "
                              f"({type(retry_exc).__name__}: {retry_exc})",
                              level="warn")
                continue
        if not any(v is not None for v in state.values()):
            continue  # có trả lời nhưng rỗng — giữ số cũ
        db.execute(
            "UPDATE entities SET state=?, updated_at=? WHERE id=?",
            (json.dumps(state, ensure_ascii=False), now_iso(), row["eid"]),
        )


async def _poll_androidtv(app) -> None:
    """Giữ trạng thái TV Android trong DB luôn tươi (màn hình đọc DB, không
    tự hỏi driver). Có kết nối thì đọc; mất kết nối thì driver nối lại —
    TV bật lại / vào standby đều tự cập nhật đúng on/off. Side effect có
    ích: phiên đến TV ngủ sâu được duy trì, lệnh 'bật TV' khả thi hơn."""
    driver = app.state.drivers.get("androidtv")
    if not driver:
        return
    for row in app.state.db.query(
        "SELECT id, host FROM devices WHERE driver='androidtv' AND host != ''"
    ):
        try:
            await asyncio.to_thread(driver.refresh, dict(row))
        except Exception as exc:
            log_event(app.state.db, "androidtv",
                      f"Poll TV '{row['host']}' lỗi: {exc}", level="warn")


async def scheduler_loop(app) -> None:
    db: Database = app.state.db
    from server.db import cleanup_old_rows

    last_cleanup_day = None
    last_sensor_poll = 0.0
    last_cloud_poll = 0.0
    last_solar_poll = 0.0
    last_cozylife_poll = 0.0
    last_androidtv_poll = 0.0
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
                    log_event(db, "scheduler", f"{message} (timer #{timer['id']})")
                else:
                    msg = timer["message"] or ""
                    if msg.startswith("raw:"):
                        # "phát câu X vào phòng Y" — đọc nguyên câu, không thêm
                        # "Đã đến giờ hẹn" vào cho tự nhiên
                        message = msg[4:].strip()
                    else:
                        message = f"Đã đến giờ hẹn: {msg}"
                    log_event(db, "scheduler",
                              f"Hẹn giờ đến: {msg} (timer #{timer['id']})")
                connections = getattr(app.state, "voice_connections", None)
                pipeline = getattr(app.state, "pipeline", None)
                if connections and message:
                    await connections.announce_room(timer["room_id"], message, pipeline)
                if message:
                    # thiết bị phòng xiaozhi: nhận qua pending + màn hình poll
                    from server.api.xiaozhi import announce_room as xiaozhi_announce

                    await xiaozhi_announce(app, timer["room_id"], message)
                    # (đã thử phát qua LOA camera Imou qua NetSDK talk — firmware
                    # chặn, xem camera_talk.py — không nối vào đây nữa)
            # battery sensors surface briefly (~seconds every 15 min): poll often
            if time.time() - last_sensor_poll >= int(app.state.cfg.get("tuya.poll_seconds", 60)):
                last_sensor_poll = time.time()
                await _poll_tuya_sensors(app)
            # Poll the inverter before the optional Tuya cloud request. The
            # cloud library historically had no HTTP timeout and could stall
            # this single scheduler task indefinitely.
            if time.time() - last_solar_poll >= int(app.state.cfg.get("solar.poll_seconds", 180)):
                last_solar_poll = time.time()
                await _poll_solar(app)
            if time.time() - last_cloud_poll >= int(app.state.cfg.get("tuya.cloud_seconds", 180)):
                last_cloud_poll = time.time()
                await _poll_tuya_cloud(app)
            if time.time() - last_cozylife_poll >= int(app.state.cfg.get("cozylife.poll_seconds", 60)):
                last_cozylife_poll = time.time()
                await _poll_cozylife(app)
            if time.time() - last_androidtv_poll >= int(app.state.cfg.get("androidtv.poll_seconds", 45)):
                last_androidtv_poll = time.time()
                await _poll_androidtv(app)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log_event(db, "scheduler", f"Lỗi scheduler: {exc}", level="warn")


def start_scheduler(app) -> asyncio.Task:
    return asyncio.create_task(scheduler_loop(app), name="timer-scheduler")


async def camera_monitor_loop(app) -> None:
    """Poll camera independently of slow Tuya/inverter network requests."""
    last_camera_poll = 0.0
    last_ptz_poll = 0.0
    while True:
        try:
            await asyncio.sleep(1)
            if time.monotonic() - last_camera_poll >= int(app.state.cfg.get("camera.poll_seconds", 60)):
                last_camera_poll = time.monotonic()
                await _poll_cameras(app)
            if time.monotonic() - last_ptz_poll >= 10:
                last_ptz_poll = time.monotonic()
                await _poll_camera_home(app)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log_event(app.state.db, "camera", f"Lỗi vòng kiểm tra camera: {exc}", level="warn")


def start_camera_monitor(app) -> asyncio.Task:
    return asyncio.create_task(camera_monitor_loop(app), name="camera-monitor")
