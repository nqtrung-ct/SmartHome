"""Trợ lý tab: AI model status, config persistence, TTS/voice testing."""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from server.api.deps import require_admin
from server.db import log_event
from server.util import set_yaml_value
from server.voice.stt import make_stt
from server.voice.tts import make_tts

router = APIRouter()


def _ollama_models(cfg):
    """Installed Ollama models [{name, size_gb}], or (None, error)."""
    # Ollama luôn ở llm.ollama_url — llm.base_url là URL cloud (đã trả giá:
    # dùng base_url lúc cấu hình cloud là hỏi /api/tags lên endpoint cloud)
    base = str(cfg.get("llm.ollama_url", "") or
               "http://127.0.0.1:11434").rstrip("/")
    try:
        resp = httpx.get(base + "/api/tags", timeout=5)
        resp.raise_for_status()
        models = [
            {"name": m.get("name", ""), "size_gb": round(m.get("size", 0) / 1e9, 1)}
            for m in resp.json().get("models", [])
        ]
        return models, None
    except Exception as exc:
        return None, str(exc)


def _cloud_models(cfg):
    """Model list của dịch vụ OpenAI-compatible (GET /models), or (None, error)."""
    base = str(cfg.get("llm.base_url", "")).rstrip("/")
    try:
        resp = httpx.get(
            base + "/models",
            headers={"Authorization": f"Bearer {cfg.get('llm.api_key', '')}"},
            timeout=10,
        )
        resp.raise_for_status()
        models = [{"name": str(m.get("id", ""))} for m in resp.json().get("data", [])]
        return models, None
    except Exception as exc:
        return None, str(exc)


def _stt_models() -> list[dict]:
    """Các thư mục model STT khả dụng trong data/models (có tokens.txt)."""
    root = Path("data/models")
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / "tokens.txt").exists():
            mb = sum(f.stat().st_size for f in d.glob("*.onnx")) / 1e6
            out.append({
                "dir": d.as_posix(),  # dạng tương đối, dùng luôn làm stt.model_dir
                "size_mb": round(mb, 1),
                "hotwords": (d / "hotwords.txt").exists(),
            })
    return out


def _tts_voices(cfg):
    url = str(cfg.get("tts.http_url", ""))
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return {}, "Chưa cấu hình dịch vụ TTS"
        endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path.rsplit("/", 1)[0] + "/voices", "", ""))
        response = httpx.get(endpoint, timeout=3)
        response.raise_for_status()
        return response.json(), None
    except Exception:
        return {}, "Không lấy được danh sách giọng từ dịch vụ TTS"


