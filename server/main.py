"""FastAPI application assembly."""
from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from server.api import admin, assistant, command_api, devices, discovery_api, voice_api
from server.config import Config
from server.core.scheduler import start_scheduler
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


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.data_path("models").mkdir(parents=True, exist_ok=True)

    db = Database(cfg.data_path("smarthome.db"))
    drivers = load_drivers(cfg, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.weather = Weather(cfg)
        app.state.pipeline = VoicePipeline(cfg, db, drivers, app.state.weather)
        app.state.voice_connections = VoiceConnections(db)
        app.state.scheduler_task = start_scheduler(app)
        app.state.learn_jobs = {}

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
