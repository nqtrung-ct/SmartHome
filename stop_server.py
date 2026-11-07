"""Dừng toàn bộ cụm SmartHome: server + TTS sidecar + Docker (Frigate/go2rtc).

    .venv\\Scripts\\python.exe -X utf8 stop_server.py

Lần lượt:
  1. Server (port 8124) — chỉ giết tiến trình python của repo này (cmdline
     chứa server.py / -m server), không đụng tiến trình lạ chiếm cổng
  2. TTS sidecar (port 8125, tts_http_example.py)
  3. docker compose stop — dừng smarthome-frigate + smarthome-mosquitto
     (giữ nguyên container để lần sau docker.py lên nhanh; vì restart policy
     là unless-stopped, container đã stop sẽ KHÔNG tự chạy lại theo engine)
  4. Thoát Docker Desktop (giải phóng luôn VM WSL ~2-4GB RAM)

Ollama KHÔNG bị đụng (dịch vụ dùng chung, model nóng giữ nguyên cho lần sau).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
COMPOSE = ROOT / "data" / "frigate" / "docker-compose.yml"
PS = ["powershell", "-NoProfile", "-Command"]


def _ps(script: str) -> str:
    r = subprocess.run(PS + [script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


def stop_port(port: int, markers: tuple[str, ...]) -> None:
    """Dừng tiến trình của repo này đang nghe port (cmdline phải chứa marker)."""
    pid = _ps(f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
              "-ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess")
    if not pid or not pid.isdigit():
        print(f"[+] port {port}: không có gì đang chạy")
        return
    cmdline = _ps(f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine")
    if not any(m in (cmdline or "") for m in markers):
        print(f"[!] port {port} bị pid {pid} chiếm nhưng không phải của mình "
              f"({(cmdline or '?')[:60]}) — bỏ qua")
        return
    subprocess.run(["taskkill", "/PID", pid, "/F", "/T"], capture_output=True)
    print(f"[+] port {port}: đã dừng pid {pid}")


def engine_up() -> bool:
    r = subprocess.run(["docker", "info"], capture_output=True)
    return r.returncode == 0


def stop_containers() -> None:
    if not engine_up():
        print("[+] Docker: engine không chạy — khỏi dừng container")
        return
    r = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "stop"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    out = (r.stdout + r.stderr).strip()
    if out:
        print("    " + out.replace("\n", "\n    "))
    print("[+] Docker: đã dừng container Frigate/mosquitto" if r.returncode == 0
          else "[!] compose stop lỗi (xem trên)")


def quit_docker_desktop() -> None:
    if not engine_up():
        print("[+] Docker Desktop: engine đã tắt sẵn")
        return
    # nhường cửa sổ chính Docker Desktop tự thoát (kéo cả VM WSL theo)
    subprocess.run(PS + ["Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue "
                         "| ForEach-Object { $_.CloseMainWindow() } | Out-Null"],
                   capture_output=True)
    for _ in range(20):  # chờ engine hạ ~30s
        if not engine_up():
            print("[+] Docker Desktop: đã thoát (engine tắt)")
            return
        time.sleep(1.5)
    # GUI không chịu — dừng tiến trình cho chắc
    subprocess.run(PS + ["Stop-Process -Name 'Docker Desktop' -Force "
                         "-ErrorAction SilentlyContinue; "
                         "wsl -t docker-desktop 2>$null"], capture_output=True)
    time.sleep(3)
    print("[+] Docker Desktop: đã dừng (force)" if not engine_up()
          else "[!] Engine vẫn sống — kiểm tra tay trong Docker Desktop")


def main() -> int:
    print("== Dừng cụm SmartHome ==")
    stop_port(8124, ("server.py", "-m server"))
    stop_port(8125, ("tts_http_example",))
    stop_containers()
    quit_docker_desktop()
    print("[i] Ollama vẫn chạy (model nóng). Khởi động lại tất cả: server.py + docker.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
