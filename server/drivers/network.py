"""Configured TCP relays for hosts that the server cannot reach directly."""
from __future__ import annotations

from server.drivers.base import DriverError


def tcp_target(cfg, host: str, port: int) -> tuple[str, int]:
    proxies = cfg.get("network.tcp_proxies", {}) if cfg else {}
    proxy = (proxies or {}).get(f"{host}:{port}")
    if not proxy:
        return host, port
    target, sep, value = str(proxy).rpartition(":")
    if not sep or not target or not value.isdecimal() or not 0 < int(value) < 65536:
        raise DriverError(f"Cấu hình TCP proxy không hợp lệ cho {host}:{port}")
    return target, int(value)
