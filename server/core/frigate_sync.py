"""Keep explicitly configured Frigate RTSP sources in sync with camera hosts."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from server.db import log_event

_lock = threading.Lock()
_RTSP = re.compile(r"(rtsps?://(?:[^\s/@\"']+@)?)(\d{1,3}(?:\.\d{1,3}){3})(?=[:/\s?\"'#]|$)", re.I)


def _child(node, key):
    if isinstance(node, MappingNode):
        return next((v for k, v in node.value if isinstance(k, ScalarNode) and k.value == key), None)
    return None


def _scalars(node):
    if isinstance(node, ScalarNode):
        yield node
    elif isinstance(node, SequenceNode):
        for child in node.value:
            yield from _scalars(child)
    elif isinstance(node, MappingNode):
        for _, child in node.value:
            yield from _scalars(child)


def update_sources(text: str, hosts: dict[str, str]) -> str:
    """Change direct IP RTSP URLs only within each mapped camera and stream."""
    root = yaml.compose(text)
    cameras = _child(root, "cameras")
    streams = _child(_child(root, "go2rtc"), "streams")
    edits = {}
    for name, host in hosts.items():
        if ipaddress.ip_address(host).version != 4:
            continue
        for target in (_child(cameras, name), _child(streams, name)):
            for node in _scalars(target):
                span = (node.start_mark.index, node.end_mark.index)
                before = text[slice(*span)]
                after = _RTSP.sub(lambda match: match.group(1) + host, before)
                if after != before:
                    if span in edits and edits[span] != after:
                        raise ValueError("Camera dùng chung nguồn RTSP nhưng có IP khác nhau")
                    edits[span] = after
    for (start, end), value in sorted(edits.items(), reverse=True):
        text = text[:start] + value + text[end:]
    yaml.safe_load(text)
    return text


def sync_frigate(db, cfg) -> None:
    """Update the configured file and retry a failed restart on later polls."""
    path_value = cfg.get("frigate.config_path", "")
    if not path_value or not _lock.acquire(blocking=False):
        return
    try:
        path = Path(path_value)
        pending = path.with_name(path.name + ".smarthome-restart-pending")
        hosts = {}
        conflicts = set()
        for row in db.query("""SELECT d.host, e.driver_data FROM entities e
                JOIN devices d ON d.id=e.device_id
                WHERE d.driver='onvif' AND e.entity_type='camera'"""):
            try:
                name = json.loads(row["driver_data"] or "{}").get("frigate")
                host = str(ipaddress.IPv4Address(row["host"]))
            except (ValueError, TypeError, AttributeError):
                continue
            if name:
                if name in hosts and hosts[name] != host:
                    conflicts.add(name)
                hosts[name] = host
        for name in conflicts:
            del hosts[name]
        original = path.read_text(encoding="utf-8")
        updated = update_sources(original, hosts)
        if updated != original:
            mode = path.stat().st_mode
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
                temp_path = Path(tmp.name)
                try:
                    tmp.write(updated)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    os.chmod(temp_path, mode)
                except BaseException:
                    temp_path.unlink(missing_ok=True)
                    raise
            try:
                if path.read_text(encoding="utf-8") != original:
                    raise RuntimeError("Cấu hình Frigate đã thay đổi trong lúc đồng bộ")
                pending.touch(exist_ok=True)
                os.replace(temp_path, path)
            finally:
                temp_path.unlink(missing_ok=True)
        if pending.exists():
            docker = str(cfg.get("frigate.docker_command", "docker"))
            container = str(cfg.get("frigate.container", "smarthome-frigate"))
            result = subprocess.run([docker, "restart", container], capture_output=True, timeout=60)
            if result.returncode:
                raise RuntimeError("Không khởi động lại được container Frigate")
            pending.unlink(missing_ok=True)
            log_event(db, "discovery", "Đã đồng bộ IP camera và khởi động lại Frigate")
    except (OSError, ValueError, RuntimeError, yaml.YAMLError, subprocess.SubprocessError) as exc:
        log_event(db, "discovery", f"Không đồng bộ được IP camera với Frigate: {type(exc).__name__}", level="warn")
    finally:
        _lock.release()
