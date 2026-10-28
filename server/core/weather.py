"""Weather via open-meteo (free, no API key). Cached for 10 minutes."""
from __future__ import annotations

import time

import httpx

from server.config import Config

WMO_VI = {
    0: "trời quang", 1: "chủ yếu quang", 2: "có mây", 3: "nhiều mây",
    45: "sương mù", 48: "sương mù đóng băng",
    51: "mưa phùn nhẹ", 53: "mưa phùn", 55: "mưa phùn dày",
    61: "mưa nhỏ", 63: "mưa vừa", 65: "mưa to",
    66: "mưa lạnh", 67: "mưa lạnh nặng",
    71: "tuyết rơi nhẹ", 73: "tuyết rơi", 75: "tuyết rơi nặng",
    80: "mưa rào nhẹ", 81: "mưa rào", 82: "mưa rào rất to",
    95: "có dông", 96: "dông kèm mưa đá", 99: "dông kèm mưa đá nặng",
}


class Weather:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cache: tuple[float, dict] | None = None

    def _fetch(self) -> dict:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": self.cfg.get("weather.latitude"),
            "longitude": self.cfg.get("weather.longitude"),
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "timezone": "auto",
        }
        resp = httpx.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def snapshot(self) -> dict:
        """Structured current weather, cached 10 minutes. Raises on failure."""
        now = time.time()
        if self._cache and now - self._cache[0] < 600:
            return self._cache[1]
        data = self._fetch()
        cur, day = data["current"], data["daily"]
        code = int(cur.get("weather_code", 0))
        snap = {
            "temperature": round(cur["temperature_2m"]),
            "humidity": round(cur["relative_humidity_2m"]),
            "wind": round(cur["wind_speed_10m"]),
            "code": code,
            "condition": WMO_VI.get(code, "thời tiết bình thường"),
            "temp_max": round(day["temperature_2m_max"][0]),
            "temp_min": round(day["temperature_2m_min"][0]),
            "rain_prob": day.get("precipitation_probability_max", [None])[0],
        }
        self._cache = (now, snap)
        return snap

    def summary(self) -> str:
        """Short Vietnamese weather summary for voice replies."""
        s = self.snapshot()
        parts = [
            f"Bây giờ {s['condition'].lower()}, {s['temperature']} độ C, "
            f"độ ẩm {s['humidity']}%, gió {s['wind']} km/h.",
            f"Hôm nay cao nhất {s['temp_max']} độ, thấp nhất {s['temp_min']} độ",
        ]
        if s["rain_prob"] is not None:
            parts[-1] += f", khả năng mưa {s['rain_prob']:.0f}%"
        parts[-1] += "."
        return " ".join(parts)
