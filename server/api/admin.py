"""Public endpoints: health, pairing, client self-info; plus rooms/floors CRUD."""
from __future__ import annotations

import asyncio
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from server.api.deps import require_admin, voice_identity
from server.db import log_event
from server.util import normalize, now as util_now, now_iso, parse_iso

router = APIRouter()


class CameraRecordingModeIn(BaseModel):
    camera: str
    motion_enabled: bool = False
    person_enabled: bool = False
    retention_days: Literal[5, 10, 15] = 10


async def _frigate_recording_context(request: Request, entity_id: int) -> tuple[dict, dict, str | None, dict]:
    """Return Frigate config, camera entity data and its mapped Frigate camera name."""
    import json as _json
    import httpx

    row = request.app.state.db.one(
        """SELECT e.driver_data, d.host FROM entities e
           JOIN devices d ON d.id=e.device_id
           WHERE e.id=? AND e.entity_type='camera'""",
        (entity_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy camera")
    try:
        driver_data = _json.loads(row["driver_data"] or "{}")
    except ValueError:
        driver_data = {}

    base = str(request.app.state.cfg.get("frigate.base_url", "http://127.0.0.1:5000")).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{base}/api/config")
            response.raise_for_status()
            config = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"Không kết nối được Frigate: {exc}") from exc

    cameras = config.get("cameras", {})
    camera_name = driver_data.get("frigate")
    if camera_name not in cameras:
        camera_name = None
        host = str(row["host"] or "")
        if host:
            for name, camera in cameras.items():
                inputs = (camera.get("ffmpeg") or {}).get("inputs") or []
                if any(host in str(source.get("path", "")) for source in inputs):
                    camera_name = name
                    break
        if camera_name is None and len(cameras) == 1:
            camera_name = next(iter(cameras))
    return config, cameras, camera_name, driver_data


@router.get("/entities/{entity_id}/recording")
async def get_camera_recording_mode(request: Request, entity_id: int, _: None = Depends(require_admin)):
    config, cameras, camera_name, driver_data = await _frigate_recording_context(request, entity_id)
    modes = []
    camera_configs = config.get("cameras", {})
    for name in cameras:
        camera_config = camera_configs.get(name) or {}
        record = camera_config.get("record") or config.get("record") or {}
        configured_motion_days = float((record.get("motion") or {}).get("days") or 0)
        configured_person_days = float(((record.get("alerts") or {}).get("retain") or {}).get("days") or 0)
        tracked = (camera_config.get("objects") or config.get("objects") or {}).get("track") or []
        preferred_days = driver_data.get("recording_retention_days")
        if preferred_days not in (5, 10, 15):
            active_days = [days for days, enabled in (
                (configured_motion_days, configured_motion_days > 0),
                (configured_person_days, configured_person_days > 0 and "person" in tracked),
            ) if enabled]
            preferred_days = active_days[0] if active_days and all(
                days == active_days[0] for days in active_days
            ) and active_days[0] in (5, 10, 15) else 10
        modes.append({
            "name": name,
            "motion_enabled": configured_motion_days > 0,
            "person_enabled": configured_person_days > 0 and "person" in tracked,
            "retention_days": preferred_days,
            "person_tracked": "person" in tracked,
        })
    return {"camera": camera_name, "cameras": modes}


@router.put("/entities/{entity_id}/recording")
async def set_camera_recording_mode(
    request: Request, entity_id: int, body: CameraRecordingModeIn,
    _: None = Depends(require_admin),
):
    import json as _json
    import httpx

    if not body.motion_enabled and not body.person_enabled:
        raise HTTPException(status_code=400, detail="Bật ít nhất một lựa chọn ghi hình")
    config, cameras, _, driver_data = await _frigate_recording_context(request, entity_id)
    if body.camera not in cameras:
        raise HTTPException(status_code=400, detail="Camera Frigate không tồn tại")

    record = ((config.get("cameras", {}).get(body.camera) or {}).get("record")
              or config.get("record") or {})
    if not record.get("enabled", True):
        raise HTTPException(status_code=400, detail="Ghi hình đang tắt trong cấu hình Frigate")
    camera_config = config.get("cameras", {}).get(body.camera) or {}
    tracked = (camera_config.get("objects") or config.get("objects") or {}).get("track") or []
    if body.person_enabled and "person" not in tracked:
        raise HTTPException(status_code=400, detail="Hãy bật theo dõi person cho camera này trong Frigate trước")
    camera_record = {
        "enabled": True,
        "motion": {"days": body.retention_days if body.motion_enabled else 0},
        "alerts": {"retain": {
            "days": body.retention_days if body.person_enabled else 0,
            **({"mode": "motion"} if body.person_enabled else {}),
        }},
        "detections": {"retain": {"days": 0}},
    }

    base = str(request.app.state.cfg.get("frigate.base_url", "http://127.0.0.1:5000")).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.put(
                f"{base}/api/config/set",
                json={"requires_restart": 1, "config_data": {
                    "cameras": {body.camera: {"record": camera_record}},
                }},
            )
            response.raise_for_status()
            result = response.json()
            if not result.get("success", True):
                raise HTTPException(status_code=400, detail=result.get("message", "Frigate không lưu được cấu hình"))
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"Không thể cập nhật cấu hình Frigate: {exc}") from exc

    driver_data["frigate"] = body.camera
    driver_data["recording_retention_days"] = body.retention_days
    request.app.state.db.execute(
        "UPDATE entities SET driver_data=? WHERE id=?",
        (_json.dumps(driver_data, ensure_ascii=False), entity_id),
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            restart = await client.post(f"{base}/api/restart")
            restart.raise_for_status()
        restarting = True
    except httpx.HTTPError:
        restarting = False
    return {
        "ok": True, "motion_enabled": body.motion_enabled,
        "person_enabled": body.person_enabled, "retention_days": body.retention_days,
        "camera": body.camera,
        "restarting": restarting, "restart_required": not restarting,
    }


def solar_snapshot(db) -> dict | None:
    """Số liệu inverter Senergy (PV / pin / tải) — dùng chung cho /api/display
    (màn hình treo tường) và /api/overview (dashboard admin)."""
    import json as _json

    row = db.one(
        """SELECT e.state, e.updated_at FROM entities e
           JOIN devices d ON d.id=e.device_id
           WHERE d.driver='senergy' AND e.entity_type='sensor' ORDER BY e.id LIMIT 1"""
    )
    if not row:
        return None
    try:
        st = _json.loads(row["state"] or "{}")
    except ValueError:
        st = {}
    if st.get("pv_power") is None:
        return None
    return {
        "pv_power": st.get("pv_power"),
        "load_power": st.get("load_power"),
        "battery_soc": st.get("battery_soc"),
        "battery_voltage": st.get("battery_voltage"),
        "battery_power": st.get("battery_power"),
        "updated_at": row["updated_at"],
    }


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/display")
async def display(request: Request, response: Response):
    """Snapshot cho màn hình treo tường. Public (server LAN-only, không có secret).
    Trả các phòng trong nhà có thiết bị hoặc cảm biến nhiệt/ẩm. Công tắc
    hiển thị chỉ đọc; số điện lưới vẫn có ở widget `grid` riêng."""
    import json as _json

    app = request.app
    db = app.state.db
    display_token = request.headers.get("X-Display-Token", "")
    role = _display_role(app, display_token)
    if display_token and not role:
        raise HTTPException(status_code=401, detail="Phiên xem đã hết hạn")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"

    weather = None
    try:
        weather = app.state.weather.snapshot()
        weather["forecast"] = app.state.weather.forecast(2)
    except Exception:
        weather = None

    rooms = []
    for room in db.query("SELECT * FROM rooms ORDER BY name"):
        if not role or (role == "floor1" and normalize(room["floor"] or "") != "tang 1"):
            continue
        if normalize(room["floor"] or "") == "ngoai nha":
            continue
        entities = db.query(
            """SELECT e.id, e.name, e.entity_type, e.state, e.driver_data, d.driver, d.extra
               FROM entities e JOIN devices d ON d.id=e.device_id
               WHERE COALESCE(e.room_id, d.room_id)=?
                 AND e.entity_type NOT IN ('controller','sensor')""",
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
                # màn hình treo tường chỉ điều khiển thiết bị IR (remote) và TV
                # Android; Tuya/zigbee hiện trạng thái thôi, bấm không có gì xảy ra
                "controllable": (e["driver"] == "broadlink"
                                 and e["entity_type"] in ("light", "climate", "fan", "media")
                                 and "readonly" not in dd)
                                or (e["driver"] == "androidtv" and e["entity_type"] == "media"),
            }
            if e["driver"] == "broadlink" and e["entity_type"] != "climate":
                # on/off/power = Bật/Tắt lớn, không lặp lại trong nút phụ
                ent["buttons"] = sorted(
                    k for k in dd.get("codes", {})
                    if k not in ("on", "off", "toggle", "power"))
            if e["driver"] == "androidtv":
                ent["buttons"] = ["volume_up", "volume_down", "mute", "home", "back", "pause"]
            if e["entity_type"] == "camera":
                # góc quay đã lưu (NetSDK PTZ) + tên stream go2rtc (live A/V WebRTC)
                ent["presets"] = dd.get("presets", [])
                ent["ptz"] = True
                try:
                    dextra = _json.loads(e["extra"] or "{}")
                except ValueError:
                    dextra = {}
                ent["stream"] = dextra.get("go2rtc_stream", "")
            ents.append(ent)
        rooms.append({
            "id": room["id"], "name": room["name"], "floor": room["floor"],
            "temperature": temp, "humidity": humidity, "entities": ents,
        })

    timers = [
        {"target_at": t["target_at"], "message": t["message"], "room": t["room_name"],
         "repeat": t["repeat"], "has_action": bool(t["action"])}
        for t in db.query(
            """SELECT t.target_at, t.message, t.repeat, t.action, r.name AS room_name
               FROM timers t
               LEFT JOIN rooms r ON r.id=t.room_id
               WHERE t.done=0 AND (?='admin' OR r.floor=?) ORDER BY t.target_at LIMIT 5""",
            (role or "", "Tầng 1"),
        )
    ] if role else []

    # điện lưới: cầu dao Tuya (đầu vào lưới) + số Volt từ cảm biến cloud_meter.
    # Tắt cầu dao = mất điện lưới (nhà có điện mặt trời nên server vẫn chạy).
    # Khi cầu dao tắt, meter không đo được và DB giữ số Vol cũ -> số Vol chỉ
    # có nghĩa khi cầu dao đang bật; lúc đó Vol < 200 = sụt áp (cảnh báo vàng).
    grid = None
    sw = db.one(
        """SELECT e.id AS eid, e.state, e.updated_at, d.id AS did, d.host FROM entities e JOIN devices d ON d.id=e.device_id
           WHERE d.driver='tuya' AND e.entity_type='switch' ORDER BY e.id LIMIT 1"""
    )
    if sw:
        try:
            sw_state = _json.loads(sw["state"] or "{}")
        except ValueError:
            sw_state = {}

        # Read the scheduler's confirmed snapshot. Polling here opens competing
        # Tuya sessions for every display client and can disrupt device replies.
        try:
            age = (util_now() - parse_iso(sw["updated_at"])).total_seconds()
            fresh = 0 <= age <= max(180, int(app.state.cfg.get("tuya.poll_seconds", 60)) * 3)
        except (TypeError, ValueError):
            fresh = False
        voltage = None
        for m in db.query(
            """SELECT e.state, e.driver_data, e.updated_at FROM entities e
               JOIN devices d ON d.id=e.device_id
               WHERE d.id=? AND e.entity_type='sensor' ORDER BY e.id""",
            (sw["did"],),
        ):
            if not _json.loads(m["driver_data"] or "{}").get("cloud_meter"):
                continue
            try:
                mv = _json.loads(m["state"] or "{}").get("voltage")
                meter_age = (util_now() - parse_iso(m["updated_at"])).total_seconds()
                if not 0 <= meter_age <= 600:
                    mv = None
            except (ValueError, TypeError):
                mv = None
            if mv is not None:
                voltage = mv
                break
        state = sw_state.get("state")
        on = (state == "on") if fresh and state in ("on", "off") else None
        # Vol = 0 khi công tắc bật: khả năng cao vừa có điện lại, số cloud chưa
        # kịp về — không cảnh báo vàng; chỉ cảnh báo khi thật sự sụt áp (0 < Vol < 200)
        grid = {
            "on": on,
            "voltage": voltage,
            "low_voltage": on and voltage is not None and 0 < voltage < 200,
            "updated_at": sw["updated_at"],
        }

    # card điện năng: inverter Senergy (PV / pin / tải) - sensor entity của driver senergy
    solar = solar_snapshot(db)

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
    if role != "admin":
        counts = {"rooms": len(rooms)}
    return {
        "weather": weather,
        "rooms": rooms,
        "timers": timers,
        "counts": counts,
        "grid": grid,
        "solar": solar,
        "updated_at": now_iso(),
    }


@router.get("/display/camera/{entity_id}/stream")
async def display_camera_stream(request: Request, entity_id: int):
    """Video camera cho màn hình treo tường: RTSP -> MJPEG qua ffmpeg.
    Trình duyệt không mở được RTSP nên server chuyển mã và đẩy từng khung
    JPEG (multipart/x-mixed-replace) — thẻ <img> hiển thị như video.
    Tài khoản RTSP lấy từ secret 'rtsp.<host>' (JSON {"user","pass"} hoặc
    chuỗi mật khẩu, user mặc định admin); đè được bằng device.extra
    rtsp_user/rtsp_port/rtsp_path cho camera khác hãng."""
    import contextlib
    import json as _json
    import shutil as _shutil
    from urllib.parse import quote

    from fastapi.responses import StreamingResponse

    from server.util import load_secret, norm_mac

    _require_display_entity(request.app, request.query_params.get("token", ""), entity_id)
    db = request.app.state.db
    row = db.one(
        """SELECT e.state, d.host, d.extra, d.mac FROM entities e
           JOIN devices d ON d.id=e.device_id
           WHERE e.id=? AND e.entity_type='camera'""",
        (entity_id,),
    )
    if not row or not row["host"]:
        raise HTTPException(status_code=404, detail="Không có camera này")
    try:
        st = _json.loads(row["state"] or "{}")
    except ValueError:
        st = {}
    try:
        extra = _json.loads(row["extra"] or "{}")
    except ValueError:
        extra = {}
    ffmpeg = _shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(status_code=503, detail="Server chưa cài ffmpeg")
    if st.get("state") != "on":
        # state do scheduler cập nhật mỗi 60s — camera vừa lên là DB còn "off".
        # Thử socket RTSP thật: mở được thì phát tiếp và tự correct state,
        # khỏi bắt người dùng chờ phút poll kế tiếp
        import socket as _socket

        def _alive() -> bool:
            try:
                with _socket.create_connection(
                        (row["host"], int(extra.get("rtsp_port") or 554)), timeout=2):
                    return True
            except OSError:
                return False

        if not await asyncio.to_thread(_alive):
            raise HTTPException(status_code=503, detail="Camera đang tắt hoặc mất kết nối")
        from server.util import now_iso

        db.execute("UPDATE entities SET state=?, updated_at=? WHERE id=?",
                   ('{"state": "on"}', now_iso(), entity_id))
    cred = {}
    raw = ""
    # IP đổi (tự chữa qua MAC lúc quét) thì secret theo host cũ trượt:
    # thử theo host -> theo MAC -> nếu nhóm rtsp chỉ có 1 entry thì dùng nó
    for key in (row["host"], norm_mac(row["mac"] or "")):
        if not key:
            continue
        try:
            raw = load_secret(request.app.state.cfg, "rtsp", key) or ""
        except Exception:
            raw = ""
        if raw:
            break
    if not raw:
        try:
            group = _json.loads(
                request.app.state.cfg.data_path("secrets.json").read_text(encoding="utf-8")
            ).get("rtsp", {})
            # bỏ key của IP test (192.0.2.x smoke test) khi đếm entry duy nhất
            real = [v for k, v in group.items() if not k.startswith("192.0.2.")]
            if len(real) == 1:
                raw = real[0]
        except Exception:
            raw = ""
    if raw:
        try:
            cred = _json.loads(raw)
        except ValueError:
            cred = {"pass": raw}
    user = quote(str(extra.get("rtsp_user") or cred.get("user") or "admin"))
    password = quote(str(cred.get("pass") or ""))
    if not password:
        raise HTTPException(status_code=503, detail="Camera chưa có mật khẩu RTSP")
    port = extra.get("rtsp_port") or 554
    # sub stream 640x352: fallback này chiếu trong popup nhỏ — sub nhẹ và có
    # hình nhanh hơn nhiều so với main 2K
    path = extra.get("rtsp_path") or "/cam/realmonitor?channel=1&subtype=1"
    url = f"rtsp://{user}:{password}@{row['host']}:{port}{path}"
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-loglevel", "error", "-rtsp_transport", "tcp",
        "-i", url, "-an", "-f", "mjpeg", "-q:v", "5", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )

    async def frames():
        buf = b""
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break  # camera ngắt luồng (rớt mạng / hết dữ liệu)
                buf += chunk
                while True:  # tách khung JPEG hoàn chỉnh (SOI..EOI)
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        buf = b""
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        buf = buf[start:]
                        break
                    frame, buf = buf[start:end + 2], buf[end + 2:]
                    if len(frame) > 512:  # bỏ mảnh rác nhỏ
                        yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                               + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
        finally:
            # client đóng popup -> generator bị hủy -> giết ffmpeg, không treo tiến trình
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")


