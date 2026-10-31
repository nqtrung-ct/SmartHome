"""Configuration: config.yaml + defaults + SMARTHOME_* environment overrides."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"

DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 8124,
        "admin_token": "changeme",
        "data_dir": "data",
        "advertise_mdns": True,
    },
    "mqtt": {"enabled": False, "host": "127.0.0.1", "port": 1883, "base_topic": "zigbee2mqtt"},
    "stt": {"engine": "none", "model_dir": "data/models/stt", "num_threads": 2},
    "tts": {"engine": "none", "http_url": ""},
    "llm": {"engine": "ollama", "base_url": "http://127.0.0.1:11434", "model": "qwen3:4b", "timeout": 30},
    "weather": {"latitude": 21.0285, "longitude": 105.8542},
    "discovery": {
        "network_scan": True,
        "subnet": "auto",
        "mdns_seconds": 3,
        "ssdp_seconds": 3,
        "wsdiscovery": True,
    },
    "broadlink": {"timeout": 5},
    "camera": {"poll_seconds": 60},  # chu kỳ kiểm tra camera online (socket RTSP)
    "solar": {"poll_seconds": 180},  # chu kỳ đọc inverter Senergy (Modbus TCP) - đừng nhanh hơn 30s
    "frigate": {"base_url": "http://127.0.0.1:5000"},  # API nội bộ của NVR Docker (map 127.0.0.1)
    "display": {"pin": ""},  # mã 4 số mở khóa điều khiển trên màn hình treo tường
    "retention": {"days": 30},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(value: str) -> Any:
    low = value.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


class Config:
    """Read-only view over merged config with dot-notation access."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else DEFAULT_CONFIG_PATH
        data: dict[str, Any] = {}
        if self.path.exists():
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        data = _deep_merge(DEFAULTS, data)
        data = self._apply_env(data)
        self._data = data

    @staticmethod
    def _apply_env(data: dict) -> dict:
        prefix = "SMARTHOME_"
        for key, raw in os.environ.items():
            if not key.startswith(prefix):
                continue
            body = key[len(prefix):].lower()
            # "__" separates section from key cleanly (e.g. DISCOVERY__NETWORK_SCAN);
            # a single "_" split stays for simple one-word keys (SERVER_PORT).
            parts = body.split("__") if "__" in body else body.split("_")
            if len(parts) < 2:
                continue
            node = data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
                if not isinstance(node, dict):
                    break
            if isinstance(node, dict):
                node[parts[-1]] = _coerce(raw)
        return data

    def get(self, key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def data_dir(self) -> Path:
        path = Path(str(self.get("server.data_dir", "data")))
        return path if path.is_absolute() else ROOT / path

    def data_path(self, *parts: str) -> Path:
        return self.data_dir.joinpath(*parts)
