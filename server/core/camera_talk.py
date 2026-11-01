"""Phát thông báo qua LOA camera trong phòng — ĐÃ THỬ THẤT BẠI trên Imou.

Firmware Imou IPC-A32P-PRO nhận kênh NetSDK talk (StartTalkEx nMode=0 mở,
TalkSendData trả OK) nhưng KHÔNG đổi loa — audio không phát ra (kiểm chứng
bằng tai + đo Goertzel 1kHz trên mic camera, so với control). Đường broadcast
nMode=1 lỗi 0x80000196, StartTalkChannel lỗi 0x80000007, HTTP CGI khóa hết.
Loa camera chỉ phát qua app Imou Life (cloud/P2P).

Module + speak() trong drivers/dahua_ptz.py GIỮ LẠI cho khi nào có camera
Dahua chính hãng — ĐỪNG nối announce_to_room vào scheduler (log sẽ báo "đã
đọc" một cách sai sự thật vì camera nhận data không lỗi).
"""
from __future__ import annotations

import audioop
import struct


def _pcm8k_from_wav(wav: bytes, rate: int) -> bytes | None:
    """WAV -> PCM16 mono 8kHz không đầu (NetSDK talk), hết 2 ValueError nếu WAV lạ."""
    if rate == 8000:
        start = wav.find(b"data")
        return wav[start + 8:] if start >= 0 else wav
    try:
        start = wav.find(b"data")
        pcm = wav[start + 8:] if start >= 0 else wav
        # WAV stereo: trộn về mono trước khi hạ tần số
        if len(wav) >= 44 and wav[22:24] == b"\x02\x00":
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        out, _ = audioop.ratecv(pcm, 2, 1, rate, 8000, None)
        return out
    except (ValueError, struct.error):
        return None


async def announce_to_room(app, room_id, text: str) -> bool:
    """Đọc `text` qua loa mọi camera trong phòng. True nếu có camera nhận phát.
    TTS lỗi / không có camera / camera offline -> False (kênh khác vẫn chạy)."""
    if not text or room_id is None:
        return False
    pipeline = getattr(app.state, "pipeline", None)
    if not pipeline:
        return False
    import asyncio

    import server.drivers.dahua_ptz as ptz
    from server.db import log_event

    rows = [
        r for r in app.state.db.query(
            """SELECT d.host, d.mac, d.extra FROM entities e
               JOIN devices d ON d.id = e.device_id
               WHERE e.entity_type='camera' AND COALESCE(e.room_id, d.room_id)=?
                 AND d.host != ''""",
            (room_id,))
    ]
    if not rows:
        return False
    out = await pipeline.tts_audio(text)
    if not out:
        return False
    wav, rate = out
    pcm = _pcm8k_from_wav(wav, rate)
    if not pcm:
        return False
    spoke = False
    for r in rows:
        device = {"host": r["host"], "mac": r["mac"], "extra": r["extra"]}
        try:
            await asyncio.to_thread(ptz.speak, app.state.cfg, device, pcm)
            spoke = True
        except Exception as exc:
            log_event(app.state.db, "camera",
                      f"Phát loa camera {r['host']} lỗi: {exc}", level="warn")
    if spoke:
        log_event(app.state.db, "camera",
                  f"Đã đọc thông báo qua loa camera (phòng {room_id}): {text[:60]}")
    return spoke
