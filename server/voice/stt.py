"""Speech-to-text. sherpa-onnx Zipformer-vi when installed; "none" otherwise."""
from __future__ import annotations

import atexit
import base64
import io
import json
import queue
import subprocess
import sys
import threading
import wave
from pathlib import Path

import httpx

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


class SttQwen:
    """Qwen3-ASR in a separate Windows CUDA environment."""

    def __init__(self, cfg: Config):
        self.model = Path(cfg.get("stt.qwen_torch_model_dir", "data/models/Qwen3-ASR-1.7B"))
        self.python = Path(cfg.get("stt.qwen_torch_python", "data/qwen3-asr-venv/Scripts/python.exe"))
        self.context = Path(cfg.get("stt.qwen_context_file", "data/qwen3-asr-context.md"))
        if not self.python.is_file() or not (self.model / "config.json").is_file() or not list(self.model.glob("*.safetensors")):
            raise RuntimeError("Chưa cài môi trường hoặc model Qwen3-ASR-1.7B trên máy này")
        self._lock = threading.RLock()
        self._proc = None
        self._lines = None
        self._keep_alive = False
        self._idle_timer = None
        self._timer_generation = 0
        self._closed = False

    def set_keep_alive(self, enabled: bool):
        with self._lock:
            if self._closed:
                return
            self._keep_alive = enabled
            self._cancel_idle_timer()
            if enabled:
                threading.Thread(target=self._warmup, daemon=True, name="qwen3-stt-warmup").start()
            elif self._proc:
                self._schedule_idle_stop()

    def _warmup(self):
        with self._lock:
            if self._closed or not self._keep_alive:
                return
            try:
                self._start()
            except Exception:
                # A later recognition attempt may retry and report the error.
                pass

    def _cancel_idle_timer(self):
        self._timer_generation += 1
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _schedule_idle_stop(self):
        self._cancel_idle_timer()
        generation = self._timer_generation
        def stop_if_idle():
            with self._lock:
                if generation == self._timer_generation and not self._keep_alive and not self._closed:
                    self._idle_timer = None
                    self._stop_worker()
        self._idle_timer = threading.Timer(300, stop_if_idle)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _start(self):
        if self._proc and self._proc.poll() is None:
            return
        self._lines = queue.Queue()
        self._proc = subprocess.Popen(
            [str(self.python.absolute()), "-u", str(Path(__file__).with_name("qwen3_worker.py")), str(self.model.resolve())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        atexit.register(self.close)
        proc, lines = self._proc, self._lines
        def read_lines():
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)
        threading.Thread(target=read_lines, daemon=True, name="qwen3-stt-output").start()
        if not self._read(180).get("ready"):
            self._stop_worker()
            raise RuntimeError("Không nạp được model Qwen3-ASR")

    def _read(self, timeout):
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self._stop_worker()
            raise RuntimeError("Qwen3-ASR không phản hồi kịp") from None
        if line is None:
            self._stop_worker()
            raise RuntimeError("Tiến trình Qwen3-ASR đã dừng")
        return json.loads(line)

    def transcribe(self, pcm16: bytes) -> str:
        if not pcm16:
            return ""
        with self._lock:
            if self._closed:
                raise RuntimeError("Qwen3-ASR đã đóng")
            self._cancel_idle_timer()
            try:
                self._start()
                context = self.context.read_text(encoding="utf-8")[:8000] if self.context.is_file() else ""
                request = {"audio": base64.b64encode(pcm16).decode("ascii"), "context": context}
                self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
                response = self._read(120)
            except (BrokenPipeError, OSError):
                self._stop_worker()
                raise RuntimeError("Mất kết nối tới Qwen3-ASR") from None
            finally:
                if not self._keep_alive and self._proc:
                    self._schedule_idle_stop()
            if response.get("error"):
                raise RuntimeError(response["error"])
            return response.get("text", "").strip()

    def close(self):
        with self._lock:
            self._closed = True
            self._cancel_idle_timer()
            self._stop_worker()

    def _stop_worker(self):
        atexit.unregister(self.close)
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        if proc:
            for stream in (proc.stdin, proc.stdout):
                if stream:
                    stream.close()


class SttQwenFallback:
    """Retry Qwen each request; use Sherpa only when Qwen fails."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.qwen = None
        self.sherpa = None
        self.set_keep_alive(str(cfg.get("llm.keep_alive", "")).strip() == "-1")

    def set_keep_alive(self, enabled):
        self.keep_alive = enabled
        try:
            if self.qwen is None:
                self.qwen = SttQwen(self.cfg)
            self.qwen.set_keep_alive(enabled)
        except Exception:
            pass

    def transcribe(self, pcm16):
        try:
            if self.qwen is None:
                self.qwen = SttQwen(self.cfg)
                self.qwen.set_keep_alive(self.keep_alive)
            return self.qwen.transcribe(pcm16)
        except Exception:
            if self.sherpa is None:
                self.sherpa = SttSherpa(self.cfg)
            return self.sherpa.transcribe(pcm16)

    def close(self):
        if self.qwen:
            self.qwen.close()


class SttQwenOllama:
    """Qwen3-ASR on Ollama, with a local Sherpa fallback."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.url = str(cfg.get("stt.qwen_ollama_url", "") or "http://127.0.0.1:11434").strip().rstrip("/")
        self.model = str(cfg.get("stt.qwen_ollama_model", "") or "frozenlab/qwen3-asr:1.7b").strip()
        self.sherpa = None
        self._lock = threading.Lock()

    def transcribe(self, pcm16):
        if not pcm16:
            return ""
        try:
            if len(pcm16) % 2:
                raise ValueError("Âm thanh PCM16 không hợp lệ")
            buf = io.BytesIO()
            with wave.open(buf, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(pcm16)
            payload = {"model": self.model, "stream": False,
                       "messages": [{"role": "user", "content": "", "images": [base64.b64encode(buf.getvalue()).decode("ascii")]}],
                       "keep_alive": self.cfg.get("llm.keep_alive", "5m")}
            response = httpx.post(self.url + "/api/chat", json=payload, timeout=180)
            response.raise_for_status()
            content = response.json()["message"]["content"]
            if "<asr_text>" not in content:
                raise ValueError("Phản hồi không phải Qwen3-ASR")
            return content.split("<asr_text>", 1)[1].strip()
        except Exception:
            with self._lock:
                if self.sherpa is None:
                    self.sherpa = SttSherpa(self.cfg)
            return self.sherpa.transcribe(pcm16)


def make_stt(cfg: Config):
    engine = cfg.get("stt.engine", "none")
    if engine == "sherpa":
        return SttSherpa(cfg)
    if engine == "qwen3_torch":
        return SttQwenFallback(cfg)
    if engine == "qwen3_ollama":
        return SttQwenOllama(cfg)
    return SttNone()