# --- wall display: PIN unlock + control ---------------------------------------
# Màn hình treo tường không có token admin; nhập đúng mã PIN 4 số (đặt trong
# tab Thiết đặt) sẽ nhận token cho đến khi thoát hoặc server khởi động lại.


def _display_tokens(app) -> dict:
    if not hasattr(app.state, "display_tokens"):
        app.state.display_tokens = {}
    return app.state.display_tokens


class DisplayUnlockIn(BaseModel):
    pin: str
    role: str = "admin"


def _display_role(app, token):
    session = _display_tokens(app).get(str(token or ""))
    if not session:
        return None
    # Accept existing in-memory tokens until the server is restarted.
    if isinstance(session, dict):
        expiry = session.get("expires")
        return session["role"] if expiry is None or expiry > util_now() else None
    return "admin" if session > util_now() else None


def _require_display_entity(app, token, entity_id):
    role = _display_role(app, token)
    if not role:
        raise HTTPException(status_code=401, detail="Phiên xem đã hết hạn")
    row = app.state.db.one("""SELECT r.floor FROM entities e JOIN devices d ON d.id=e.device_id
        LEFT JOIN rooms r ON r.id=COALESCE(e.room_id, d.room_id) WHERE e.id=?""", (entity_id,))
    if not row or (role == "floor1" and normalize(row["floor"] or "") != "tang 1"):
        raise HTTPException(status_code=403, detail="Không có quyền truy cập phòng này")