@router.get("/assistant/status")
def assistant_status(request: Request, _: None = Depends(require_admin)):
    app = request.app.state
    cfg = app.cfg
    pipeline = app.pipeline

    if cfg.get("llm.engine") == "openai":
        models, llm_error = _cloud_models(cfg)
    else:
        models, llm_error = _ollama_models(cfg)
    # Danh sách model local LUÔN trả kèm: đang cấu hình cloud mà người dùng
    # đổi select về Ollama trong UI phải thấy ngay model local (bẫy đã trả
    # giá: model list chỉ lấy theo engine hiện tại → dropdown local toàn
    # tên model cloud). Ngược lại, có api_key thì gợi ý cả list cloud.
    ollama_models, _ = _ollama_models(cfg)
    cloud_models, _ = (None, None)
    if str(cfg.get("llm.api_key", "")).strip():
        cloud_models, _ = _cloud_models(cfg)

    model_dir = Path(str(cfg.get("stt.model_dir", "data/models/stt")))
    if not model_dir.is_absolute():
        model_dir = Path.cwd() / model_dir
    onnx_files = sorted(f.name for f in model_dir.glob("*.onnx")) if model_dir.exists() else []

    tts_voices, tts_voices_error = _tts_voices(cfg)
    return {
        "llm": {
            "engine": cfg.get("llm.engine"),
            "base_url": cfg.get("llm.base_url"),
            "model": cfg.get("llm.model"),
            "api_key_set": bool(str(cfg.get("llm.api_key", "")).strip()),
            "fallback": bool(cfg.get("llm.fallback", True)),
            "local_model": cfg.get("llm.local_model", ""),
            "ollama_url": cfg.get("llm.ollama_url", ""),
            "keep_alive": str(cfg.get("llm.keep_alive", "")).strip() == "-1",
            "reachable": llm_error is None,
            "models": models or [],
            "ollama_models": ollama_models or [],
            "cloud_models": cloud_models or [],
            "error": llm_error,
            # friendly name of the currently running model, if any
            "loaded": next((m["name"] for m in (models or []) if m["name"] == cfg.get("llm.model")), None),
        },
        "stt": {
            "engine": cfg.get("stt.engine"),
            "model_dir": str(model_dir),
            "model_dir_rel": str(cfg.get("stt.model_dir", "")),
            "models": _stt_models(),
            "has_tokens": (model_dir / "tokens.txt").exists(),
            "onnx_files": onnx_files,
            "loaded": type(pipeline.stt).__name__ in ("SttSherpa", "SttQwen"),
            "available": _sherpa_importable(),
            "error": None if type(pipeline.stt).__name__ in ("SttSherpa", "SttQwen") else _stt_error_hint(cfg, model_dir),
            "qwen_context_file": cfg.get("stt.qwen_context_file", "data/qwen3-asr-context.md"),
        },
        "tts": {
            "engine": cfg.get("tts.engine"),
            "model": cfg.get("tts.model", "piper"),
            "voices": tts_voices,
            "voices_error": tts_voices_error,
            "piper_voice": cfg.get("tts.piper_voice", ""),
            "vieneu_voice": cfg.get("tts.vieneu_voice", "Mai Anh"),
            "http_url": cfg.get("tts.http_url", ""),
            "loaded": type(pipeline.tts).__name__ == "TtsHttp",
            "error": None if type(pipeline.tts).__name__ == "TtsHttp" else _tts_error_hint(cfg),
        },
    }


def _sherpa_importable() -> bool:
    try:
        import sherpa_onnx  # noqa: F401

        return True
    except ImportError:
        return False


def _stt_error_hint(cfg, model_dir) -> str:
    if cfg.get("stt.engine") == "none":
        return "Engine đang là 'none'"
    if cfg.get("stt.engine") == "qwen3_torch":
        return "Chưa cài hoặc không nạp được Qwen3-ASR-1.7B"
    if not _sherpa_importable():
        return "Chưa cài module sherpa-onnx: pip install -r optional-requirements.txt"
    if not (model_dir / "tokens.txt").exists():
        return f"Thiếu tokens.txt trong {model_dir}"
    return "Thiếu file encoder/decoder/joiner .onnx trong " + str(model_dir)


def _tts_error_hint(cfg) -> str:
    if cfg.get("tts.engine") == "none":
        return "Engine đang là 'none'"
    if not cfg.get("tts.http_url"):
        return "Engine 'http' nhưng chưa nhập http_url"
    return "Không kết nối được TTS service"


class AssistantConfigIn(BaseModel):
    llm_engine: str | None = None  # ollama | openai (OpenAI-compatible)
    llm_base_url: str | None = None
    llm_api_key: str | None = None  # không bao giờ đọc ngược lại; rỗng = giữ nguyên
    llm_fallback: bool | None = None  # engine openai lỗi -> chạy qua Ollama local
    llm_local_model: str | None = None  # model Ollama dùng khi fallback
    llm_ollama_url: str | None = None  # URL Ollama khi fallback (rỗng = 127.0.0.1:11434)
    llm_model: str | None = None
    llm_keep_alive: bool | None = None  # True = giữ model trong VRAM luôn
    stt_engine: str | None = None  # none | sherpa | qwen3_torch
    stt_model_dir: str | None = None  # thư mục model trong data/models (đổi là nạp lại ngay)
    stt_qwen_context_file: str | None = None
    tts_engine: str | None = None  # none | http
    tts_http_url: str | None = None
    tts_model: str | None = None
    tts_piper_voice: str | None = None
    tts_vieneu_voice: str | None = None


