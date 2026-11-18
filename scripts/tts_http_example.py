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

VieNeu v3 Turbo: cài `pip install vieneu==3.8.4`, chọn trong tab Trợ lý.
Lần đầu cần mạng để tải model; mặc định dùng giọng nữ Mai Anh.
"""
from __future__ import annotations

import io
import importlib.util
import json
import os
import sys
import threading
import wave
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI()

# Tốc độ đọc piper: length_scale càng lớn càng chậm (1.0 = gốc).
# Đổi qua biến môi trường TTS_LENGTH_SCALE rồi khởi động lại sidecar.
LENGTH_SCALE = float(os.environ.get("TTS_LENGTH_SCALE", "1.15"))

_voice = None  # piper.PiperVoice | None (None = placeholder silence)
_vieneu = None
_vieneu_lock = threading.Lock()
_piper_voices = {}
_piper_lock = threading.Lock()


def available_voices() -> dict:
    result = {"vieneu": [], "piper": []}
    spec = importlib.util.find_spec("vieneu")
    if spec and spec.submodule_search_locations:
        path = Path(next(iter(spec.submodule_search_locations))) / "assets/voices_v3_turbo.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            result["vieneu"] = [{"id": name, "label": name + (" — " + v["description"] if v.get("description") else "")}
                                for name, v in data.get("presets", {}).items()]
    root = Path("data/models/tts")
    for model in sorted(root.glob("**/*.onnx")):
        config_path = model.with_suffix(".onnx.json")
        if not config_path.is_file():
            continue
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        model_id = model.relative_to(root).as_posix()
        name = model.parent.name if model.stem == "model" else model.stem
        speakers = data.get("speaker_id_map") or {}
        if speakers:
            result["piper"].extend({"id": f"{model_id}#{speaker_id}", "label": f"{name} — {speaker}"}
                                   for speaker, speaker_id in speakers.items())
        else:
            result["piper"].append({"id": model_id, "label": name})
    return result


@app.get("/voices")
def voices():
    return available_voices()


def synthesize_vieneu(text: str, voice: str = "Mai Anh") -> bytes:
    """Nạp v3 Turbo khi dùng lần đầu; giữ khóa vì backend CPU chạy một luồng."""
    global _vieneu
    with _vieneu_lock:
        if _vieneu is None:
            from vieneu import Vieneu
            # ONNX Runtime từ chối dữ liệu bên ngoài khi các graph trong cache
            # Hugging Face là symlink tới các blob ở thư mục khác. Lưu file thật
            # cạnh nhau; áp dụng cho cả backbone lẫn codec của vieneu 3.8.4.
            from huggingface_hub import hf_hub_download
            from vieneu._v3_turbo_engine.onnx_runtime_lite import OnnxV3LiteEngine

            def fetch_local(repo, files, subfolder):
                base = Path("data/models/vieneu") / repo.replace("/", "--")
                folder = base / subfolder if subfolder else base
                required = [name for name in files if not name.endswith(".json")]
                if subfolder:
                    required += ["config.json", "tokenizer.json"]
                if all((folder / name).is_file() for name in required):
                    return folder
                last = None
                for name in files:
                    try:
                        last = hf_hub_download(repo, name, subfolder=subfolder or None,
                                               local_dir=str(base))
                    except Exception:
                        if name.endswith(".json"):
                            continue
                        raise
                return Path(last).parent

            original_fetch = OnnxV3LiteEngine.__dict__["_fetch"]
            OnnxV3LiteEngine._fetch = staticmethod(fetch_local)
            try:
                _vieneu = Vieneu(mode="v3turbo", backend="onnx")
            finally:
                OnnxV3LiteEngine._fetch = original_fetch
        import numpy as np

        audio = np.asarray(_vieneu.infer(text, voice=voice), dtype=np.float32).reshape(-1)
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        return pcm_to_wav(pcm, 48000)


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


def synthesize(text: str, sample_rate: int = 22050, length_scale: float | None = None, voice: str = "") -> bytes:
    """PCM16 mono WAV của câu text. length_scale càng lớn đọc càng chậm."""
    selected, speaker_id = _voice, None
    if voice:
        if voice not in {v["id"] for v in available_voices()["piper"]}:
            raise ValueError("Giọng Piper không tồn tại")
        model_id, sep, speaker = voice.partition("#")
        speaker_id = int(speaker) if sep else None
        with _piper_lock:
            if model_id not in _piper_voices:
                import piper
                model = Path("data/models/tts") / model_id
                _piper_voices[model_id] = piper.PiperVoice.load(str(model), config_path=str(model.with_suffix(".onnx.json")))
            selected = _piper_voices[model_id]
    if selected is not None:
        scale = length_scale if length_scale else LENGTH_SCALE
        # piper >= 1.3: synthesize() yields AudioChunk per sentence
        try:
            from piper import PiperSynthesisConfig

            opts = PiperSynthesisConfig(length_scale=scale, speaker_id=speaker_id)
            chunks = selected.synthesize(text, opts)
        except (ImportError, TypeError):
            if speaker_id is not None:
                raise RuntimeError("Phiên bản Piper không hỗ trợ chọn speaker")
            chunks = selected.synthesize(text)
        pcm = b"".join(chunk.audio_int16_bytes for chunk in chunks)
        return pcm_to_wav(pcm, selected.config.sample_rate)
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
    length_scale: float | None = None  # None = dùng mặc định LENGTH_SCALE của sidecar
    model: str = "piper"
    voice: str = ""


@app.post("/tts")
def tts(req: TtsRequest) -> Response:
    if req.model == "vieneu":
        try:
            audio = synthesize_vieneu(req.text, req.voice or "Mai Anh")
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"VieNeu chưa sẵn sàng: {exc}") from exc
        return Response(content=audio, media_type="audio/wav", headers={"X-TTS-Model": "vieneu"})
    if req.model != "piper":
        raise HTTPException(status_code=400, detail="Model TTS không được hỗ trợ")
    scale = req.length_scale if req.length_scale else LENGTH_SCALE
    try:
        audio = synthesize(req.text, length_scale=scale, voice=req.voice)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(content=audio, media_type="audio/wav", headers={"X-TTS-Model": "piper"})


if __name__ == "__main__":
    import uvicorn

    _voice = _load_piper()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8125
    print(f"[tts] listening on http://127.0.0.1:{port}/tts")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
