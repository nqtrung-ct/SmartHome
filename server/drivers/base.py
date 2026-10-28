"""Driver interface. One driver per brand; entities are functions of a device."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


class DriverError(Exception):
    """User-facing driver failure - the message is shown/spoken in Vietnamese."""


Row = sqlite3.Row


def loads(value: str) -> dict:
    return json.loads(value) if value else {}


class Driver:
    name = "base"

    def start(self) -> None:
        """Background work (e.g. the Zigbee MQTT listener). Called once at startup."""

    def stop(self) -> None:
        pass

    def adopt(self, device: Row, body: dict) -> list[dict]:
        """Default entities for a newly adopted device.

        body carries whatever the adoption form supplied (local_key, rtsp_password...).
        Returns a list of entity dicts: name, entity_type, aliases, driver_data.
        """
        return []

    def set_state(self, entity: Row, device: Row, command: str, params: dict) -> dict:
        """command: 'on' | 'off' | 'toggle' | 'set'. Returns the new state dict."""
        raise DriverError(f"Driver '{self.name}' không hỗ trợ điều khiển")

    def get_state(self, entity: Row, device: Row) -> dict:
        return loads(entity["state"])

    def learn_ir(self, device: Row, timeout: int = 30) -> str | None:
        raise DriverError(f"Driver '{self.name}' không hỗ trợ học mã IR")

    def send_ir(self, device: Row, code_hex: str) -> None:
        raise DriverError(f"Driver '{self.name}' không hỗ trợ gửi mã IR")
