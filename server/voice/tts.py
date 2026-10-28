"""Text-to-speech engines: "none" (text only) or "http" (POST text -> wav bytes).

Wrap VieNeu-TTS (or any generator) in a tiny HTTP service and point
tts.http_url at it - see scripts/tts_http_example.py.
"""
from __future__ import annotations

import struct

import httpx

from server.config import Config


def wav_sample_rate(wav: bytes) -> int:
    """Parse the sample rate out of a RIFF header; 16000 if unparseable."""
    if len(wav) >= 28 and wav[:4] == b"RIFF" and wav[8:12] == b"WAVE":
        return struct.unpack_from("<I", wav, 24)[0]
    return 16000


class TtsNone:
    def synthesize(self, text: str) -> tuple[bytes, int] | None:
        return None


class TtsHttp:
    def __init__(self, cfg: Config):
        self.url = cfg.get("tts.http_url", "")

    def synthesize(self, text: str) -> tuple[bytes, int] | None:
        resp = httpx.post(self.url, json={"text": text}, timeout=60)
        resp.raise_for_status()
        audio = resp.content
        return audio, wav_sample_rate(audio)


def make_tts(cfg: Config):
    engine = cfg.get("tts.engine", "none")
    if engine == "http" and cfg.get("tts.http_url"):
        return TtsHttp(cfg)
    return TtsNone()
