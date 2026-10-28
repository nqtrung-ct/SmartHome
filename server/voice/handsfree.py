"""Hands-free listening state machine for one voice connection.

The client streams mic audio continuously; the listener wakes on
"trợ lý ơi" and ends the utterance on the "over" keyword (detected by
re-running STT on the growing buffer) or after `silence_ms` of quiet,
whichever comes first.
"""
from __future__ import annotations

import asyncio
import re
import time

from server.util import normalize
from server.voice.pipeline import _has_speech_energy

# STT renders "trợ lý ơi" variously: "trợ lý ơi", "cho ý ơi", "cho lý ơi"...
WAKE_PREFIX = re.compile(r"^(?:\S+\s+)?(?:hey |ei )?(?:tro|cho)\s*(?:ly|li|y|uy)\s*oi\b")
# ...and the end keyword even more so; all anchored at the very end of the text
END_KEYWORD = re.compile(
    r"\s*(?:over|o vo|o ve|ket thuc|voi toi|voi tup|tup|vet|co hoi|co a|co chu ky)[\s.?!]*$"
)

PREROLL_S = 0.6            # kept before speech starts so the wake word isn't clipped
MIN_UTTERANCE_S = 1.0
MAX_UTTERANCE_S = 20.0
PARTIAL_INTERVAL_S = 0.5   # how often STT re-runs on the buffer while capturing
TAIL_KEEP_S = 0.5          # quiet tail kept when finalizing on silence
BYTES_PER_SEC = 32_000     # 16 kHz mono PCM16


class HandsFreeListener:
    def __init__(self, stt, silence_ms: int = 3000):
        self._stt = stt
        self.silence_s = max(1.0, silence_ms) / 1000
        self.reset()

    def reset(self) -> None:
        self._preroll = bytearray()
        self.utterance = bytearray()
        self._capturing = False
        self._head = 0        # preroll bytes at the front of the utterance
        self._t0 = 0.0
        self._last_speech = 0.0
        self._keyword_hit = False
        self._next_partial = 0.0
        self._checker: asyncio.Task | None = None

    async def stop(self) -> None:
        if self._checker:
            self._checker.cancel()
            self._checker = None

    async def feed(self, pcm: bytes) -> tuple[str, bytes] | None:
        """One PCM16 chunk in; (end_reason, utterance_pcm) when complete."""
        now = time.monotonic()
        if not self._capturing:
            if not _has_speech_energy(pcm):
                self._preroll.extend(pcm)
                del self._preroll[:-int(PREROLL_S * BYTES_PER_SEC)]
                return None
            self._capturing = True
            self._head = len(self._preroll)
            self.utterance = bytearray(self._preroll) + pcm
            self._preroll = bytearray()
            self._t0 = now
            self._last_speech = now
            self._next_partial = now + MIN_UTTERANCE_S
            self._checker = asyncio.create_task(self._keyword_checker())
            return None

        self.utterance.extend(pcm)
        if _has_speech_energy(pcm):
            self._last_speech = now
        dur = len(self.utterance) / BYTES_PER_SEC
        if self._keyword_hit:
            return await self._finish("keyword")
        if now - self._last_speech >= self.silence_s and dur >= MIN_UTTERANCE_S:
            return await self._finish("silence")
        if dur >= MAX_UTTERANCE_S:
            return await self._finish("maxlen")
        return None

    async def _keyword_checker(self) -> None:
        """Re-transcribe the buffer now and then; flag when it ends with the keyword."""
        try:
            while True:
                await asyncio.sleep(0.1)
                if self._keyword_hit or time.monotonic() < self._next_partial:
                    continue
                self._next_partial = time.monotonic() + PARTIAL_INTERVAL_S
                pcm = bytes(self.utterance)
                dur = len(pcm) / BYTES_PER_SEC
                if dur < MIN_UTTERANCE_S or dur >= MAX_UTTERANCE_S:
                    continue
                try:
                    text = await asyncio.to_thread(self._stt.transcribe, pcm)
                except Exception:
                    continue
                if END_KEYWORD.search(normalize(text)):
                    self._keyword_hit = True
        except asyncio.CancelledError:
            pass

    async def _finish(self, reason: str) -> tuple[str, bytes]:
        await self.stop()
        if reason == "silence":  # drop the trailing quiet, keep a short tail
            keep = self._head + int(
                (self._last_speech - self._t0 + TAIL_KEEP_S) * BYTES_PER_SEC
            )
            keep &= ~1  # float rounding can land odd: keep PCM16 sample-aligned
            del self.utterance[max(keep, BYTES_PER_SEC // 2):]
        pcm = bytes(self.utterance)
        self.reset()
        return reason, pcm


def split_wake_word(transcript: str) -> str | None:
    """Normalized command with wake word and end keyword removed, or None when
    the transcript doesn't start with the wake word."""
    norm = normalize(transcript)
    m = WAKE_PREFIX.match(norm)
    if not m:
        return None
    command = END_KEYWORD.sub("", norm[m.end():])
    return command.strip(" .,?!")