@router.post("/assistant/config")
def update_config(request: Request, body: AssistantConfigIn, _: None = Depends(require_admin)):
    app = request.app.state
    cfg = app.cfg
    changed = []
    old_stt = (cfg.get("stt.engine"), cfg.get("stt.model_dir"), cfg.get("stt.qwen_context_file", "data/qwen3-asr-context.md"))
    if body.stt_engine is not None and body.stt_engine not in ("none", "sherpa", "qwen3_torch"):
        raise HTTPException(status_code=400, detail="Engine STT không được hỗ trợ")
    if body.stt_engine == "qwen3_torch":
        from server.voice.stt import SttQwen
        try:
            SttQwen(cfg)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.llm_engine in ("ollama", "openai") and body.llm_engine != cfg.get("llm.engine"):
        new_engine = body.llm_engine
        old_model = str(cfg.get("llm.model", ""))
        if new_engine == "openai":
            # giữ lại model Ollama cũ làm model dự phòng khi fallback
            if not str(cfg.get("llm.local_model", "")).strip() and old_model:
                cfg._data.setdefault("llm", {})["local_model"] = old_model
                set_yaml_value(cfg.path, "llm", "local_model", old_model)
        elif str(cfg.get("llm.local_model", "")).strip():
            # về lại Ollama: phục hồi model local đã cất (llm.model đang là tên model cloud)
            restored = str(cfg.get("llm.local_model")).strip()
            cfg._data.setdefault("llm", {})["model"] = restored
            set_yaml_value(cfg.path, "llm", "model", restored)
            # xóa local_model: lần chuyển cloud sau cất đúng model đang dùng
            cfg._data["llm"]["local_model"] = ""
            set_yaml_value(cfg.path, "llm", "local_model", "")
            changed.append(f"llm.model = {restored} (phục hồi từ local_model)")
        cfg._data.setdefault("llm", {})["engine"] = new_engine
        set_yaml_value(cfg.path, "llm", "engine", new_engine)
        changed.append(f"llm.engine = {new_engine}")
    if body.llm_base_url:
        cfg._data.setdefault("llm", {})["base_url"] = body.llm_base_url
        set_yaml_value(cfg.path, "llm", "base_url", body.llm_base_url)
        changed.append(f"llm.base_url = {body.llm_base_url}")
    if body.llm_api_key:
        cfg._data.setdefault("llm", {})["api_key"] = body.llm_api_key
        set_yaml_value(cfg.path, "llm", "api_key", body.llm_api_key)
        changed.append("llm.api_key = ***")
    if body.llm_fallback is not None:
        cfg._data.setdefault("llm", {})["fallback"] = body.llm_fallback
        set_yaml_value(cfg.path, "llm", "fallback", body.llm_fallback)
        changed.append(f"llm.fallback = {body.llm_fallback} "
                       f"({'cloud lỗi tự chuyển Ollama local' if body.llm_fallback else 'báo lỗi thẳng'})")
    if body.llm_local_model:  # rỗng = giữ nguyên (engine-switch đã tự cất model cũ)
        cfg._data.setdefault("llm", {})["local_model"] = body.llm_local_model
        set_yaml_value(cfg.path, "llm", "local_model", body.llm_local_model)
        changed.append(f"llm.local_model = {body.llm_local_model}")
    if body.llm_ollama_url:  # rỗng = mặc định http://127.0.0.1:11434
        cfg._data.setdefault("llm", {})["ollama_url"] = body.llm_ollama_url
        set_yaml_value(cfg.path, "llm", "ollama_url", body.llm_ollama_url)
        changed.append(f"llm.ollama_url = {body.llm_ollama_url}")
    if body.llm_model:
        cfg._data.setdefault("llm", {})["model"] = body.llm_model
        set_yaml_value(cfg.path, "llm", "model", body.llm_model)
        changed.append(f"llm.model = {body.llm_model}")
    if body.llm_keep_alive is not None:
        value = "-1" if body.llm_keep_alive else "5m"
        cfg._data.setdefault("llm", {})["keep_alive"] = value
        set_yaml_value(cfg.path, "llm", "keep_alive", value)
        changed.append(
            f"llm.keep_alive = {value} ({'giữ trong VRAM luôn' if body.llm_keep_alive else 'tự dỡ sau 5 phút idle'})")
    if body.stt_engine in ("none", "sherpa", "qwen3_torch"):
        cfg._data.setdefault("stt", {})["engine"] = body.stt_engine
        set_yaml_value(cfg.path, "stt", "engine", body.stt_engine)
        changed.append(f"stt.engine = {body.stt_engine}")
    if body.stt_model_dir:
        if (Path(body.stt_model_dir) / "tokens.txt").exists():
            cfg._data.setdefault("stt", {})["model_dir"] = body.stt_model_dir
            set_yaml_value(cfg.path, "stt", "model_dir", body.stt_model_dir)
            changed.append(f"stt.model_dir = {body.stt_model_dir} (nạp lại ngay)")
        else:
            changed.append(f"bỏ qua stt.model_dir — không có model trong {body.stt_model_dir}")
    if body.stt_qwen_context_file is not None:
        value = body.stt_qwen_context_file.strip()
        cfg._data.setdefault("stt", {})["qwen_context_file"] = value
        set_yaml_value(cfg.path, "stt", "qwen_context_file", value)
        changed.append(f"stt.qwen_context_file = {value}")
    if body.tts_engine in ("none", "http"):
        cfg._data.setdefault("tts", {})["engine"] = body.tts_engine
        set_yaml_value(cfg.path, "tts", "engine", body.tts_engine)
        changed.append(f"tts.engine = {body.tts_engine}")
    if body.tts_http_url is not None:
        cfg._data.setdefault("tts", {})["http_url"] = body.tts_http_url
        set_yaml_value(cfg.path, "tts", "http_url", body.tts_http_url)
        changed.append(f"tts.http_url = {body.tts_http_url}")
    if body.tts_model in ("piper", "vieneu"):
        cfg._data.setdefault("tts", {})["model"] = body.tts_model
        set_yaml_value(cfg.path, "tts", "model", body.tts_model)
        changed.append(f"tts.model = {body.tts_model}")
    for model in ("piper", "vieneu"):
        value = getattr(body, f"tts_{model}_voice")
        if value is not None:
            cfg._data.setdefault("tts", {})[f"{model}_voice"] = value
            set_yaml_value(cfg.path, "tts", f"{model}_voice", value)
            changed.append(f"tts.{model}_voice = {value}")

    # rebuild the engines the pipeline uses (fall back to "none" on bad settings)
    new_stt = (cfg.get("stt.engine"), cfg.get("stt.model_dir"), cfg.get("stt.qwen_context_file", "data/qwen3-asr-context.md"))
    if old_stt != new_stt:
        try:
            previous = app.pipeline.stt
            app.pipeline.stt = make_stt(cfg)
            if hasattr(previous, "close"):
                previous.close()
        except Exception as exc:
            changed.append(f"stt load lỗi: {exc}; giữ engine đang chạy")
    if body.llm_keep_alive is not None and hasattr(app.pipeline.stt, "set_keep_alive"):
        app.pipeline.stt.set_keep_alive(body.llm_keep_alive)
    try:
        app.pipeline.tts = make_tts(cfg)
    except Exception:
        from server.voice.tts import TtsNone

        app.pipeline.tts = TtsNone()

    # ACK phải được tổng hợp lại sau khi thay engine/giọng.
    app.pipeline._ack_audio = None

    log_event(app.db, "assistant", "Cấu hình trợ lý: " + ("; ".join(changed) or "không đổi"))
    if body.llm_keep_alive is True:
        manager = getattr(app, "ollama_keepalive", None)
        if manager:
            manager.request_load()
    return {"ok": True, "changed": changed}


