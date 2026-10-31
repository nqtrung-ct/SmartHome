"""Thiết bị WiFi CozyLife (nền DoiT) — giao thức local TCP 5555, JSON + \r\n.

KHÔNG phải Tuya: cổng 6668 đóng, không cần local key, không auth. Giao thức
theo plugin chính thức hass_cozylife_local_pull (github.com/cozylife):
  cmd 0 = info (did/pid/dtp/mac), cmd 2 = query, cmd 3 = set, cmd 10 = push.
Firmware thiết bị thật thường KHÔNG trả lời UDP discovery 6095 (phải dò TCP).

DPID (theo cozylife_local): 1 = bật/tắt (bitmask nếu nhiều nút),
26 = energy kWh, 27 = dòng mA, 28 = công suất W, 29 = điện áp V, 42 = tổng kWh.
"""
from __future__ import annotations

import json
import socket
import time

from server.db import Database
from server.drivers.base import Driver, DriverError, Row, loads

PORT = 5555
# DPID đo điện của ổ cắm CozyLife có công tắc điện: {metric: dpid}
METER_DPIDS = {"energy": 26, "current": 27, "power": 28, "voltage": 29}


def cozylife_request(host: str, cmd: int, msg: dict, timeout: float = 3.0) -> dict | None:
    """Một giao dịch TCP ngắn: gửi JSON+\\r\\n, đọc frame đầu về. None = không trả lời."""
    sn = str(int(time.time() * 1000))
    frame = json.dumps({"cmd": cmd, "pv": 0, "sn": sn, "msg": msg}, separators=(",", ":")) + "\r\n"
    try:
        with socket.create_connection((host, PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(frame.encode())
            buf = b""
            while b"\r\n" not in buf:
                chunk = s.recv(1024)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return None
    line = buf.split(b"\r\n", 1)[0].strip()
    try:
        return json.loads(line)
    except (ValueError, TypeError):
        return None


class CozyLifeDriver(Driver):
    name = "cozylife"

    def __init__(self, db: Database):
        self.db = db

    def _query(self, device: Row) -> dict:
        """Query toàn bộ DPID (attr 0 = all). Trả về {} nếu có trả lời nhưng rỗng;
        raise DriverError khi thiết bị im lặng (scheduler cần phân biệt để ghi 'off')."""
        res = cozylife_request(device["host"], 2, {"attr": [0]})
        if res is None:
            raise DriverError(f"Không đọc được trạng thái CozyLife ({device['host']})")
        msg = res.get("msg") or {}
        data = msg.get("data")
        return data if isinstance(data, dict) else {}

    def adopt(self, device: Row, body: dict) -> list[dict]:
        raw = loads(device["extra"])
        try:
            data = self._query(device)
        except DriverError:
            data = {}  # offline lúc thêm: vẫn tạo công tắc, poll sẽ bắt sau
        info = cozylife_request(device["host"], 0, {}) or {}
        msg = info.get("msg") or {}

        specs = [{
            "name": device["name"],
            "entity_type": "switch",
            "aliases": "công tắc,ổ cắm",
            "driver_data": {
                "did": raw.get("did") or msg.get("did") or "",
                "pid": raw.get("pid") or msg.get("pid") or "",
                "attr": 1,
                # tạm thời chỉ xem trạng thái (công tắc cấp nguồn cho thiết bị
                # khác —vd PC): đổi thành false (Sửa trong DB) để cho phép bật/tắt
                "readonly": True,
            },
        }]
        meter_map = {m: d for m, d in METER_DPIDS.items() if str(d) in data}
        if meter_map:
            specs.append({
                "name": f"{device['name']} điện năng",
                "entity_type": "sensor",
                "aliases": "cảm biến,công suất,điện năng",
                "driver_data": {"meter_map": meter_map},
            })
        return specs

    def set_state(self, entity: Row, device: Row, command: str, params: dict) -> dict:
        if loads(entity["driver_data"]).get("readonly"):
            raise DriverError("Công tắc này chỉ xem trạng thái, không điều khiển từ xa được")
        if entity["entity_type"] == "sensor":
            raise DriverError("Cảm biến chỉ đọc, không bật/tắt được")
        if command not in ("on", "off", "toggle"):
            raise DriverError("Thiết bị CozyLife chỉ hỗ trợ bật/tắt")
        attr = int(loads(entity["driver_data"]).get("attr", 1))
        data = self._query(device)
        current = data.get(str(attr), 0)
        if command == "toggle":
            value = 0 if current else 1
        else:
            value = 1 if command == "on" else 0
        # DPID 1 là bitmask trên công tắc nhiều nút: chỉ ghi bit của nút này
        if current > 1:
            value = (current & ~(1 << (attr - 1))) | (value << (attr - 1))
        res = cozylife_request(device["host"], 3, {"attr": [attr], "data": {str(attr): value}})
        if not res or res.get("res") not in (0, None):
            raise DriverError("Công tắc CozyLife không nhận lệnh (thiết bị bận hoặc offline)")
        return {"state": command}

    def get_state(self, entity: Row, device: Row) -> dict:
        data = self._query(device)
        dd = loads(entity["driver_data"])
        if dd.get("meter_map"):  # sensor ổ cắm đo điện
            out: dict[str, float] = {}
            for metric, dpid in dd["meter_map"].items():
                value = data.get(str(dpid))
                if value is None:
                    continue
                if metric == "current":
                    value = round(value / 1000, 3)  # mA -> A
                elif metric == "energy":
                    value = round(value / 1000, 2)  # Wh -> kWh
                out[metric] = value
            return out
        attr = str(dd.get("attr", 1))
        value = data.get(attr)
        if value is None:
            return {}
        # giá trị 0/1 = nút đơn; >1 là bitmask nhiều nút — lấy bit của nút này
        on = bool(value >> (int(attr) - 1) & 1) if isinstance(value, int) else bool(value)
        return {"state": "on" if on else "off"}
