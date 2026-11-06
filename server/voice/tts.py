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


class TtsHttp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.url = cfg.get("tts.http_url", "")
        # length_scale piper: càng lớn đọc càng chậm (1.0 = tốc gốc)
        self.length_scale = cfg.get("tts.length_scale")

    def synthesize(self, text: str) -> tuple[bytes, int] | None:
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
        audio = resp.content
        return audio, wav_sample_rate(audio)


def make_tts(cfg: Config):
    engine = cfg.get("tts.engine", "none")
    if engine == "http" and cfg.get("tts.http_url"):
        return TtsHttp(cfg)
    return TtsNone()
