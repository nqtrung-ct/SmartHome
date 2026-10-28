"""Mẫu TTS HTTP service cho config tts.engine: http.

Chạy model TTS bạn chọn (VieNeu-TTS, Piper, ...) và expose nó qua:

    POST /tts  {"text": "Đã tắt điều hòa"}  ->  audio/wav bytes (PCM16)

Cài: pip install fastapi uvicorn
Chạy: python scripts/tts_http_example.py   (port 8125)

Sau đó trong config.yaml:
    tts:
      engine: http
      http_url: http://127.0.0.1:8125/tts
"""
from __future__ import annotations

import io
import wave

from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI()


class TtsRequest(BaseModel):
    text: str


def generate_pcm16(text: str, sample_rate: int = 16000) -> bytes:
    """THAY hàm này bằng model TTS thật của bạn.

    Ví dụ với VieNeu-TTS / bất kỳ model nào: trả về bytes PCM16 mono
    ở sample_rate cho trước. Bản mẫu này trả về khoảng lặng dài theo độ dài câu
    để chạy thử pipeline.
    """
    duration = min(1.0 + len(text) * 0.04, 20.0)
    return b"\x00\x00" * int(sample_rate * duration)


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


@app.post("/tts")
def tts(req: TtsRequest) -> Response:
    rate = 16000
    wav = pcm_to_wav(generate_pcm16(req.text, rate), rate)
    return Response(content=wav, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8125)
