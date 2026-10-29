"""Truy vấn Frigate NVR (Docker, API nội bộ 127.0.0.1:5000).

Frigate chạy person detection trên detect stream của camera — mỗi lần hỏi
"camera có người không" chỉ cần đọc sự kiện person đang mở (end_time null)
hoặc vừa kết thúc vài giây trước.
"""
from __future__ import annotations

import time

import httpx

from server.config import Config


def _base_url() -> str:
    return str(Config().get("frigate.base_url", "http://127.0.0.1:5000")).rstrip("/")


async def person_events(cam: str, limit: int = 10) -> list[dict] | None:
    """Sự kiện person gần nhất của camera; None = Frigate không trả lời."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{_base_url()}/api/events",
                params={"cameras": cam, "label": "person", "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


async def person_present(cam: str, recent: float = 8.0) -> bool | None:
    """Có người trong hình không? None = không kết nối được Frigate.

    Sự kiện đang mở (end_time null) = người còn đó; kết thúc trong vòng
    `recent` giây cũng tính là còn (chồng rơ hở giữa hai sự kiện liền nhau)."""
    events = await person_events(cam)
    if events is None:
        return None
    now = time.time()
    for e in events:
        end = e.get("end_time")
        if end is None or now - float(end) <= recent:
            return True
    return False


async def sole_camera() -> str | None:
    """Tên camera duy nhất trong Frigate (nhà 1 camera khỏi cấu hình map tên)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{_base_url()}/api/config")
            resp.raise_for_status()
            cams = list((resp.json() or {}).get("cameras", {}).keys())
            return cams[0] if len(cams) == 1 else None
    except Exception:
        return None
