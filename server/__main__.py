"""Run with: python -m server"""
import uvicorn

from server.config import Config
from server.main import app

if __name__ == "__main__":
    cfg = Config()
    if cfg.get("server.admin_token") == "changeme":
        print("!! CẢNH BÁO: admin_token vẫn là 'changeme' - sửa config.yaml")
    uvicorn.run(
        app,
        host=str(cfg.get("server.host", "0.0.0.0")),
        port=int(cfg.get("server.port", 8124)),
        log_level="info",
    )
