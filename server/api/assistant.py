"""Trợ lý tab: AI model status, config persistence, TTS/voice testing."""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

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
    base = str(cfg.get("llm.base_url", "http://127.0.0.1:11434")).rstrip("/")
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


@router.get("/assistant/status")
def assistant_status(request: Request, _: None = Depends(require_admin)):
    app = request.app.state
    cfg = app.cfg
    pipeline = app.pipeline

    if cfg.get("llm.engine") == "openai":
        models, llm_error = _cloud_models(cfg)
    else:
        models, llm_error = _ollama_models(cfg)

    model_dir = Path(str(cfg.get("stt.model_dir", "data/models/stt")))
    if not model_dir.is_absolute():
        model_dir = Path.cwd() / model_dir
    onnx_files = sorted(f.name for f in model_dir.glob("*.onnx")) if model_dir.exists() else []

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
            "error": llm_error,
            # friendly name of the currently running model, if any
            "loaded": next((m["name"] for m in (models or []) if m["name"] == cfg.get("llm.model")), None),
        },
        "stt": {
            "engine": cfg.get("stt.engine"),
            "model_dir": str(model_dir),
            "has_tokens": (model_dir / "tokens.txt").exists(),
            "onnx_files": onnx_files,
            "loaded": type(pipeline.stt).__name__ == "SttSherpa",
            "available": _sherpa_importable(),
            "error": None if type(pipeline.stt).__name__ == "SttSherpa" else _stt_error_hint(cfg, model_dir),
        },
        "tts": {
            "engine": cfg.get("tts.engine"),
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
    stt_engine: str | None = None  # none | sherpa
    tts_engine: str | None = None  # none | http
    tts_http_url: str | None = None


@router.post("/assistant/config")
def update_config(request: Request, body: AssistantConfigIn, _: None = Depends(require_admin)):
    app = request.app.state
    cfg = app.cfg
    changed = []

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
    if body.stt_engine in ("none", "sherpa"):
        cfg._data.setdefault("stt", {})["engine"] = body.stt_engine
        set_yaml_value(cfg.path, "stt", "engine", body.stt_engine)
        changed.append(f"stt.engine = {body.stt_engine}")
    if body.tts_engine in ("none", "http"):
        cfg._data.setdefault("tts", {})["engine"] = body.tts_engine
        set_yaml_value(cfg.path, "tts", "engine", body.tts_engine)
        changed.append(f"tts.engine = {body.tts_engine}")
    if body.tts_http_url is not None:
        cfg._data.setdefault("tts", {})["http_url"] = body.tts_http_url
        set_yaml_value(cfg.path, "tts", "http_url", body.tts_http_url)
        changed.append(f"tts.http_url = {body.tts_http_url}")

    # rebuild the engines the pipeline uses (fall back to "none" on bad settings)
    try:
        app.pipeline.stt = make_stt(cfg)
    except Exception as exc:
        from server.voice.stt import SttNone

        app.pipeline.stt = SttNone()
        changed.append(f"stt load lỗi: {exc}")
    try:
        app.pipeline.tts = make_tts(cfg)
    except Exception:
        from server.voice.tts import TtsNone

        app.pipeline.tts = TtsNone()

    log_event(app.db, "assistant", "Cấu hình trợ lý: " + ("; ".join(changed) or "không đổi"))
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


class AnnounceIn(BaseModel):
    room_id: int
    text: str = "Đây là giọng đọc thử của trợ lý nhà thông minh."


@router.post("/assistant/announce")
async def announce(request: Request, body: AnnounceIn, _: None = Depends(require_admin)):
    """Speak through every phone in a room - tests the exact path timers use."""
    app = request.app.state
    sent = await app.voice_connections.announce_room(body.room_id, body.text, app.pipeline)
    return {"sent": sent}
