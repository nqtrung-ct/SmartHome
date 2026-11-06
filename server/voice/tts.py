"""Text-to-speech engines: "none" (text only) or "http" (POST text -> wav bytes).

Wrap VieNeu-TTS (or any generator) in a tiny HTTP service and point
tts.http_url at it - see scripts/tts_http_example.py.
"""
from __future__ import annotations

import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import httpx

from server.config import Config


def wav_sample_rate(wav: bytes) -> int:
    """Parse the sample rate out of a RIFF header; 16000 if unparseable."""
    if len(wav) >= 28 and wav[:4] == b"RIFF" and wav[8:12] == b"WAVE":
        return struct.unpack_from("<I", wav, 24)[0]
    return 16000


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def ensure_sidecar(cfg: Config) -> bool:
    """TTS sidecar (scripts/tts_http_example.py) là tiến trình RIÊNG — chạy lại
    server / sidecar chết giữa chừng là mọi câu giọng nói im lặng dù lệnh vẫn
    xử lý đúng (đã trả giá). tts.engine=http trỏ về máy này mà cổng chưa mở thì
    tự sinh, chờ cổng lên cho kịp piper nạp model. Trả True khi sidecar sẵn sàng.
    """
    if cfg.get("tts.engine") != "http":
        return False
    url = str(cfg.get("tts.http_url") or "")
    m = re.match(r"http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", url)
    if not m:
        return False  # sidecar máy khác / dịch vụ ngoài — không đụng
    port = int(m.group(2)[1:]) if m.group(2) else 80
    if port <= 0 or _port_open(port):
        return True
    script = Path(__file__).resolve().parents[2] / "scripts" / "tts_http_example.py"
    if not script.exists():
        return False
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.DETACHED_PROCESS)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(  # tách hẳn khỏi vòng đời tiến trình gọi
            [sys.executable, "-X", "utf8", str(script), str(port)],
            cwd=str(script.parents[1]), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
        )
    except Exception:
        return False
    for _ in range(30):  # piper nạp model mất vài giây
        if _port_open(port):
            return True
        time.sleep(0.5)
    return False


class TtsNone:
    def synthesize(self, text: str) -> tuple[bytes, int] | None:
        return None


def _wav_to_pcm(wav: bytes) -> tuple[bytes, int]:
    """PCM16 bytes + sample rate từ WAV (giả định mono; parse chunk thật)."""
    rate = wav_sample_rate(wav)
    pos = 12  # sau 'RIFF<size>WAVE'
    while pos + 8 <= len(wav):
        cid = wav[pos:pos + 4]
        size = struct.unpack_from("<I", wav, pos + 4)[0]
        pos += 8
        if cid == b"data":
            return wav[pos:pos + size], rate
        pos += size + (size & 1)
    return b"", rate


def _pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class TtsHttp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.url = cfg.get("tts.http_url", "")
        # length_scale piper: càng lớn đọc càng chậm (1.0 = tốc gốc)
        self.length_scale = cfg.get("tts.length_scale")
        # khoảng lặng giữa các DÒNG (giây) — bản tin nhiều tin đọc dễ nghe hơn
        self.pause_seconds = float(cfg.get("tts.pause_seconds", 1.0) or 0)

    def _one(self, text: str) -> bytes:
        """Đọc MỘT câu, tự sinh lại sidecar nếu nó vừa chết."""
        payload = {"text": text}
        if self.length_scale:
            payload["length_scale"] = float(self.length_scale)
        try:
            resp = httpx.post(self.url, json=payload, timeout=60)
            resp.raise_for_status()
        except httpx.TransportError:
            # sidecar vừa chết → tự sinh lại rồi thử đúng câu này lần nữa,
            # kẻo người dùng chịu im lặng tới lần restart server kế tiếp
            if not ensure_sidecar(self.cfg):
                raise
            resp = httpx.post(self.url, json=payload, timeout=60)
            resp.raise_for_status()
        return resp.content

    def synthesize(self, text: str) -> tuple[bytes, int] | None:
        # nhiều dòng = bản tin nhiều tin: đọc từng dòng, chèn khoảng lặng
        # giữa các dòng (model được dặn xuống dòng sau mỗi tin)
        lines = [re.sub(r"^[\s\-–•*]+", "", ln).strip()
                 for ln in (text or "").split("\n")]
        lines = [ln for ln in lines if ln]
        if len(lines) > 1 and self.pause_seconds > 0:
            wavs = [self._one(ln) for ln in lines]
            rate = min(wav_sample_rate(w) for w in wavs)
            sil = b"\x00\x00" * int(rate * self.pause_seconds)
            pcm = sil.join(_wav_to_pcm(w)[0] for w in wavs)
            return _pcm_to_wav(pcm, rate), rate
        audio = self._one(text)
        return audio, wav_sample_rate(audio)


def make_tts(cfg: Config):
    engine = cfg.get("tts.engine", "none")
    if engine == "http" and cfg.get("tts.http_url"):
        return TtsHttp(cfg)
    return TtsNone()
