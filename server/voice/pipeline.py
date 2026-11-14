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
        self._ack_audio: tuple[bytes, int] | None = None  # câu ACK cố định, tổng hợp 1 lần
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

    async def process_audio(self, pcm16: bytes, room_id, skip_gate: bool = False,
                            on_ack=None, stt_only: bool = False) -> dict:
        """16 kHz mono PCM16 -> {transcript, reply, actions, engine}.

        Lệnh chậm (quay camera đo, LLM tool-calls) thì gọi `on_ack` sau
        `voice.ack_after` giây để máy nói "Đang kiểm tra" — người dùng biết
        lệnh đã nhận, không tưởng máy chết. `stt_only` chỉ nghe chữ (hands-free
        gọi handle_command riêng sau khi tách từ đánh thức — chạy lệnh ở đây
        nữa thì mọi lệnh PTZ/classification bị thi hành 2 lần)."""
        # energy gate: near-silent audio makes small STT models hallucinate
        # single words ("Đúng") and then waste a slow LLM round-trip
        if not skip_gate and not _has_speech_energy(pcm16):
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
        if stt_only:
            return {"transcript": transcript, "reply": "", "actions": [], "engine": "stt"}
        task = asyncio.create_task(
            handle_command(self.db, self.drivers, self.cfg, self.weather, transcript, room_id,
                           app=getattr(self, "app", None))
        )
        done, _ = await asyncio.wait({task}, timeout=float(self.cfg.get("voice.ack_after", 1.2)))
        if not done and on_ack:
            try:
                await on_ack()
            except Exception:
                pass  # ACK chỉ là nuột lời — lỗi chỗ khác không được ăn mất câu trả lời
        result = await task
        result["transcript"] = transcript
        return result

    async def ack_audio(self) -> tuple[bytes, int] | None:
        """Audio câu ACK (voice.ack_text) — synthesize đúng 1 lần rồi dùng lại."""
        if self._ack_audio is None:
            try:
                self._ack_audio = await self.tts_audio(self.cfg.get("voice.ack_text", "Đang kiểm tra"))
            except Exception:
                return None
        return self._ack_audio

    async def tts_audio(self, text: str) -> tuple[bytes, int] | None:
        try:
            return await asyncio.to_thread(self.tts.synthesize, text)
        except Exception as exc:
            from server.db import log_event

            log_event(self.db, "tts", f"TTS lỗi: {exc}", level="warn")
            return None
