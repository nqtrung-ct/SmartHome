"""Trạng thái thị giác từ Frigate custom classification qua MQTT.

Frigate train model phân loại trạng thái (Cửa chính mở/đóng, Ti vi bật/tắt)
trên crop của detect stream; kết quả ĐÃ XÁC NHÂN (3 lần liên tiếp ≥ threshold)
publish MQTT `frigate/<camera>/classification/<model>` — payload là chuỗi nhãn,
chỉ gửi khi state ĐỔI (cảnh tĩnh không có message, state cũ vẫn đúng).

Camera là PTZ nên crop chỉ đúng khi camera đứng đúng góc đã vẽ crop:
mỗi trạng thái gắn với một preset (position gate) — camera lệch góc thì
entity = "unknown" và bỏ qua kết quả classification (model vẫn chạy và
confirm bừa ở góc khác — đã thấy bằng thử nghiệm).

Cấu hình nằm trong driver_data của entity camera:
  "vision": [{"model": "Cửa chính", "preset": "Cửa chính", "name": "Cửa chính"},
             {"model": "Ti vi",     "preset": "TV",        "name": "TV"}]
Mỗi mục tự sinh một entity cảm biến readonly treo vào device của camera.
"""
from __future__ import annotations

import json
import threading
import time

from server.db import Database, log_event
from server.drivers.base import loads
from server.util import now_iso

# (cam_entity_id, preset_name) -> entity_ids đang hợp lệ ở góc đó
# vị trí camera cập nhật mỗi 10s từ scheduler._poll_camera_home


def ptz_near(a: dict, b: dict, tol: int = 25) -> bool:
    """Hai tọa độ coi là cùng góc (pan wrap 3600; motor dừng lệch vài đơn vị)."""
    dp = abs(int(a["pan"]) - int(b["pan"]))
    dp = min(dp, 3600 - dp)
    return (dp <= tol and abs(int(a["tilt"]) - int(b["tilt"])) <= tol
            and abs(int(a.get("zoom") or 1) - int(b.get("zoom") or 1)) <= 2)


