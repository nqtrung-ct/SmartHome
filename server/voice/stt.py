"""Speech-to-text. sherpa-onnx Zipformer-vi when installed; "none" otherwise."""
from __future__ import annotations

from pathlib import Path

from server.config import Config


class SttNone:
    """Placeholder so the pipeline can run (and say so) without models."""

    def transcribe(self, pcm16: bytes) -> str:
        raise RuntimeError(
            "Chưa cài mô hình nhận dạng giọng nói. "
            "Cài sherpa-onnx và đặt model Zipformer-vi vào data/models/stt (xem docs/setup-windows.md)."
        )


class SttSherpa:
    def __init__(self, cfg: Config):
        import sherpa_onnx  # optional dependency

        model_dir = Path(cfg.get("stt.model_dir", "data/models/stt"))
        if not model_dir.is_absolute():
            model_dir = Path.cwd() / model_dir
        tokens = model_dir / "tokens.txt"
        if not tokens.exists():
            raise RuntimeError(f"Không thấy {tokens}. Đặt model Zipformer-vi vào {model_dir}.")
        onnx = sorted(model_dir.glob("*.onnx"))
        names = {p.name for p in onnx}

        kwargs = {
            "tokens": str(tokens),
            "num_threads": int(cfg.get("stt.num_threads", 2)),
            "sample_rate": 16000,
            "feature_dim": 80,
            "debug": False,
        }
        if "encoder-epoch-99-avg-1.onnx" in names or any("encoder" in n for n in names):
            kwargs.update(
                encoder=str(model_dir / "encoder-epoch-99-avg-1.onnx")
                if (model_dir / "encoder-epoch-99-avg-1.onnx").exists()
                else str(next(p for p in onnx if "encoder" in p.name)),
                decoder=str(next(p for p in onnx if "decoder" in p.name)),
                joiner=str(next(p for p in onnx if "joiner" in p.name)),
            )
        recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(**kwargs)
        self._recognizer = recognizer
        self._new_api = hasattr(recognizer, "create_stream")

    def transcribe(self, pcm16: bytes) -> str:
        import numpy as np

        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return ""
        if self._new_api:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(16000, samples)
            self._recognizer.decode_stream(stream)
            return stream.result.text.strip()
        self._recognizer.accept_waveform(16000, samples)
        self._recognizer.decode()
        return self._recognizer.get_result().strip()


def make_stt(cfg: Config):
    engine = cfg.get("stt.engine", "none")
    if engine == "sherpa":
        return SttSherpa(cfg)
    return SttNone()
