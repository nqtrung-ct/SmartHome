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

DAY_LABELS = {1: "Ngày mai", 2: "Ngày mốt"}


class Weather:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cache: tuple[float, tuple[dict, list[dict], dict]] | None = None

    def _fetch(self) -> dict:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": self.cfg.get("weather.latitude"),
            "longitude": self.cfg.get("weather.longitude"),
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "hourly": "precipitation_probability",
            "timezone": "auto",
            "forecast_days": 4,
        }
        resp = httpx.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _data(self) -> tuple[dict, list[dict], dict]:
        """(current snapshot, per-day forecast list, hourly rain), cached 10 min."""
        now = time.time()
        if self._cache and now - self._cache[0] < 600:
            return self._cache[1]
        raw = self._fetch()
        cur, day = raw["current"], raw["daily"]
        code = int(cur.get("weather_code", 0))
        current = {
            "temperature": round(cur["temperature_2m"]),
            "humidity": round(cur["relative_humidity_2m"]),
            "wind": round(cur["wind_speed_10m"]),
            "code": code,
            "condition": WMO_VI.get(code, "thời tiết bình thường"),
        }
        dates = day.get("time", [])
        rain_probs = day.get("precipitation_probability_max") or []
        daily = []
        for i, raw_code in enumerate(day.get("weather_code", [])):
            code = int(raw_code)
            daily.append({
                "date": dates[i] if i < len(dates) else "",
                "code": code,
                "condition": WMO_VI.get(code, "thời tiết bình thường"),
                "temp_max": round(day["temperature_2m_max"][i]),
                "temp_min": round(day["temperature_2m_min"][i]),
                "rain_prob": rain_probs[i] if i < len(rain_probs) else None,
            })
        hourly = raw.get("hourly") or {}
        hours = {"time": hourly.get("time", []), "rain": hourly.get("precipitation_probability", [])}
        self._cache = (now, (current, daily, hours))
        return current, daily, hours

    def snapshot(self) -> dict:
        """Current readings merged with today's forecast (for /api/display)."""
        current, daily, _ = self._data()
        return {**current, **daily[0]}

    def summary(self, day: int = 0) -> str:
        """Short Vietnamese weather summary for voice replies. day: 0 today,
        1 tomorrow, 2 the day after."""
        if day <= 0:
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

        _, daily, _ = self._data()
        if day >= len(daily):
            raise ValueError(f"mình chỉ có dự báo {len(daily)} ngày")
        s = daily[day]
        label = DAY_LABELS.get(day, f"Sau {day} ngày")
        text = (f"{label} {s['condition'].lower()}, cao nhất {s['temp_max']} độ, "
                f"thấp nhất {s['temp_min']} độ")
        if s["rain_prob"] is not None:
            text += f", khả năng mưa {s['rain_prob']:.0f}%"
        return text + "."

    def rain(self, day: int = 0, evening: bool = False) -> int | None:
        """Rain probability % for a day; evening=True uses that day's 18-23h."""
        _, daily, hours = self._data()
        if day >= len(daily):
            raise ValueError(f"mình chỉ có dự báo {len(daily)} ngày")
        daily_prob = daily[day].get("rain_prob")
        if not evening:
            return daily_prob
        date = daily[day].get("date", "")
        probs = [
            hours["rain"][i]
            for i, t in enumerate(hours["time"])
            if t[:10] == date and 18 <= int(t[11:13]) <= 23 and i < len(hours["rain"])
        ]
        return max((p for p in probs if p is not None), default=daily_prob)
