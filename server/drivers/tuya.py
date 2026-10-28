"""Tuya WiFi devices, controlled locally through tinytuya (optional install).

Local control needs each device's local key - fetch it once via a free Tuya
IoT developer account / tinytuya wizard, then supply it when adopting.
"""
from __future__ import annotations

import json

from server.db import Database
from server.drivers.base import Driver, DriverError, Row, loads

# Most temp/humidity sensors expose: dps 1 temperature, 2 humidity, 3 battery.
FALLBACK_SENSOR_DPS = {"temperature": 1, "humidity": 2}


def _sensor_dps_map(dps: dict) -> dict:
    """Guess {metric: dps id} from a status snapshot.

    Tuya temp/humidity sensors use ascending dps ids (temperature, humidity,
    battery); plugs with a sensor probe mix a bool channel in and get both
    a switch and a sensor entity.
    """
    nums = sorted(
        (int(k), v) for k, v in dps.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    )
    mapping: dict[str, int] = {}
    for key, value in nums:
        if "temperature" not in mapping and -20 <= value <= 60:
            mapping["temperature"] = key
        elif "humidity" not in mapping and 0 <= value <= 100:
            mapping["humidity"] = key
        elif "battery" not in mapping and 0 <= value <= 100:
            mapping["battery"] = key
    return mapping


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
            if hasattr(dev, "set_socketTimeout"):
                dev.set_socketTimeout(3)
            return dev
        except Exception as exc:
            raise DriverError(f"Không tạo được kết nối Tuya: {exc}") from exc

    def _status_dps(self, device: Row) -> dict:
        try:
            return self._device(device).status().get("dps", {}) or {}
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(f"Không đọc được trạng thái Tuya: {exc}") from exc

    def adopt(self, device: Row, body: dict) -> list[dict]:
        raw = loads(device["extra"])
        local_key = body.get("local_key") or raw.get("key")
        if not local_key:
            raise DriverError("Cần local key của thiết bị Tuya (lấy qua tài khoản Tuya IoT / tinytuya)")
        self.db.execute(
            "UPDATE devices SET extra=? WHERE id=?",
            (json.dumps({**raw, "key": local_key}, ensure_ascii=False), device["id"]),
        )
        device = self.db.one("SELECT * FROM devices WHERE id=?", (device["id"],))

        try:
            dps = self._status_dps(device)
        except DriverError:
            dps = {}  # battery sensors sleep between reports - cannot probe now

        switch_dps = sorted(int(k) for k, v in dps.items() if isinstance(v, bool))
        sensor_map = _sensor_dps_map(dps) if dps else dict(FALLBACK_SENSOR_DPS)

        specs: list[dict] = []
        if switch_dps:
            specs.append({
                "name": device["name"],
                "entity_type": "switch",
                "aliases": "ổ cắm,công tắc",
                "driver_data": {"dps": switch_dps[0]},
            })
        if "temperature" in sensor_map and "humidity" in sensor_map:
            specs.append({
                "name": f"Cảm biến {device['name']}" if specs else device["name"],
                "entity_type": "sensor",
                "aliases": "cảm biến,nhiệt độ,độ ẩm",
                "driver_data": {"dps_map": sensor_map},
            })
        if not specs:
            # probed fine but nothing recognized - behave like the old switch
            specs.append({
                "name": device["name"],
                "entity_type": "switch",
                "aliases": "ổ cắm,công tắc",
                "driver_data": {"dps": switch_dps[0] if switch_dps else 1},
            })
        return specs

    def set_state(self, entity: Row, device: Row, command: str, params: dict) -> dict:
        if entity["entity_type"] == "sensor":
            raise DriverError("Cảm biến chỉ đọc, không bật/tắt được")
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
        dps = self._status_dps(device)
        data = loads(entity["driver_data"])
        if data.get("dps_map"):  # sensor entity: translate dps ids -> metrics
            return {
                metric: dps.get(str(dps_id))
                for metric, dps_id in data["dps_map"].items()
            }
        return dps
