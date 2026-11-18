import io
import wave
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api import command_api
from server.api.deps import require_admin
from server.db import Database, save_stt_recording


def test_recent_recordings_are_bounded_and_audio_is_protected(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    app = FastAPI()
    app.state.db = db
    app.state.cfg = {"server.admin_token": "test-admin"}
    app.include_router(command_api.router, prefix="/api")
    client = TestClient(app)
    pcm = b"\x01\x00\xff\xff"
    try:
        for i in range(6):
            save_stt_recording(db, "room", "qwen3_torch", f"lệnh {i}", pcm)
        assert client.get("/api/stt-recordings").status_code in (401, 403)
        assert client.get("/api/stt-recordings/6/audio").status_code in (401, 403)
        app.dependency_overrides[require_admin] = lambda: None
        rows = client.get("/api/stt-recordings").json()
        assert [r["transcript"] for r in rows] == [f"lệnh {i}" for i in range(5, 0, -1)]
        assert client.get("/api/stt-recordings/1/audio").status_code == 404
        response = client.get(f"/api/stt-recordings/{rows[0]['id']}/audio")
        assert response.status_code == 200
        with wave.open(io.BytesIO(response.content)) as recording:
            assert (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()) == (1, 2, 16000)
            assert recording.readframes(recording.getnframes()) == pcm
    finally:
        client.close()
        db.close()