class VisionStates:
    """MQTT subscriber Frigate classification + position gate + cache state."""

    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db
        self.client = None
        self._stop = threading.Event()
        self._running = False
        self._lock = threading.Lock()
        # entity_id -> {"label": str|None, "ts": float, "valid": bool}
        self._cache: dict[int, dict] = {}
        self._maps: dict[int, dict] = {}  # camera_entity_id -> {preset_name: coords}
        self._waiters: list[tuple[int, float, threading.Event]] = []

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if not self.cfg.get("mqtt.enabled", False) or self._running:
            return
        self._running = True
        self._stop = threading.Event()
        self.bootstrap()
        threading.Thread(target=self._run, name="vision-mqtt", daemon=True).start()

    def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass

    def _run(self) -> None:
        import paho.mqtt.client as mqtt

        delay = 5
        warned = False
        while not self._stop.is_set():
            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="smarthome-vision")
                client.on_connect = self._on_connect
                client.on_message = self._on_message
                client.reconnect_delay_set(min_delay=1, max_delay=30)
                client.connect(
                    self.cfg.get("mqtt.host", "127.0.0.1"),
                    int(self.cfg.get("mqtt.port", 1883)),
                    keepalive=60,
                )
                if warned:
                    log_event(self.db, "vision", "MQTT đã kết nối trở lại")
                warned = False
                delay = 5
                self.client = client
                client.loop_forever(retry_first_connection=False)
            except Exception as exc:
                if not warned:
                    log_event(self.db, "vision", f"MQTT không kết nối được: {exc}", level="warn")
                    warned = True
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, 300)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            # model name chứa dấu + khoảng trắng — '+' khớp nguyên một level
            client.subscribe("frigate/+/classification/+")
            log_event(self.db, "vision", "Đã kết nối MQTT (Frigate classification)")
        else:
            log_event(self.db, "vision", f"MQTT từ chối kết nối: {reason_code}", level="warn")

    # -- entities -----------------------------------------------------------

    def bootstrap(self) -> None:
        """Sinh entity cảm biến readonly cho mỗi mục vision của mỗi camera."""
        cams = self.db.query(
            """SELECT e.id AS eid, e.device_id, e.room_id, e.driver_data FROM entities e
               WHERE e.entity_type='camera'"""
        )
        for cam in cams:
            dd = loads(cam["driver_data"])
            targets = dd.get("vision") or []
            self._maps[cam["eid"]] = {
                p["name"]: p for p in dd.get("presets", []) if p.get("name")
            }
            frigate_cam = dd.get("frigate", "")
            for t in targets:
                # phải chặn entity_type='sensor': driver_data của CHÍNH camera
                # cũng chứa chuỗi "model": ... trong mục cấu hình vision
                row = self.db.one(
                    "SELECT id FROM entities WHERE entity_type='sensor' "
                    "AND device_id=? AND driver_data LIKE ?",
                    (cam["device_id"], f'%"model": "{t["model"]}"%'),
                )
                if row:
                    continue
                self.db.execute(
                    """INSERT INTO entities(device_id, name, entity_type, aliases, room_id,
                                            driver_data, state, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        cam["device_id"], t["name"], "sensor", t.get("aliases", ""),
                        cam["room_id"],
                        json.dumps(
                            {"readonly": True, "vision": True, "model": t["model"],
                             "preset": t.get("preset", ""), "camera_entity": cam["eid"],
                             "frigate_cam": frigate_cam},
                            ensure_ascii=False,
                        ),
                        "{}", now_iso(),
                    ),
                )
                log_event(self.db, "vision", f"Đã tạo entity thị giác '{t['name']}'")
        # Chưa đọc lại được góc PTZ sau restart thì không giữ trạng thái cũ.
        for row in self._vision_entities():
            if loads(row["driver_data"]).get("preset"):
                self._cache[row["id"]] = {"label": None, "ts": 0.0, "valid": False}
                if (loads(row["state"]) or {}).get("state") != "unknown":
                    self._write(row["id"], "unknown")
                continue
            # Frigate chỉ publish khi state đổi.
            label = (loads(row["state"]) or {}).get("state")
            if label and label != "unknown":
                self._cache[row["id"]] = {"label": label, "ts": 0.0, "valid": False}

    def _vision_entities(self) -> list:
        return self.db.query(
            "SELECT * FROM entities WHERE entity_type='sensor' AND driver_data LIKE '%\"vision\": true%'"
        )

    # -- MQTT messages ------------------------------------------------------

    def _on_message(self, client, userdata, msg):
        try:
            self._handle(msg.topic, msg.payload.decode("utf-8", "replace").strip())
        except Exception as exc:
            log_event(self.db, "vision", f"Lỗi xử lý MQTT: {exc}", level="warn")

    def _handle(self, topic: str, label: str) -> None:
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != "frigate" or parts[2] != "classification":
            return
        frigate_cam, model = parts[1], parts[3]
        if not label:
            return
        row = self.db.one(
            "SELECT * FROM entities WHERE entity_type='sensor' AND driver_data LIKE ?",
            (f'%"model": "{model}"%',),
        )
        if not row:
            return  # model chưa được cấu hình vision — bỏ qua
        bound = loads(row["driver_data"]).get("frigate_cam", "")
        if bound and bound != frigate_cam:
            return  # model trùng tên nhưng thuộc camera khác
        with self._lock:
            ent = self._cache.setdefault(row["id"], {"label": None, "ts": 0.0, "valid": False})
            if not ent["valid"]:
                return  # crop ngoài góc preset: không ghi đè label đúng bằng kết quả sai
            ent["label"], ent["ts"] = label, time.time()
        self._write(row["id"], label)
        self._wake(row["id"])

    def _wake(self, entity_id: int) -> None:
        with self._lock:
            for w in self._waiters:
                if w[0] == entity_id:
                    w[2].set()

    # -- position gate (gọi từ scheduler mỗi 10s) ---------------------------

    def invalidate_camera_position(self, cam_entity_id: int) -> None:
        """Mất vị trí PTZ: không thể khẳng định trạng thái ở crop nào."""
        rows = [row for row in self._vision_entities()
                if loads(row["driver_data"]).get("camera_entity") == cam_entity_id]
        with self._lock:
            for row in rows:
                self._cache[row["id"]] = {"label": None, "ts": 0.0, "valid": False}
        for row in rows:
            if (loads(row["state"]) or {}).get("state") != "unknown":
                self._write(row["id"], "unknown")

    def set_camera_position(self, cam_entity_id: int, pos: dict) -> None:
        """Đánh dấu entity nào đang hợp lệ theo góc camera hiện tại."""
        presets = self._maps.get(cam_entity_id)
        if presets is None:
            self._reload_maps(cam_entity_id)
            presets = self._maps.get(cam_entity_id, {})
        wanted: dict[int, bool] = {}
        for row in self._vision_entities():
            dd = loads(row["driver_data"])
            if dd.get("camera_entity") != cam_entity_id:
                continue
            coords = presets.get(dd.get("preset", ""))
            wanted[row["id"]] = bool(coords and ptz_near(pos, coords))
        if not wanted:
            return
        changed = False
        with self._lock:
            for eid, ok in wanted.items():
                ent = self._cache.setdefault(eid, {"label": None, "ts": 0.0, "valid": False})
                if ent["valid"] != ok:
                    ent["valid"] = ok
                    if not ok:
                        ent["label"] = None
                        ent["ts"] = 0.0
                    changed = True
            snapshot = {eid: (ok, dict(self._cache[eid])) for eid, ok in wanted.items()}
        if changed:
            for eid, (ok, ent) in snapshot.items():
                if ok and ent["label"]:
                    self._write(eid, ent["label"])
                elif not ok:
                    self._write(eid, "unknown")

    def _reload_maps(self, cam_entity_id: int) -> None:
        row = self.db.one("SELECT driver_data FROM entities WHERE id=?", (cam_entity_id,))
        if row:
            dd = loads(row["driver_data"])
            self._maps[cam_entity_id] = {
                p["name"]: p for p in dd.get("presets", []) if p.get("name")
            }

    # -- đọc state ----------------------------------------------------------

    def snapshot(self, entity_id: int) -> dict:
        with self._lock:
            ent = dict(self._cache.get(entity_id) or {})
        ent.setdefault("label", None)
        ent.setdefault("ts", 0.0)
        ent.setdefault("valid", False)
        return ent

    def wait_fresh(self, entity_id: int, since: float, timeout: float = 8.0) -> dict:
        """Chờ message confirmed mới hơn `since` (dùng cho lệnh goto-đo-về).

        Frigate không publish khi state KHÔNG đổi — hết timeout mà không có
        message thì state hiện tại vẫn đang được xác nhận liên tục: dùng luôn."""
        deadline = time.time() + timeout
        ev = threading.Event()
        with self._lock:
            self._waiters.append((entity_id, since, ev))
        try:
            while True:
                snap = self.snapshot(entity_id)
                if snap["ts"] >= since or time.time() >= deadline:
                    return snap
                ev.wait(0.5)
                ev.clear()
        finally:
            with self._lock:
                self._waiters = [w for w in self._waiters if w[2] is not ev]

    # -- DB -----------------------------------------------------------------

    def _write(self, entity_id: int, label: str) -> None:
        self.db.execute(
            "UPDATE entities SET state=?, updated_at=? WHERE id=?",
            (json.dumps({"state": label}, ensure_ascii=False), now_iso(), entity_id),
        )


_instance: VisionStates | None = None


def instance() -> VisionStates | None:
    return _instance


def start_vision(app) -> None:
    global _instance
    if not app.state.cfg.get("mqtt.enabled", False):
        return
    _instance = VisionStates(app.state.cfg, app.state.db)
    app.state.vision = _instance
    _instance.start()