class TtsTestIn(BaseModel):
    text: str = "Đã tắt điều hòa phòng ngủ."


@router.post("/assistant/test-tts")
async def test_tts(request: Request, body: TtsTestIn, _: None = Depends(require_admin)):
    pipeline = request.app.state.pipeline
    started = time.perf_counter()
    try:
        audio = await asyncio.to_thread(pipeline.tts.synthesize, body.text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"TTS lỗi: {exc}")
    if not audio:
        raise HTTPException(
            status_code=400,
            detail="TTS đang ở chế độ 'none' - cấu hình engine 'http' trước (xem docs/setup-windows.md)",
        )
    data, rate = audio
    latency_ms = int((time.perf_counter() - started) * 1000)
    return Response(
        content=data,
        media_type="audio/wav",
        headers={"X-Latency-Ms": str(latency_ms), "X-Sample-Rate": str(rate)},
    )


class SttTestIn(BaseModel):
    text: str = "Hãy hẹn giờ 15 phút nữa nhắc tôi tắt bếp."


_PIPER_VOICE = None  # cache giữa các lần test — piper load model mất ~1s


def _piper_synth(text: str) -> bytes:
    """PCM16 16 kHz của câu text do piper đọc — test STT không cần mic thật."""
    global _PIPER_VOICE
    import piper

    if _PIPER_VOICE is None:
        models = sorted(Path("data/models/tts").glob("**/*.onnx"))
        if not models:
            raise RuntimeError("Không thấy model TTS trong data/models/tts (piper cần để sinh audio test)")
        _PIPER_VOICE = piper.PiperVoice.load(str(models[0]))
    try:
        from piper import PiperSynthesisConfig

        chunks = _PIPER_VOICE.synthesize(text, PiperSynthesisConfig(length_scale=1.0))
    except (ImportError, TypeError):
        chunks = _PIPER_VOICE.synthesize(text)
    pcm = b"".join(c.audio_int16_bytes for c in chunks)
    src = _PIPER_VOICE.config.sample_rate
    if src != 16000:
        import numpy as np

        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        n = int(len(x) * 16000 / src)
        pcm = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
        pcm = pcm.astype(np.int16).tobytes()
    return pcm


