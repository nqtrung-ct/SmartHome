"""Zigbee via Zigbee2MQTT over Mosquitto.

Zigbee2MQTT owns pairing; we just watch MQTT. Sensor readings update entity
state, and paired-but-not-adopted devices show up in the Discovered list.
"""
from __future__ import annotations

import json
import threading
import time

from server.db import Database, log_event
from server.drivers.base import Driver, Row, loads
from server.util import now_iso


class ZigbeeDriver(Driver):
    name = "zigbee"

    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db
        self.base_topic = cfg.get("mqtt.base_topic", "zigbee2mqtt")
        self.client = None
        self._stop = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if not self.cfg.get("mqtt.enabled", True):
            return
        threading.Thread(target=self._run, name="zigbee-mqtt", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass

    def _run(self) -> None:
        import paho.mqtt.client as mqtt

        while not self._stop.is_set():
            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="smarthome-server")
                client.on_connect = self._on_connect
                client.on_message = self._on_message
                client.connect(
                    self.cfg.get("mqtt.host", "127.0.0.1"),
                    int(self.cfg.get("mqtt.port", 1883)),
                    keepalive=60,
                )
                self.client = client
                client.loop_forever(retry_first_connection=False)
            except Exception as exc:
                log_event(self.db, "zigbee", f"MQTT không kết nối được: {exc}", level="warn")
            if self._stop.wait(30):
                return

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe(f"{self.base_topic}/#")
            log_event(self.db, "zigbee", "Đã kết nối MQTT")
        else:
            log_event(self.db, "zigbee", f"MQTT từ chối kết nối: {reason_code}", level="warn")

    # -- messages -----------------------------------------------------------

    def _on_message(self, client, userdata, msg):
        try:
            self._handle(msg.topic, msg.payload)
        except Exception as exc:
            log_event(self.db, "zigbee", f"Lỗi xử lý MQTT: {exc}", level="warn")

    def _handle(self, topic: str, payload: bytes) -> None:
        remainder = topic[len(self.base_topic) + 1:] if topic.startswith(self.base_topic) else ""
        if not remainder:
            return

        if remainder.startswith("bridge/"):
            if remainder == "bridge/devices":
                self._ingest_device_list(json.loads(payload))
            return

        friendly = remainder.split("/")[0]
        data = json.loads(payload)
        if not isinstance(data, dict):
            return
        row = self.db.one(
            "SELECT * FROM entities WHERE driver_data LIKE ? AND entity_type='sensor'",
            (f'%"{friendly}"%',),
        )
        if row:
            self.db.execute(
                "UPDATE entities SET state=?, updated_at=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), now_iso(), row["id"]),
            )
        else:
            self._upsert_discovered(friendly, data)

    def _ingest_device_list(self, devices: list) -> None:
        if not isinstance(devices, list):
            return
        adopted = {
            loads(r["driver_data"]).get("friendly_name")
            for r in self.db.query("SELECT driver_data FROM entities WHERE entity_type='sensor'")
        }
        for dev in devices:
            friendly = dev.get("friendly_name", "")
            if not friendly or dev.get("role") == "coordinator" or friendly in adopted:
                continue
            definition = dev.get("definition") or {}
            self._upsert_discovered(friendly, dev, definition)

    def _upsert_discovered(self, friendly: str, data: dict, definition: dict | None = None) -> None:
        definition = definition or data.get("definition") or {}
        raw = json.dumps(
            {"friendly_name": friendly, "ieee": data.get("ieee_address"), "definition": definition},
            ensure_ascii=False,
        )
        with self.db.conn() as c:
            c.execute(
                """
                INSERT INTO discovered(method, ident, name, brand, supported, raw, first_seen, last_seen)
                VALUES ('zigbee', ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(method, ident) DO UPDATE SET
                  raw=excluded.raw, last_seen=excluded.last_seen,
                  brand=CASE WHEN excluded.brand != '' THEN excluded.brand ELSE discovered.brand END
                """,
                (
                    friendly,
                    friendly,
                    definition.get("vendor", "") or "Zigbee",
                    raw,
                    now_iso(),
                    now_iso(),
                ),
            )

    # -- driver API ---------------------------------------------------------

    def adopt(self, device: Row, body: dict) -> list[dict]:
        extra = loads(device["extra"])
        friendly = extra.get("friendly_name") or device["name"]
        self.db.execute(
            "UPDATE devices SET extra=? WHERE id=?",
            (json.dumps({"friendly_name": friendly}, ensure_ascii=False), device["id"]),
        )
        return [
            {
                "name": device["name"],
                "entity_type": "sensor",
                "aliases": "cảm biến,nhiệt độ,độ ẩm",
                "driver_data": {"friendly_name": friendly},
            }
        ]

    def get_state(self, entity: Row, device: Row) -> dict:
        return loads(entity["state"])
