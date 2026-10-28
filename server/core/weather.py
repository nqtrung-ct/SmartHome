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
        self._cache: tuple[float, str] | None = None

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

    def summary(self) -> str:
        """Short Vietnamese weather summary, cached 10 minutes."""
        now = time.time()
        if self._cache and now - self._cache[0] < 600:
            return self._cache[1]
        data = self._fetch()
        cur = data["current"]
        day = data["daily"]
        code = int(cur.get("weather_code", 0))
        desc = WMO_VI.get(code, "thời tiết bình thường")
        rain = day.get("precipitation_probability_max", [None])[0]
        parts = [
            f"Bây giờ {desc.lower()}, {cur['temperature_2m']:.0f} độ C, "
            f"độ ẩm {cur['relative_humidity_2m']:.0f}%, gió {cur['wind_speed_10m']:.0f} km/h.",
            f"Hôm nay cao nhất {day['temperature_2m_max'][0]:.0f} độ, "
            f"thấp nhất {day['temperature_2m_min'][0]:.0f} độ",
        ]
        if rain is not None:
            parts[-1] += f", khả năng mưa {rain:.0f}%"
        parts[-1] += "."
        text = " ".join(parts)
        self._cache = (now, text)
        return text