@router.post("/display/unlock")
async def display_unlock(request: Request, body: DisplayUnlockIn):
    pin = str(body.pin or "").strip()
    if body.role not in ("floor1", "admin"):
        raise HTTPException(status_code=400, detail="Quyền không hợp lệ")
    key = "display.pin_floor1" if body.role == "floor1" else "display.pin_admin"
    expected = str(request.app.state.cfg.get(key, "") or (request.app.state.cfg.get("display.pin", "") if body.role == "admin" else "") or "").strip()
    if not expected:
        raise HTTPException(status_code=400, detail="Chưa bật điều khiển trên màn hình")
    if pin != expected:
        await asyncio.sleep(1)  # làm chậm dò mã
        raise HTTPException(status_code=401, detail="Mã PIN không đúng")
    token = secrets.token_hex(16)
    tokens = _display_tokens(request.app)
    for k in [k for k in tokens if not _display_role(request.app, k)]:
        del tokens[k]  # dọn token hết hạn luôn
    tokens[token] = {"role": body.role}
    log_event(request.app.state.db, "display", "Màn hình mở khóa điều khiển")
    return {"token": token}


@router.post("/display/logout")
async def display_logout(request: Request):
    token = request.headers.get("X-Display-Token", "")
    _display_tokens(request.app).pop(token, None)
    return {"status": "ok"}


