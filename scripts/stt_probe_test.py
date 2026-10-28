"""Transcribe the model's bundled Vietnamese test wavs through our STT engine."""
import sys
import time
import wave
from pathlib import Path

from server.config import Config
from server.voice.stt import make_stt

wavs_dir = Path(sys.argv[1])
stt = make_stt(Config())
for name in sorted(wavs_dir.glob("*.wav")):
    with wave.open(str(name)) as w:
        pcm = w.readframes(w.getnframes())
    t0 = time.perf_counter()
    print(f"{name.name} -> {stt.transcribe(pcm)!r} ({(time.perf_counter() - t0) * 1000:.0f} ms)")
