"""Virtual devices for testing the whole stack without hardware."""
from __future__ import annotations

import time

from server.drivers.base import Driver, Row


class DummyDriver(Driver):
    name = "dummy"

    def set_state(self, entity: Row, device: Row, command: str, params: dict) -> dict:
        if entity["entity_type"] in ("switch", "light"):
            return {"state": command if command != "set" else "on"}
        if entity["entity_type"] == "climate":
            if command in ("on", "off", "toggle"):
                return {"state": command}
            return {"state": "on", **params}
        raise DriverError("Thiết bị demo này chỉ hỗ trợ bật/tắt")

    def get_state(self, entity: Row, device: Row) -> dict:
        if entity["entity_type"] == "sensor":
            # gently drifting fake readings
            t = time.time()
            return {
                "temperature": round(27 + 2 * (t % 3600) / 3600, 1),
                "humidity": int(62 + 5 * (t % 1800) / 1800),
                "battery": 100,
            }
        return {}