class DisplayCommandIn(BaseModel):
    token: str
    entity: int
    command: str  # on | off | set
    params: dict = {}


@router.post("/display/command")
async def display_command(request: Request, body: DisplayCommandIn):
    _require_display_entity(request.app, body.token, body.entity)
    entity = request.app.state.db.one(
        """SELECT e.entity_type, d.driver FROM entities e
           JOIN devices d ON d.id=e.device_id WHERE e.id=?""", (body.entity,))
    if entity and entity["entity_type"] == "switch":
        raise HTTPException(status_code=403, detail="Công tắc chỉ xem trạng thái")
    from server.api.devices import run_entity_command
    state = await run_entity_command(request.app, body.entity, body.command, body.params)
    return {"state": state}


# --- wall display: PTZ camera (NetSDK 37777) ---------------------------------


class PtzIn(BaseModel):
    action: str            # goto | save | del | move | stop
    index: int = 0         # số preset (goto/save/del)
    direction: str = ""    # up/down/left/right (move/stop)
    name: str = ""         # tên preset khi lưu


async def run_camera_ptz(app, entity_id: int, body: PtzIn) -> dict:
    """Thực thi lệnh PTZ lên camera Imou/Dahua — dùng chung display + admin."""
    import json as _json

    import server.drivers.dahua_ptz as ptz

    db = app.state.db
    row = db.one(
        """SELECT e.driver_data, d.host, d.mac, d.extra FROM entities e
           JOIN devices d ON d.id=e.device_id
           WHERE e.id=? AND e.entity_type='camera'""",
        (entity_id,),
    )
    if not row or not row["host"]:
        raise HTTPException(status_code=404, detail="Không có camera này")
    device = {"host": row["host"], "mac": row["mac"], "extra": row["extra"]}
    cfg = app.state.cfg
    try:
        if body.action == "goto":
            try:
                dd = _json.loads(row["driver_data"] or "{}")
            except ValueError:
                dd = {}
            presets = dd.get("presets", [])
            if not (0 <= body.index < len(presets)):
                raise HTTPException(status_code=404, detail="Không có góc này")
            p = presets[body.index]
            await asyncio.to_thread(ptz.fast_goto, cfg, device,
                                    p["pan"], p["tilt"], p.get("zoom", 1))
        elif body.action == "save":
            # preset = tọa độ hiện tại của camera (Imou bỏ qua preset slot trên máy)
            pos = await asyncio.to_thread(ptz.read_position, cfg, device)
            try:
                dd = _json.loads(row["driver_data"] or "{}")
            except ValueError:
                dd = {}
            presets = dd.setdefault("presets", [])
            presets.append({"name": body.name.strip() or f"Góc {len(presets) + 1}", **pos})
            db.execute("UPDATE entities SET driver_data=? WHERE id=?",
                       (_json.dumps(dd, ensure_ascii=False), entity_id))
            return {"ok": True, "presets": presets}
        elif body.action == "move":
            await asyncio.to_thread(ptz.move, cfg, device, body.direction)
        elif body.action == "stop":
            await asyncio.to_thread(
                ptz.move, cfg, device, body.direction or "right", stop=True)
        elif body.action == "del":
            try:
                dd = _json.loads(row["driver_data"] or "{}")
            except ValueError:
                dd = {}
            presets = dd.get("presets", [])
            if 0 <= body.index < len(presets):
                removed = presets.pop(body.index)
                if dd.get("home_preset") == removed["name"]:
                    dd.pop("home_preset", None)  # góc mặc định đã xóa thì bỏ luôn
            dd["presets"] = presets
            db.execute("UPDATE entities SET driver_data=? WHERE id=?",
                       (_json.dumps(dd, ensure_ascii=False), entity_id))
            return {"ok": True, "presets": presets, "home": dd.get("home_preset", "")}
        elif body.action == "home":
            # góc mặc định: scheduler thấy camera rời góc này quá lâu sẽ tự quay về.
            # Lưu theo TÊN (index đổi khi xóa góc); bấm lại góc đang mặc định = bỏ
            try:
                dd = _json.loads(row["driver_data"] or "{}")
            except ValueError:
                dd = {}
            presets = dd.get("presets", [])
            if not (0 <= body.index < len(presets)):
                raise HTTPException(status_code=404, detail="Không có góc này")
            name = presets[body.index]["name"]
            if dd.get("home_preset") == name:
                dd.pop("home_preset", None)
            else:
                dd["home_preset"] = name
            db.execute("UPDATE entities SET driver_data=? WHERE id=?",
                       (_json.dumps(dd, ensure_ascii=False), entity_id))
            return {"ok": True, "presets": presets, "home": dd.get("home_preset", "")}
        else:
            raise HTTPException(status_code=400, detail="action không hợp lệ")
    except ptz.PtzError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


