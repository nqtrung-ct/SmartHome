"""Tuya WiFi devices, controlled locally through tinytuya (optional install).

Local control needs each device's local key - fetch it once via a free Tuya
IoT developer account / tinytuya wizard, then supply it when adopting.
"""
from __future__ import annotations

import json

from server.db import Database
from server.drivers.base import Driver, DriverError, Row, loads


class TuyaDriver(Driver):
    name = "tuya"

    def __init__(self, db: Database):
        self.db = db

    def _device(self, device: Row):
        try:
            import tinytuya
        except ImportError as exc:
            raise DriverError("Chưa cài tinytuya (pip install tinytuya)") from exc
        extra = loads(device["extra"])
        try:
            dev = tinytuya.Device(extra["id"], device["host"], extra["key"])
            dev.set_version(float(extra.get("ver", "3.3")))
            return dev
        except Exception as exc:
            raise DriverError(f"Không tạo được kết nối Tuya: {exc}") from exc

    def adopt(self, device: Row, body: dict) -> list[dict]:
        raw = loads(device["extra"])
        local_key = body.get("local_key") or raw.get("key")
        if not local_key:
            raise DriverError("Cần local key của thiết bị Tuya (lấy qua tài khoản Tuya IoT / tinytuya)")
        self.db.execute(
            "UPDATE devices SET extra=? WHERE id=?",
            (json.dumps({**raw, "key": local_key}, ensure_ascii=False), device["id"]),
        )
        return [
            {
                "name": device["name"],
                "entity_type": "switch",
                "aliases": "ổ cắm,công tắc",
                "driver_data": {"dps": 1},
            }
        ]

    def set_state(self, entity: Row, device: Row, command: str, params: dict) -> dict:
        if command not in ("on", "off", "toggle"):
            raise DriverError("Thiết bị Tuya chỉ hỗ trợ bật/tắt")
        dev = self._device(device)
        try:
            if command == "on":
                dev.turn_on()
            elif command == "off":
                dev.turn_off()
            else:
                dps = loads(entity["driver_data"]).get("dps", 1)
                status = dev.status()
                dev.set_value(dps, not status.get("dps", {}).get(str(dps)))
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(f"Lỗi điều khiển Tuya: {exc}") from exc
        return {"state": command}

    def get_state(self, entity: Row, device: Row) -> dict:
        try:
            dev = self._device(device)
            return dev.status().get("dps", {})
        except Exception as exc:
            raise DriverError(f"Không đọc được trạng thái Tuya: {exc}") from exc
