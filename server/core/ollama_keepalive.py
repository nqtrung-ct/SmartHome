"""Nạp model Ollama khi bật giữ model và mỗi ngày theo giờ nhà."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from server.db import log_event
from server.util import now


def _restart_local_ollama():
    """Stop only an Ollama server listening on the local default port."""
    if sys.platform == "win32":
        script = ("Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue | "
                  "Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { "
                  "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=$_\"; "
                  "Write-Output (\"{0}|{1}\" -f $_,$p.Name) }")
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                                text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError("Không kiểm tra được tiến trình đang nghe cổng Ollama")
        listeners = [line.strip().split("|", 1) for line in result.stdout.splitlines() if line.strip()]
        if any(len(row) != 2 or not row[0].isdigit() or row[1].lower() != "ollama.exe" for row in listeners):
            raise RuntimeError("Cổng Ollama do tiến trình khác chiếm; không khởi động lại")
        for pid, _ in listeners:
            result = subprocess.run(["taskkill", "/PID", pid, "/F", "/T"], capture_output=True,
                                    timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise RuntimeError("Không dừng được tiến trình Ollama")
    else:
        result = subprocess.run(["lsof", "-tiTCP:11434", "-sTCP:LISTEN"], capture_output=True,
                                text=True, timeout=10)
        if result.returncode not in (0, 1):
            raise RuntimeError("Không kiểm tra được tiến trình đang nghe cổng Ollama")
        pids = [int(value) for value in result.stdout.split()]
        if pids:
            listing = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True,
                                     text=True, check=True, timeout=10)
            processes = {}
            for line in listing.stdout.splitlines():
                parts = line.strip().split(None, 2)
                if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                    processes[int(parts[0])] = (int(parts[1]), parts[2])
            if any(pid not in processes or Path(processes[pid][1]).name != "ollama" for pid in pids):
                raise RuntimeError("Cổng Ollama do tiến trình khác chiếm; không khởi động lại")
            children = [pid for pid, (parent, exe) in processes.items()
                        if parent in pids and Path(exe).name == "ollama"]
            for pid in children + pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for _ in range(50):
                result = subprocess.run(["lsof", "-tiTCP:11434", "-sTCP:LISTEN"], capture_output=True,
                                        text=True, timeout=5)
                if not set(result.stdout.split()).intersection(map(str, pids)):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Tiến trình Ollama cũ chưa dừng")


class OllamaKeepAlive:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.loop = asyncio.get_running_loop()
        self.trigger = asyncio.Event()
        self.task = self.loop.create_task(self._run(), name="ollama-keepalive")

    def request_load(self):
        # The config endpoint runs in a FastAPI worker thread.
        self.loop.call_soon_threadsafe(self.trigger.set)

    async def _run(self):
        # At server startup, load the model without disrupting a working Ollama.
        if str(self.cfg.get("llm.keep_alive", "")).strip() == "-1":
            await self._attempt_load(restart=False)
        while True:
            current = now()
            midnight = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            try:
                await asyncio.wait_for(self.trigger.wait(), timeout=(midnight - current).total_seconds())
                self.trigger.clear()
            except asyncio.TimeoutError:
                pass
            if str(self.cfg.get("llm.keep_alive", "")).strip() != "-1":
                continue
            await self._attempt_load(restart=True)

    async def _attempt_load(self, restart):
        try:
            await self._load(restart=restart)
        except Exception as exc:
            log_event(self.db, "ollama", f"Không nạp được model Ollama: {type(exc).__name__}: {exc}", level="warn")

    async def _load(self, restart=False):
        cfg = self.cfg
        model = (cfg.get("llm.local_model", "") if cfg.get("llm.engine") == "openai"
                 else cfg.get("llm.model", ""))
        if not model:
            raise ValueError("Chưa cấu hình model Ollama")
        base = str(cfg.get("llm.ollama_url", "") or "http://127.0.0.1:11434").rstrip("/")
        url = urlsplit(base)
        local = url.hostname in ("127.0.0.1", "localhost", "::1") and (url.port or 11434) == 11434
        if restart:
            if not local:
                raise RuntimeError("Không thể khởi động lại Ollama trên máy khác")
            await asyncio.to_thread(_restart_local_ollama)
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(base + "/api/tags", timeout=5)
                response.raise_for_status()
            except httpx.ConnectError:
                if not local:
                    raise RuntimeError("Ollama từ xa không kết nối được; không thể tự khởi động")
                exe = shutil.which("ollama")
                if not exe and sys.platform == "win32":
                    candidate = Path.home() / "AppData/Local/Programs/Ollama/ollama.exe"
                    exe = str(candidate) if candidate.exists() else None
                if not exe:
                    raise RuntimeError("Không tìm thấy chương trình Ollama")
                kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
                subprocess.Popen([exe, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, **kwargs)
                for _ in range(30):
                    await asyncio.sleep(1)
                    try:
                        response = await client.get(base + "/api/tags", timeout=2)
                        response.raise_for_status()
                        break
                    except httpx.HTTPError:
                        continue
                else:
                    raise RuntimeError("Ollama không khởi động trong 30 giây")
            if str(cfg.get("llm.keep_alive", "")).strip() != "-1":
                return
            response = await client.post(base + "/api/generate", json={"model": model, "prompt": "", "keep_alive": -1}, timeout=120)
            response.raise_for_status()
            log_event(self.db, "ollama", f"Đã nạp và giữ model {model}")