class DisplayPtzIn(PtzIn):
    token: str
    entity: int


@router.post("/display/ptz")
async def display_ptz(request: Request, body: DisplayPtzIn):
    _require_display_entity(request.app, body.token, body.entity)
    if body.action != "goto":
        # màn hình treo tường chỉ được gọi góc đã lưu — xoay/lưu/xóa làm ở admin
        raise HTTPException(status_code=400, detail="Màn hình chỉ được quay về góc đã lưu")
    return await run_camera_ptz(request.app, body.entity, body)


@router.post("/entities/{entity_id}/ptz")
async def entity_ptz(request: Request, entity_id: int, body: PtzIn, _: None = Depends(require_admin)):
    return await run_camera_ptz(request.app, entity_id, body)


@router.post("/pair", status_code=201)
def pair(request: Request, body: dict):
    """Pair an Android phone with a one-time code shown in the admin UI."""
    db = request.app.state.db
    code = str(body.get("code", "")).strip()
    name = str(body.get("client_name", "")).strip() or "Android"
    row = db.one("SELECT * FROM pairing_codes WHERE code=? AND used=0", (code,))
    if not row or datetime.fromisoformat(row["expires_at"]) < util_now():
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
    expires = (util_now() + timedelta(minutes=15)).isoformat(timespec="seconds")
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
            "pin_floor1_set": bool(cfg.get("display.pin_floor1", "")),
            "pin_admin_set": bool(cfg.get("display.pin_admin", "") or cfg.get("display.pin", "")),
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
    display_pin_floor1: str | None = None
    display_pin_admin: str | None = None


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

    for role in ("floor1", "admin"):
        pin = getattr(body, f"display_pin_{role}")
        if pin is not None:
            pin = pin.strip()
            if pin and (len(pin) != 4 or not pin.isascii() or not pin.isdigit()):
                raise HTTPException(status_code=400, detail="Mã PIN phải gồm 4 chữ số")
            apply("display", f"pin_{role}", pin)
            if role == "admin":
                apply("display", "pin", "")
            _display_tokens(app).clear()

    # live effects
    zigbee = app.state.drivers.get("zigbee")
    if zigbee is not None and body.mqtt_enabled is not None:
        if body.mqtt_enabled:
            zigbee.start()
        else:
            zigbee.stop()

    log_event(app.state.db, "settings", "Đã lưu cài đặt: " + ("; ".join(changed) or "không đổi"))
    return {"ok": True, "changed": changed, "admin_token_changed": token_changed}
