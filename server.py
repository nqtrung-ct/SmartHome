"""Khởi động cả cụm SmartHome bằng một lệnh: python server.py

Lần lượt đảm bảo các dịch vụ kèm theo (mỗi cái chỉ khởi khi CHƯA chạy):
  1. Ollama   (LLM, port 11434)  — sinh `ollama serve` + nạp sẵn model cấu hình
  2. TTS piper (port 8125)       — scripts/tts_http_example.py (server cũng
                                   tự sinh lại khi nó chết giữa chừng)
  3. SmartHome server (uvicorn, port 8124) — tiến trình chính, chạy foreground

KHÔNG đụng Docker (Frigate/go2rtc tự khởi động riêng với máy).

Chạy: .venv\\Scripts\\python.exe -X utf8 server.py
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))

from server.config import Config  # noqa: E402
from server.voice.tts import ensure_sidecar  # noqa: E402

OLLAMA_PORT = 11434


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _spawn(args: list[str]) -> bool:
    kwargs = {}
    if sys.platform == "win32":
        # CREATE_NO_WINDOW (console ẩn) thay cho DETACHED_PROCESS: ollama.exe
        # thấy mình không có console là tự AllocConsole → bật cửa sổ cmd TRỐNG
        # lên desktop (đóng cửa sổ = kill ollama). Có sẵn console ẩn là nó
        # chịu, không mở gì cả. Sidecar TTS cũng vậy (piper AllocConsole).
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=str(Path(__file__).parent), **kwargs)
        return True
    except OSError:
        return False


def ensure_ollama() -> bool:
    """Ollama sống ở 11434; chưa có thì sinh `ollama serve`, chờ cổng lên."""
    if _port_open(OLLAMA_PORT):
        print("[+] Ollama đang chạy (11434)")
        return True
    exe = shutil.which("ollama") or str(Path.home()
                                        / "AppData/Local/Programs/Ollama/ollama.exe")
    if not Path(exe).exists() and not shutil.which("ollama"):
        print("[!] Không thấy 'ollama' trên PATH — LLM sẽ lỗi tới khi tự mở Ollama")
        return False
    print("[.] Khởi động Ollama...")
    if not _spawn([exe, "serve"]):
        print("[!] Không sinh được tiến trình Ollama")
        return False
    for _ in range(60):  # lần đầu Ollama cần vài giây
        if _port_open(OLLAMA_PORT):
            print("[+] Ollama đã lên (11434)")
            return True
        time.sleep(0.5)
    print("[!] Ollama không lên cổng sau 30 giây")
    return False


def warm_model(cfg: Config) -> None:
    """Nạp sẵn model LLM vào VRAM (keep_alive -1) để câu lệnh đầu không bị chờ."""
    model = cfg.get("llm.model")
    if not model:
        return
    try:
        r = httpx.post("http://127.0.0.1:11434/api/generate",
                       json={"model": model, "prompt": "", "keep_alive": -1},
                       timeout=300)
        r.raise_for_status()
        print(f"[+] Đã nạp sẵn model {model}")
    except Exception as exc:
        print(f"[!] Nạp model {model} thất bại ({exc}) — sẽ nạp lúc gọi đầu tiên")


def main() -> None:
    cfg = Config()
    ensure_ollama()
    # Keep-alive runs asynchronously in the server so a stalled model cannot
    # delay the whole application for the warm-up timeout.
    if _port_open(OLLAMA_PORT) and str(cfg.get("llm.keep_alive", "")).strip() != "-1":
        warm_model(cfg)
    if ensure_sidecar(cfg):
        print("[+] TTS sidecar sẵn sàng (8125)")
    else:
        print("[!] TTS sidecar chưa lên — giọng nói sẽ im lặng cho tới khi nó chạy")

    import uvicorn

    from server.main import create_app

    print(f"[.] SmartHome server: http://127.0.0.1:{cfg.get('server.port', 8124)}")
    uvicorn.run(create_app(cfg),
                host=str(cfg.get("server.host", "0.0.0.0")),
                port=int(cfg.get("server.port", 8124)),
                log_level="info")


if __name__ == "__main__":
    main()
