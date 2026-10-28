"""Voice pipeline: audio -> STT -> command handling -> TTS, shared by WS and scheduler."""
from __future__ import annotations

import asyncio

from server.config import Config
from server.core.text_command import handle_command
from server.db import Database
from server.voice.stt import make_stt
from server.voice.tts import make_tts


class VoicePipeline:
    def __init__(self, cfg: Config, db: Database, drivers, weather):
        self.cfg = cfg
        self.db = db
        self.drivers = drivers
        self.weather = weather
        self.stt = make_stt(cfg)
        self.tts = make_tts(cfg)

    async def process_audio(self, pcm16: bytes, room_id) -> dict:
        """16 kHz mono PCM16 -> {transcript, reply, actions, engine}."""
        try:
            transcript = await asyncio.to_thread(self.stt.transcribe, pcm16)
        except Exception as exc:
            return {"transcript": "", "reply": str(exc), "actions": [], "engine": "stt-error"}
        if not transcript.strip():
            return {
                "transcript": "",
                "reply": "Mình không nghe rõ, bạn nói lại nhé.",
                "actions": [],
                "engine": "stt",
            }
        result = await handle_command(self.db, self.drivers, self.cfg, self.weather, transcript, room_id)
        result["transcript"] = transcript
        return result

    async def tts_audio(self, text: str) -> tuple[bytes, int] | None:
        try:
            return await asyncio.to_thread(self.tts.synthesize, text)
        except Exception as exc:
            from server.db import log_event

            log_event(self.db, "tts", f"TTS lỗi: {exc}", level="warn")
            return None
