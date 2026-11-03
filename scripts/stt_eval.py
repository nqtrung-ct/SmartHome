"""So sánh chất lượng các bản model STT trên cùng một bộ audio.

    .venv\\Scripts\\python.exe -X utf8 scripts\\stt_eval.py
    .venv\\Scripts\\python.exe -X utf8 scripts\\stt_eval.py -p "hãy hẹn giờ 15 phút nữa"
    .venv\\Scripts\\python.exe -X utf8 scripts\\stt_eval.py -w audio1.wav audio2.wav

Không có -w/-p thì dùng bộ câu mẫu, sinh audio bằng piper (giọng TTS của server)
rồi resample về 16 kHz. Giọng tổng hợp không thay thế micro thật nhưng đủ để so
sánh tương đối giữa các model khi chỉnh hotwords / đổi model.
"""
from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.voice.stt import SttSherpa

# các bản model sẽ so sánh: nhãn -> thư mục (thiếu thư mục thì bỏ qua)
MODELS = {
    "cũ 30M/6k giờ (data/models/stt)": "data/models/stt",
    "mới 70k giờ + hotwords (data/models/stt-vi70k)": "data/models/stt-vi70k",
    "gipformer-68M-rnnt int8 (data/models/stt-gipformer)": "data/models/stt-gipformer",
}

PHRASES = [
    "Hãy hẹn giờ 15 phút nữa.",
    "Hẹn giờ 30 phút nữa nhắc tôi tắt bếp.",
    "Bảy giờ tối tắt điều hòa.",
    "Hủy hẹn giờ.",
    "Còn hẹn giờ nào đang chờ không?",
    "Hiện giờ là mấy giờ?",
    "Bật đèn phòng khách.",
    "Tắt điều hòa phòng ngủ.",
    "Quay camera về phía bếp.",
    "Trợ lý ơi, tăng âm lượng TV.",
]


class _Cfg:
    """Config giả chỉ chứa các khóa stt.* mà SttSherpa đọc."""

    def __init__(self, **kv):
        self.kv = kv

    def get(self, key, default=None):
        return self.kv.get(key, default)


def synth(text: str, length_scale: float = 1.0) -> bytes:
    """PCM16 16 kHz của câu text do piper đọc."""
    import piper

    models = sorted(Path("data/models/tts").glob("**/*.onnx"))
    if not models:
        raise SystemExit("Không thấy model TTS trong data/models/tts.")
    voice = piper.PiperVoice.load(str(models[0]))
    try:
        from piper import PiperSynthesisConfig

        chunks = voice.synthesize(text, PiperSynthesisConfig(length_scale=length_scale))
    except (ImportError, TypeError):
        chunks = voice.synthesize(text)
    pcm = b"".join(c.audio_int16_bytes for c in chunks)
    return resample(pcm, voice.config.sample_rate, 16000)


def resample(pcm: bytes, src: int, dst: int) -> bytes:
    if src == dst:
        return pcm
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = int(len(x) * dst / src)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return y.astype(np.int16).tobytes()


def wav_pcm16_16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        pcm = w.readframes(w.getnframes())
        return resample(pcm, w.getframerate(), 16000)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-w", "--wav", nargs="*", type=Path, default=[],
                    help="file .wav để test (16k hay không đều được)")
    ap.add_argument("-p", "--phrase", nargs="*", default=[],
                    help="câu cần piper đọc rồi test (mặc định: bộ PHRASES)")
    args = ap.parse_args()

    items = [(f"{p.name}", wav_pcm16_16k(p)) for p in args.wav]
    for phrase in args.phrase or (PHRASES if not args.wav else []):
        items.append((phrase, synth(phrase)))
    if not items:
        raise SystemExit("Không có gì để test.")

    for label, model_dir in MODELS.items():
        if not (Path(model_dir) / "tokens.txt").exists():
            print(f"\n=== {label}: BỎ QUA (không có model) ===")
            continue
        stt = SttSherpa(_Cfg(**{"stt.model_dir": model_dir, "stt.num_threads": 2,
                                 "stt.hotwords_file": "hotwords.txt"}))
        print(f"\n=== {label} ===")
        for name, pcm in items:
            t0 = time.perf_counter()
            text = stt.transcribe(pcm)
            ms = (time.perf_counter() - t0) * 1000
            print(f"  [{ms:6.0f} ms] {name} -> {text!r}")


if __name__ == "__main__":
    main()
