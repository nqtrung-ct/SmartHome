"""Broadlink RM4 IR blasters: send/learn IR codes.

AC remotes send their full state every press, so IR climate entities store a
small map of learned states, e.g. {"off": hex, "cool_25": hex, "cool_26": hex}.
"""
from __future__ import annotations

import re
import time

import broadlink
from broadlink.exceptions import ReadError, StorageError

from server.drivers.base import Driver, DriverError, Row, loads


class BroadlinkDriver(Driver):
    name = "broadlink"

    def __init__(self, cfg):
        self.timeout = int(cfg.get("broadlink.timeout", 5))
        self.local_ip = cfg.get("broadlink.local_ip") or None

    # -- connection ---------------------------------------------------------

    def _discover(self):
        """Discover, trying the configured IP, the auto LAN IP, then the library default.

        On machines with VirtualBox/WSL/VPN adapters the library's own guess
        broadcasts on the wrong interface and finds nothing.
        """
        from server.util import lan_ip

        candidates: list[str | None] = []
        if self.local_ip:
            candidates.append(self.local_ip)
        auto = lan_ip()
        if auto and auto not in candidates:
            candidates.append(auto)
        candidates.append(None)
        found = []
        for local_ip in candidates:
            try:
                found = broadlink.discover(timeout=self.timeout, local_ip_address=local_ip) or []
            except Exception:
                found = []
            if found:
                return found
        return found

    def _connect(self, device: Row):
        extra = loads(device["extra"])
        mac = extra.get("mac") or device["mac"]
        if not device["host"] or not mac:
            raise DriverError("Thiếu địa chỉ IP hoặc MAC của Broadlink")
        devtype = extra.get("devtype", 0)
        if not isinstance(devtype, int):
            # chuỗi kiểu "RM4MINI" -> tự dò lại số hiệu thật qua LAN
            devtype = int(self.probe(device).get("devtype", 0))
        dev = broadlink.gendevice(
            devtype,
            (device["host"], 80),
            bytes.fromhex(mac.replace(":", "").replace("-", "")),
        )
        dev.auth()
        return dev

    def probe(self, device: Row) -> dict:
        """Fill in mac/devtype by rediscovering on the LAN."""
        for dev in self._discover():
            if dev.host[0] == device["host"]:
                return {
                    "mac": dev.mac.hex(),
                    "devtype": dev.type,
                    "model": str(dev.model),
                    "name": dev.name,
                }
        raise DriverError(f"Không tìm thấy Broadlink tại {device['host']}")

    # -- IR -----------------------------------------------------------------

    def learn_ir(self, device: Row, timeout: int = 30) -> str | None:
        dev = self._connect(device)
        dev.enter_learning()
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            try:
                packet = dev.check_data()
                return packet.hex()
            except (ReadError, StorageError):
                continue
        return None

    def send_ir(self, device: Row, code_hex: str) -> None:
        try:
            dev = self._connect(device)
            dev.send_data(bytes.fromhex(code_hex))
        except DriverError:
            raise
        except Exception as exc:  # network/auth failures
            raise DriverError(f"Gửi IR lỗi: {exc}") from exc

    # -- state --------------------------------------------------------------

    def set_state(self, entity: Row, device: Row, command: str, params: dict) -> dict:
        codes = loads(entity["driver_data"]).get("codes", {})
        if not codes:
            raise DriverError(f"'{entity['name']}' chưa học mã IR nào")

        if entity["entity_type"] == "climate":
            # lệnh không nói mức quạt -> dùng mức mặc định trong driver_data
            default_fan = loads(entity["driver_data"]).get("default_fan", "")
            if default_fan and not params.get("fan"):
                params = {**params, "fan": default_fan}
            key = self._climate_key(codes, command, params)
        else:
            key = command if command in codes else ("toggle" if "toggle" in codes else command)

        if key not in codes:
            options = ", ".join(sorted(codes))
            raise DriverError(f"Chưa học mã '{key}' cho {entity['name']} (đã có: {options})")
        self.send_ir(device, codes[key])
        return {"state": command, **params}

    @staticmethod
    def _climate_key(codes: dict, command: str, params: dict) -> str:
        if command in ("on", "off", "toggle"):
            for candidate in (command, "toggle"):
                if candidate in codes:
                    return candidate
            return command
        mode = params.get("mode", "cool")
        temp = int(params.get("temperature", 26))
        fan = str(params.get("fan") or "")
        # mã dạng "{mode}_{temp}" (1 mức quạt) hoặc "{mode}_{fan}_{temp}" (đủ mức)
        exact = f"{mode}_{fan}_{temp}" if fan else f"{mode}_{temp}"
        if exact in codes:
            return exact

        def nearest(prefix: str) -> str | None:
            """Nhiệt độ gần nhất trong nhóm có tiền tố này (mode hoặc mode_fan)."""
            cands = [
                (abs(int(m.group(2)) - temp), m.group(0))
                for m in (re.match(r"([a-z_]+)_(\d{2})$", k) for k in codes)
                if m and m.group(1) == prefix
            ]
            return min(cands)[1] if cands else None

        for prefix in (f"{mode}_{fan}" if fan else mode, mode):
            got = nearest(prefix)
            if got:
                return got
        any_temp = [
            (abs(int(m.group(2)) - temp), m.group(0))
            for m in (re.match(r"([a-z_]+)_(\d{2})$", k) for k in codes)
            if m
        ]
        if any_temp:
            return min(any_temp)[1]
        return exact
