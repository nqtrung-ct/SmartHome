"""Tuya WiFi devices, controlled locally through tinytuya (optional install).

Local control needs each device's local key - fetch it once via a free Tuya
IoT developer account / tinytuya wizard, then supply it when adopting.
"""
from __future__ import annotations

import json

from server.db import Database
from server.drivers.base import Driver, DriverError, Row, loads

# Most temp/humidity sensors expose: dps 1 temperature, 2 humidity, 3 battery.
FALLBACK_SENSOR_DPS = {
    "temperature": {"dps": 1, "scale": 1},
    "humidity": {"dps": 2, "scale": 1},
}


def _sensor_dps_map(dps: dict) -> dict:
    """Guess {metric: {dps, scale}} from a status snapshot.

    Tuya temp/humidity sensors use ascending dps ids (temperature, humidity,
    battery). Many LED display models report tenths (323 = 32.3°C), which the
    [100, 600] branch catches. Only trusted for devices without a bool
    (on/off) channel - a metering breaker's current/power readings also fall
    in the numeric ranges and must not become a bogus "sensor".
    """
    nums = sorted(
        (int(k), v) for k, v in dps.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    )
    mapping: dict[str, dict] = {}

    def temp_scale(value) -> float | None:
        if -20 <= value <= 60:
            return 1
        if 100 <= value <= 600:
            return 0.1
        return None

    for key, value in nums:
        if "temperature" not in mapping and (scale := temp_scale(value)) is not None:
            mapping["temperature"] = {"dps": key, "scale": scale}
        elif "humidity" not in mapping and 0 <= value <= 100:
            mapping["humidity"] = {"dps": key, "scale": 1}
        elif "battery" not in mapping and 0 <= value <= 100:
            mapping["battery"] = {"dps": key, "scale": 1}
    return mapping


def decode_phase(data) -> dict:
    """Metering breaker 'phase_x' raw: 8 bytes = [V*10: 2B][W*0.01: 4B][?: 2B]."""
    import base64

    if not data:
        return {}
    try:
        raw = base64.b64decode(data)
    except Exception:
        return {}
    if len(raw) != 8:
        return {}
    return {
        "voltage": int.from_bytes(raw[0:2], "big") / 10,
        "power": int.from_bytes(raw[2:6], "big") / 100,
    }


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
            # breaker / plug: on-off channel; metering dps stay uninterpreted
            specs.append({
                "name": device["name"],
                "entity_type": "switch",
                "aliases": "ổ cắm,công tắc",
                "driver_data": {"dps": switch_dps[0]},
            })
        elif "temperature" in sensor_map and "humidity" in sensor_map:
            specs.append({
                "name": device["name"],
                "entity_type": "sensor",
                "aliases": "cảm biến,nhiệt độ,độ ẩm",
                "driver_data": {"dps_map": sensor_map},
            })
        else:
            # probed but nothing recognized (or asleep mains device): dumb switch
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
        if loads(entity["driver_data"]).get("readonly"):
            raise DriverError("Công tắc này chỉ xem trạng thái, không điều khiển từ xa được")
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

    def cloud_meter(self, api_key, api_secret, region, device_ids: list[str]) -> dict[str, dict]:
        """Voltage/power/energy of metering breakers via the Tuya cloud.

        This firmware hides the metering dps over LAN (detect_available_dps
        skips them), but cloud getstatus carries phase_a raw blobs plus the
        accumulated kWh. Returns {device_id: {"voltage", "power", "energy"}}.
        """
        import tinytuya

        cloud = tinytuya.Cloud(apiKey=api_key, apiSecret=api_secret, apiRegion=region)
        out: dict[str, dict] = {}
        for dev_id in device_ids:
            res = cloud.getstatus(dev_id)
            if isinstance(res, dict):
                res = res.get("result")
            if not isinstance(res, list):
                continue
            codes = {x.get("code"): x.get("value") for x in res if isinstance(x, dict)}
            meter = decode_phase(codes.get("phase_a", ""))
            energy = codes.get("total_forward_energy")
            if energy is not None:
                meter["energy"] = round(energy / 100, 2)
            if meter:
                out[dev_id] = meter
        return out

    def get_state(self, entity: Row, device: Row) -> dict:
        data = loads(entity["driver_data"])
        if data.get("cloud_meter"):
            return {}  # state comes from the scheduler's cloud poll
        dps = self._status_dps(device)
        dps_map = data.get("dps_map")
        if dps_map:  # sensor entity: translate dps ids (+scale) -> metrics
            out = {}
            for metric, spec in dps_map.items():
                dps_id, scale = (spec["dps"], spec.get("scale", 1)) if isinstance(spec, dict) else (spec, 1)
                value = dps.get(str(dps_id))
                if value is not None and scale != 1:
                    value = round(value * scale, 2)
                out[metric] = value
            return out
        # switch entity: report the on/off channel in the common {"state"} shape
        return {"state": "on" if dps.get(str(data.get("dps", 1))) else "off"}
