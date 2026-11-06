"""FastAPI application assembly."""
from __future__ import annotations

import asyncio
import re
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from server.api import admin, assistant, command_api, devices, discovery_api, voice_api, xiaozhi
from server.config import Config
from server.core.scheduler import start_scheduler
from server.core.vision_states import start_vision
from server.core.weather import Weather
from server.db import Database, log_event
from server.drivers import load_drivers
from server.voice.connections import VoiceConnections
from server.voice.pipeline import VoicePipeline


def lan_ip() -> str:
    """Best-effort LAN IPv4 (UDP connect trick, no traffic sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _ensure_tts_sidecar(cfg: Config, db) -> None:
    """TTS sidecar là tiến trình RIÊNG (scripts/tts_http_example.py) — chạy lại
    server mà quên nó là mọi câu trả lời giọng nói im lặng (lệnh vẫn xử lý đúng,
    đã trả giá). tts.engine=http trỏ về máy này mà cổng chưa mở thì tự sinh,
    chờ cổng lên cho kịp piper nạp model."""
    if cfg.get("tts.engine") != "http":
        return
    url = str(cfg.get("tts.http_url") or "")
    m = re.match(r"http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", url)
    if not m:
        return  # sidecar máy khác / dịch vụ ngoài — không đụng
    port = int(m.group(2)[1:]) if m.group(2) else 80
    if port <= 0 or _port_open(port):
        return
    script = Path(__file__).resolve().parents[1] / "scripts" / "tts_http_example.py"
    if not script.exists():
        return
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.DETACHED_PROCESS)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(  # tách hẳn khỏi vòng đời server
            [sys.executable, "-X", "utf8", str(script), str(port)],
            cwd=str(script.parents[1]), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
        )
    except Exception as exc:
        log_event(db, "tts", f"Tự khởi động sidecar thất bại: {exc}", level="warn")
        return
    for _ in range(30):  # piper nạp model mất vài giây
        if _port_open(port):
            log_event(db, "tts", f"Đã tự khởi động TTS sidecar (port {port})")
            return
        time.sleep(0.5)
    log_event(db, "tts", "TTS sidecar không lên cổng sau 15 giây", level="warn")


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.data_path("models").mkdir(parents=True, exist_ok=True)

    db = Database(cfg.data_path("smarthome.db"))
    drivers = load_drivers(cfg, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(_ensure_tts_sidecar, cfg, db)
        app.state.weather = Weather(cfg)
        app.state.pipeline = VoicePipeline(cfg, db, drivers, app.state.weather)
        app.state.voice_connections = VoiceConnections(db)
        app.state.scheduler_task = start_scheduler(app)
        app.state.learn_jobs = {}
        start_vision(app)

        for driver in drivers.values():
            try:
                driver.start()
            except Exception as exc:
                log_event(db, "driver", f"{driver.name}.start() lỗi: {exc}", level="warn")

        zc = _advertise_mdns(cfg)
        log_event(db, "server", f"Server đã khởi động (http://{lan_ip()}:{cfg.get('server.port')})")
        if cfg.get("server.admin_token") == "changeme":
            log_event(db, "server", "admin_token vẫn là mặc định - hãy đổi trong config.yaml", level="warn")

        yield

        app.state.scheduler_task.cancel()
        for driver in drivers.values():
            driver.stop()
        if zc:
            try:
                zc.unregister_all_services()
                zc.close()
            except Exception:
                pass
        db.close()

    app = FastAPI(title="SmartHome", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.db = db
    app.state.drivers = drivers

    for router in (admin.router, assistant.router, devices.router, discovery_api.router, command_api.router):
        app.include_router(router, prefix="/api")
    app.include_router(voice_api.router)
    app.include_router(xiaozhi.router)

    @app.get("/admin", include_in_schema=False)
    async def admin_redirect():  # StaticFiles doesn't redirect the bare path itself
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url="/admin/")

    static_dir = Path(__file__).parent / "static"
    admin_dir = static_dir / "admin"
    display_dir = static_dir / "display"
    if admin_dir.exists():
        app.mount("/admin", StaticFiles(directory=str(admin_dir), html=True), name="admin")
    if display_dir.exists():
        app.mount("/", StaticFiles(directory=str(display_dir), html=True), name="display")
    return app


def _advertise_mdns(cfg: Config):
    if not cfg.get("server.advertise_mdns", True):
        return None
    try:
        from zeroconf import ServiceInfo, Zeroconf

        port = int(cfg.get("server.port", 8124))
        info = ServiceInfo(
            "_smarthome._tcp.local.",
            "SmartHome-Server._smarthome._tcp.local.",
            addresses=[socket.inet_aton(lan_ip())],
            port=port,
            properties={"path": "/"},
        )
        zc = Zeroconf()
        zc.register_service(info)
        return zc
    except Exception:
        return None


app = create_app()