@router.post("/assistant/test-stt")
def test_stt(request: Request, body: SttTestIn, _: None = Depends(require_admin)):
    """Cho model STT đang nạp nghe lại câu do piper đọc (chính là đường
    stt_eval.py chạy tay) — so model nào nghe chuẩn hơn mà không cần mic."""
    pipeline = request.app.state.pipeline
    try:
        t0 = time.perf_counter()
        pcm = _piper_synth(body.text)
        synth_ms = int((time.perf_counter() - t0) * 1000)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Sinh audio test lỗi: {exc}")
    t0 = time.perf_counter()
    try:
        transcript = pipeline.stt.transcribe(pcm)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"STT lỗi: {exc}")
    stt_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "text": body.text,
        "transcript": transcript,
        "synth_ms": synth_ms,
        "stt_ms": stt_ms,
        "model_dir": str(request.app.state.cfg.get("stt.model_dir", "")),
    }


@router.post("/assistant/test-stt-mic")
async def test_stt_mic(request: Request, _: None = Depends(require_admin)):
    """Transcribe 16 kHz mono PCM16 only; never execute the transcript."""
    max_bytes = 16000 * 2 * 20
    pcm = bytearray()
    async for chunk in request.stream():
        pcm.extend(chunk)
        if len(pcm) > max_bytes:
            raise HTTPException(status_code=413, detail="Bản ghi quá 20 giây")
    if not pcm or len(pcm) % 2:
        raise HTTPException(status_code=400, detail="Âm thanh PCM16 không hợp lệ")
    from server.voice.pipeline import _has_speech_energy
    if not _has_speech_energy(bytes(pcm)):
        return {"transcript": "", "stt_ms": 0, "message": "Không nghe thấy giọng nói"}
    start = time.perf_counter()
    try:
        transcript = await asyncio.to_thread(request.app.state.pipeline.stt.transcribe, bytes(pcm))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"STT lỗi: {exc}") from exc
    return {"transcript": transcript, "stt_ms": int((time.perf_counter() - start) * 1000)}


class AnnounceIn(BaseModel):
    room_id: int
    text: str = "Đây là giọng đọc thử của trợ lý nhà thông minh."


@router.post("/assistant/announce")
async def announce(request: Request, body: AnnounceIn, _: None = Depends(require_admin)):
    """Speak through every phone in a room - tests the exact path timers use."""
    app = request.app.state
    sent = await app.voice_connections.announce_room(body.room_id, body.text, app.pipeline)
    return {"sent": sent}
