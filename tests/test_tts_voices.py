import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from scripts import tts_http_example as sidecar
from server.voice.tts import TtsHttp


def test_preview_uses_selected_voice_without_changing_saved_voice():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.api.assistant import router
    from server.api.deps import require_admin

    values = {"tts.http_url": "http://localhost:8125/tts", "tts.model": "vieneu", "tts.vieneu_voice": "Mai Anh"}
    cfg = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
    engine = TtsHttp(cfg)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: None
    app.state.pipeline = SimpleNamespace(tts=engine)
    response = httpx.Response(200, headers={"X-TTS-Model": "vieneu"}, content=b"wav", request=httpx.Request("POST", values["tts.http_url"]))
    with patch("server.voice.tts.httpx.post", return_value=response) as post:
        result = TestClient(app).post("/assistant/test-tts", json={"text": "Xin chào", "model": "vieneu", "voice": "Quốc Tuấn"})
    assert result.status_code == 200
    assert post.call_args.kwargs["json"]["voice"] == "Quốc Tuấn"
    assert engine.voice == "Mai Anh"


def test_voice_catalog_lists_installed_presets_and_piper_speakers(tmp_path, monkeypatch):
    package = tmp_path / "vieneu"
    (package / "assets").mkdir(parents=True)
    (package / "assets/voices_v3_turbo.json").write_text(
        json.dumps({"presets": {"Mai Anh": {"description": "Giọng nữ"}, "An": {}}}), encoding="utf-8")
    model = tmp_path / "data/models/tts/example/model.onnx"
    model.parent.mkdir(parents=True)
    model.touch()
    model.with_suffix(".onnx.json").write_text(json.dumps({"speaker_id_map": {"Alice": 0, "Bob": 1}}))
    monkeypatch.chdir(tmp_path)
    with patch.object(sidecar.importlib.util, "find_spec", return_value=SimpleNamespace(submodule_search_locations=[str(package)])):
        voices = sidecar.available_voices()
    assert [v["id"] for v in voices["vieneu"]] == ["Mai Anh", "An"]
    assert [v["id"] for v in voices["piper"]] == ["example/model.onnx#0", "example/model.onnx#1"]


def test_selected_voice_is_sent_to_tts_service():
    values = {"tts.http_url": "http://localhost:8125/tts", "tts.model": "vieneu", "tts.vieneu_voice": "An"}
    cfg = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
    response = httpx.Response(200, headers={"X-TTS-Model": "vieneu"}, content=b"wav", request=httpx.Request("POST", values["tts.http_url"]))
    with patch("server.voice.tts.httpx.post", return_value=response) as post:
        assert TtsHttp(cfg)._one("xin chào") == b"wav"
    assert post.call_args.kwargs["json"]["voice"] == "An"


def test_piper_rejects_unknown_voice(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with patch.object(sidecar.importlib.util, "find_spec", return_value=None):
        with pytest.raises(ValueError, match="không tồn tại"):
            sidecar.synthesize("xin chào", voice="../outside.onnx")
