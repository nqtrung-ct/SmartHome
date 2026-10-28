"""TTS HTTP service cho config tts.engine: http.

Ưu tiên dùng piper-tts với giọng tiếng Việt trong data/models/tts/
(vi_VN-vais1000-medium). Nếu chưa cài/chưa có model -> trả về khoảng lặng
để chạy thử pipeline.

    POST /tts  {"text": "Đã tắt điều hòa"}  ->  audio/wav bytes

Cài:    pip install piper-tts   (xem optional-requirements.txt)
Model:  tải từ https://huggingface.co/rhasspy/piper-voices (vi/vi_VN/vais1000/medium)
        đặt 2 file .onnx + .onnx.json  vào  data/models/tts/<tên-giọng>/
Chạy:   python scripts/tts_http_example.py   (port 8125)

Sau đó trong tab Trợ lý (hoặc config.yaml):
    tts:
      engine: http
      http_url: http://127.0.0.1:8125/tts

Muốn đổi sang VieNeu-TTS sau này: giữ nguyên giao diện POST {"text"} -> wav này,
chỉ thay hàm synthesize() bên dưới.
"""
from __future__ import annotations

import io
import sys
import wave
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI()

_voice = None  # piper.PiperVoice | None (None = placeholder silence)


def _load_piper():
    models = sorted(Path("data/models/tts").glob("**/*.onnx"))
    if not models:
        print("[tts] Không thấy model trong data/models/tts -> dùng khoảng lặng (placeholder)")
        return None
    try:
        import piper

        print(f"[tts] Đang nạp piper: {models[0]}")
        config = models[0].with_suffix(".onnx.json")
        return piper.PiperVoice.load(
            str(models[0]),
            config_path=str(config) if config.exists() else None,
        )
    except Exception as exc:
        print(f"[tts] Nạp piper thất bại ({exc}) -> dùng khoảng lặng")
        return None


def synthesize(text: str, sample_rate: int = 22050) -> bytes:
    """PCM16 mono WAV của câu text."""
    if _voice is not None:
        # piper >= 1.3: synthesize() yields AudioChunk per sentence
        chunks = _voice.synthesize(text)
        pcm = b"".join(chunk.audio_int16_bytes for chunk in chunks)
        return pcm_to_wav(pcm, _voice.config.sample_rate)
    # placeholder: silence proportional to text length
    duration = min(1.0 + len(text) * 0.04, 20.0)
    return pcm_to_wav(b"\x00\x00" * int(sample_rate * duration), sample_rate)


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class TtsRequest(BaseModel):
    text: str


@app.post("/tts")
def tts(req: TtsRequest) -> Response:
    return Response(content=synthesize(req.text), media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn

    _voice = _load_piper()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8125
    print(f"[tts] listening on http://127.0.0.1:{port}/tts")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
