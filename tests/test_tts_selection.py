"""Model switching must not silently play Piper when VieNeu was requested."""
import struct
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from scripts import tts_http_example as sidecar
from server.voice.tts import TtsHttp


class Config:
    def __init__(self, model):
        self.model = model

    def get(self, key, default=None):
        return {"tts.model": self.model, "tts.http_url": "http://localhost:8125/tts"}.get(key, default)


def test_old_sidecar_cannot_silently_return_piper_for_vieneu():
    response = httpx.Response(200, content=b"old piper", request=httpx.Request("POST", "http://localhost"))
    with patch("server.voice.tts.httpx.post", return_value=response):
        with pytest.raises(RuntimeError, match="chưa hỗ trợ VieNeu"):
            TtsHttp(Config("vieneu")).synthesize("Xin chào")


def test_sidecar_selects_vieneu_and_returns_pcm_wav():
    import numpy as np

    class FakeVieNeu:
        def infer(self, text, voice):
            assert voice == "Mai Anh"
            return np.array([0.0, 0.5, -0.5], dtype=np.float32)

    with patch.object(sidecar, "_vieneu", FakeVieNeu()):
        response = TestClient(sidecar.app).post("/tts", json={"text": "Xin chào", "model": "vieneu"})
    assert response.status_code == 200
    assert response.headers["x-tts-model"] == "vieneu"
    assert response.content[:4] == b"RIFF"
    assert struct.unpack_from("<I", response.content, 24)[0] == 48000
    assert len(response.content) > 44


def test_sidecar_defaults_to_piper():
    with patch.object(sidecar, "synthesize", return_value=b"piper"):
        response = TestClient(sidecar.app).post("/tts", json={"text": "Xin chào"})
    assert response.headers["x-tts-model"] == "piper"
    assert response.content == b"piper"
