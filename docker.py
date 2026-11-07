"""Khởi động Docker Desktop + cụm Frigate/go2rtc/mosquitto bằng một lệnh.

    .venv\\Scripts\\python.exe -X utf8 docker.py

Lần lượt:
  1. Docker engine (Docker Desktop) — chưa chạy thì mở Desktop rồi chờ engine
     (nóng ~30-60s, lạnh lần đầu sau bật máy có thể 2-3 phút)
  2. docker compose up -d cho data/frigate/docker-compose.yml:
     smarthome-frigate (có go2rtc gói bên trong) + smarthome-mosquitto.
     Compose idempotent — chạy lại không đổi gì nếu mọi thứ đang lên.
  3. Chờ Frigate API (127.0.0.1:5000) trả lời — camera cần vài chục giây
     bắt tay RTSP sau khi container lên.

Server chính (server.py) KHÔNG đụng Docker — chạy file này riêng khi cần
camera/NVR (Frigate chỉ ghi sự kiện + serve live; mất nó thì các lệnh
"camera có người không" và popup xem camera sẽ lỗi).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).parent
COMPOSE = ROOT / "data" / "frigate" / "docker-compose.yml"
DOCKER_DESKTOP = Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")
ENGINE_TIMEOUT_S = 240   # Docker Desktop lạnh khá lâu
FRIGATE_TIMEOUT_S = 180


def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def engine_up() -> bool:
    return _run(["docker", "info"]).returncode == 0


def ensure_engine() -> bool:
    if engine_up():
        print("[+] Docker engine đang chạy")
        return True
    if not DOCKER_DESKTOP.exists():
        print("[!] Không thấy Docker Desktop — cài tại https://www.docker.com")
        return False
    print("[.] Khởi động Docker Desktop (lạnh có thể 2-3 phút)...")
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([str(DOCKER_DESKTOP)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    t0 = time.time()
    while time.time() - t0 < ENGINE_TIMEOUT_S:
        if engine_up():
            print(f"[+] Docker engine đã lên ({time.time() - t0:.0f}s)")
            return True
        time.sleep(3)
    print(f"[!] Engine không lên sau {ENGINE_TIMEOUT_S}s — mở Docker Desktop thủ công xem lỗi gì")
    return False


def compose_up() -> bool:
    if not COMPOSE.exists():
        print(f"[!] Không thấy {COMPOSE}")
        return False
    r = _run(["docker", "compose", "-f", str(COMPOSE), "up", "-d"])
    out = (r.stdout + r.stderr).strip()
    if out:
        print("    " + out.replace("\n", "\n    "))
    if r.returncode != 0:
        print("[!] compose up thất bại (xem log trên)")
        return False
    return True


def wait_frigate() -> bool:
    print("[.] Chờ Frigate API (127.0.0.1:5000)...")
    t0 = time.time()
    while time.time() - t0 < FRIGATE_TIMEOUT_S:
        try:
            r = httpx.get("http://127.0.0.1:5000/api/version", timeout=5)
            if r.is_success:
                print(f"[+] Frigate sẵn sàng ({time.time() - t0:.0f}s) — {r.text.strip()[:40]}")
                return True
        except Exception:
            pass
        time.sleep(3)
    print("[!] Frigate API không trả lời sau container đã lên — xem log:")
    log = _run(["docker", "logs", "--tail", "15", "smarthome-frigate"])
    print("    " + (log.stdout or log.stderr).strip().replace("\n", "\n    "))
    return False


def status() -> None:
    r = _run(["docker", "ps", "--format", "{{.Names}} | {{.Status}}"])
    print(r.stdout.strip() or "(không container nào đang chạy)")


def main() -> int:
    if not ensure_engine():
        return 1
    if not compose_up():
        return 1
    ok = wait_frigate()
    status()
    print("[i] UI Frigate: https://127.0.0.1:8971 | go2rtc API: 1984 | mosquitto: 1883")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
