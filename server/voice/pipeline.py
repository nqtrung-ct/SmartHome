"""Voice pipeline: audio -> STT -> command handling -> TTS, shared by WS and scheduler."""
from __future__ import annotations

import asyncio

from server.config import Config
from server.core.text_command import handle_command
from server.db import Database, log_event
from server.voice.stt import SttNone, make_stt
from server.voice.tts import TtsNone, make_tts

SILENCE_PEAK = 400  # int16 peak below this on every sampled frame = no speech


def _has_speech_energy(pcm16: bytes) -> bool:
    if not pcm16:
        return False
    samples = memoryview(pcm16).cast("h")
    peak = max(abs(s) for s in samples[::16])  # subsample: fast enough for a minute of audio
    return peak >= SILENCE_PEAK


class VoicePipeline:
    def __init__(self, cfg: Config, db: Database, drivers, weather):
        self.cfg = cfg
        self.db = db
        self.drivers = drivers
        self.weather = weather
        # a broken engine must never take the whole server down
        try:
            self.stt = make_stt(cfg)
        except Exception as exc:
            log_event(db, "stt", f"Không nạp được STT (dùng none): {exc}", level="warn")
            self.stt = SttNone()
        try:
            self.tts = make_tts(cfg)
        except Exception as exc:
            log_event(db, "tts", f"Không nạp được TTS (dùng none): {exc}", level="warn")
            self.tts = TtsNone()

    async def process_audio(self, pcm16: bytes, room_id) -> dict:
        """16 kHz mono PCM16 -> {transcript, reply, actions, engine}."""
        # energy gate: near-silent audio makes small STT models hallucinate
        # single words ("Đúng") and then waste a slow LLM round-trip
        if not _has_speech_energy(pcm16):
            return {
                "transcript": "",
                "reply": "Mình không nghe rõ, bạn nói lại nhé.",
                "actions": [],
                "engine": "vad",
            }
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
