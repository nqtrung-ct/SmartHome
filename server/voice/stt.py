"""Speech-to-text. sherpa-onnx Zipformer-vi when installed; "none" otherwise."""
from __future__ import annotations

import threading
from pathlib import Path

from server.config import Config


class SttNone:
    """Placeholder so the pipeline can run (and say so) without models."""

    def transcribe(self, pcm16: bytes) -> str:
        raise RuntimeError(
            "Chưa cài mô hình nhận dạng giọng nói. "
            "Cài sherpa-onnx và đặt model Zipformer-vi vào data/models/stt (xem docs/setup-windows.md)."
        )


def _resolve_hotwords(cfg: Config, model_dir: Path) -> Path | None:
    """File hotwords (mỗi dòng một từ/cụm muốn ưu tiên lúc nhận dạng). Đường
    dẫn tương đối tính từ model_dir (nếu có) hoặc cwd; thiếu file -> None."""
    raw = str(cfg.get("stt.hotwords_file", "") or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        in_model = model_dir / p
        p = in_model if in_model.exists() else Path.cwd() / p
    return p if p.exists() else None


def _bpe_setup(hw: Path, model_dir: Path) -> tuple[Path, Path | None]:
    """Khi modeling_unit="bpe", sherpa-onnx muốn (a) tham số bpe_vocab = file
    vocab 2 cột piece/điểm-log và (b) hotwords là TỪ nguyên để nó tự tách piece
    lúc encode. Dùng bpe.model cạnh model + sentencepiece để sinh cả hai; thiếu
    một trong hai thì trả (file gốc, None)."""
    bpe = model_dir / "bpe.model"
    tokens_file = model_dir / "tokens.txt"
    if not bpe.exists() or not tokens_file.exists():
        return hw, None
    try:
        import sentencepiece as spm
    except ImportError:
        return hw, None
    sp = spm.SentencePieceProcessor(model_file=str(bpe))
    vocab = {ln.split()[0] for ln in tokens_file.read_text(encoding="utf-8").splitlines() if ln}
    words = []
    for line in hw.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # model Zipformer-vi học chữ HOA ("▁HẸN") nên thử nguyên văn / HOA /
        # thường — bản nào encode ra piece có đủ trong tokens.txt thì dùng
        for cand in (line, line.upper(), line.lower()):
            enc = sp.encode(cand, out_type=str)
            if enc and all(p in vocab for p in enc):
                words.append(cand)
                break
    if not words:
        return hw, None
    vocab_out = hw.with_name(hw.stem + ".bpe.vocab")
    with vocab_out.open("w", encoding="utf-8") as f:
        for i in range(sp.get_piece_size()):
            f.write(f"{sp.id_to_piece(i)}\t{sp.get_score(i)}\n")
    words_out = hw.with_name(hw.stem + ".bpe.txt")
    words_out.write_text("\n".join(words) + "\n", encoding="utf-8")
    return words_out, vocab_out


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
        # Hotwords (contextual biasing): cộng điểm cho từ vựng nhà thông minh
        # lúc decode — sửa tận gốc lỗi nghe 'hẹn' thành 'hiện'. Chỉ có tác dụng
        # với modified_beam_search nên bật tự động khi có hotwords và config
        # chưa chỉ định thuật toán khác.
        hotwords = _resolve_hotwords(cfg, model_dir)
        method = str(cfg.get("stt.decoding_method", "") or "")
        if hotwords:
            hw_file, bpe_vocab = _bpe_setup(hotwords, model_dir)
            kwargs["hotwords_file"] = str(hw_file)
            kwargs["hotwords_score"] = float(cfg.get("stt.hotwords_score", 2.0))
            kwargs["max_active_paths"] = int(cfg.get("stt.max_active_paths", 4))
            if bpe_vocab:
                # mặc định "cjkchar" (tiếng Trung) băm mỗi từ thành ký tự lẻ;
                # model BPE cần "bpe" + file vocab 2 cột, thiếu bpe_vocab thì
                # segfault trong C++ (con trỏ encoder null)
                kwargs["modeling_unit"] = "bpe"
                kwargs["bpe_vocab"] = str(bpe_vocab)
            method = method or "modified_beam_search"
        kwargs["decoding_method"] = method or "greedy_search"
        recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(**kwargs)
        self._recognizer = recognizer
        self._new_api = hasattr(recognizer, "create_stream")
        self._lock = threading.Lock()  # hands-free runs STT from two tasks at once

    def transcribe(self, pcm16: bytes) -> str:
        import numpy as np

        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return ""
        with self._lock:
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
