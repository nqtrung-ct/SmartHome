"""Hands-free voice loopback test.

Synthesizes Vietnamese with the local piper TTS service, resamples to 16 kHz
and feeds it to /ws/voice as if it were a mic, then checks:

  1. no wake word  -> utterance ignored
  2. "... over"    -> finalized by the keyword, well before the 3 s silence
  3. no "over"     -> finalized by 3 s of silence, weather answered

  python scripts/ws_handsfree_test.py [base_url] [admin_token]
"""
from __future__ import annotations

import asyncio
import json
import struct
import sys
import time

import httpx
import numpy as np
import websockets

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8124"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else "changeme"
TTS_URL = "http://127.0.0.1:8125/tts"
SILENCE_MS = 3000
QUIET_S = 6.0  # quiet seconds streamed after speech (must exceed SILENCE_MS)


def synth16k(text: str) -> bytes:
    """TTS -> 16 kHz PCM16 (what a mic would send)."""
    wav = httpx.post(TTS_URL, json={"text": text}, timeout=60).content
    assert wav[:4] == b"RIFF", f"piper trả về không phải WAV: {wav[:20]!r}"
    rate = struct.unpack("<I", wav[24:28])[0]
    off = wav.find(b"data") + 8
    data = np.frombuffer(wav[off:], dtype=np.int16).astype(np.float64)
    n = int(len(data) * 16000 / rate)
    out = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data)
    return out.astype(np.int16).tobytes()


async def run_case(token: str, name: str, text: str, quiet_s: float) -> tuple[float, list[dict]]:
    """Stream one utterance on a fresh connection; returns (speech_end, events)."""
    ws_url = BASE.replace("http", "ws") + "/ws/voice?token=" + token
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"type": "handsfree", "silence_ms": SILENCE_MS}))
        hello = json.loads(await ws.recv())
        assert hello["type"] == "listening", hello

        events: list[dict] = []
        reader = asyncio.create_task(_reader(ws, events))

        pcm = synth16k(text)
        chunk = 3200  # 100 ms at 16 kHz
        for i in range(0, len(pcm), chunk):
            await ws.send(pcm[i : i + chunk])
            await asyncio.sleep(0.1)
        speech_end = time.monotonic()

        zero = b"\x00\x00" * 1600
        for _ in range(int(quiet_s * 10)):
            await ws.send(zero)
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.0)
        reader.cancel()

    print(f"--- {name} ---")
    for t, ev in events:
        label = ev.get("type")
        if label in ("reply", "ignored"):
            label += " " + ev.get("text", "")[:60]
        print(f"  +{t - speech_end:5.2f}s  {label}")
    return speech_end, events


async def _reader(ws, events: list) -> None:
    while True:
        msg = await ws.recv()
        if isinstance(msg, str):
            events.append((time.monotonic(), json.loads(msg)))


async def main() -> None:
    global AUTH
    AUTH = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(base_url=BASE, timeout=15) as c:
        code = (await c.post("/api/pairing-codes", headers=AUTH)).json()["code"]
        client = (await c.post("/api/pair", json={"code": code, "client_name": "hf-test"})).json()
    try:
        await run_cases(client["token"])
    finally:
        # dọn client test để không đọng dòng rác trong tab Máy thoại
        async with httpx.AsyncClient(base_url=BASE, timeout=15) as c:
            await c.delete(f"/api/voice-clients/{client['client_id']}", headers=AUTH)


async def run_cases(token: str) -> None:
    passed = 0
    # 1) no wake word -> ignored, no command runs
    _, events = await run_case(token, "không có từ đánh thức",
                               "Cho tôi biết thời tiết hôm nay.", QUIET_S)
    types = [e.get("type") for _, e in events]
    if "ignored" in types and "reply" not in types:
        print("[PASS] bỏ qua khi thiếu từ đánh thức\n"); passed += 1
    else:
        print(f"[FAIL] muốn ignored mà thấy: {types}\n")

    # 2) end keyword ends the utterance before the silence timer could.
    #    STT renders the keyword differently each take, so try a few times.
    keyword_ok = False
    for take in range(3):
        speech_end, events = await run_case(token, f"kết thúc bằng từ khóa (lần {take + 1})",
                                            "Trợ lý ơi, cho tôi biết thời tiết hôm nay. Kết thúc.",
                                            QUIET_S)
        t_transcript = next(
            (t - speech_end for t, e in events if e.get("type") == "transcript"), None
        )
        types = [e.get("type") for _, e in events]
        if t_transcript is not None and t_transcript < SILENCE_MS / 1000 - 0.3 and "reply" in types:
            print(f"[PASS] từ khóa chốt sớm: transcript sau {t_transcript:.2f}s "
                  f"(im {SILENCE_MS/1000}s chưa đến)\n")
            keyword_ok = True
            break
        print(f"  lần này từ khóa không khớp, rơi về im lặng (+{t_transcript}s)\n")
    passed += keyword_ok

    # 3) no "over" -> 3 s silence ends it, weather answered by voice
    speech_end, events = await run_case(token, "kết thúc bằng im lặng 3s",
                                        "Trợ lý ơi, cho tôi biết thời tiết hôm nay.", QUIET_S)
    t_transcript = next((t - speech_end for t, e in events if e.get("type") == "transcript"), None)
    reply = next((e.get("text", "") for _, e in events if e.get("type") == "reply"), "")
    types = [e.get("type") for _, e in events]
    if (t_transcript is not None and t_transcript >= SILENCE_MS / 1000 - 0.4
            and "độ" in reply and "listening" in types):
        print(f"[PASS] im lặng 3s chốt (+{t_transcript:.2f}s), trả lời thời tiết\n"); passed += 1
    else:
        print(f"[FAIL] im lặng không chốt đúng (transcript +{t_transcript}s), types={types}\n")

    print(f"{passed}/3 pass")
    sys.exit(0 if passed == 3 else 1)


if __name__ == "__main__":
    asyncio.run(main())
