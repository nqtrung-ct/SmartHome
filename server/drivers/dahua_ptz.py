"""Điều khiển PTZ camera Dahua/Imou qua NetSDK (TCP 37777).

Imou khóa PTZ trên mọi đường mở (ONVIF stub, HTTP CGI treo) nhưng giao thức
binary Dahua vẫn chạy: NetSDK login bằng user 'admin' + mã bảo mật thiết bị
(cùng tài khoản RTSP, lưu trong secret nhóm 'rtsp'). Preset tạo trong app
Imou Life nằm trên cloud — preset hệ thống dùng phải tự lưu qua SET_PRESET.

DLL chính hãng Dahua nằm ở data/netsdk/win64 (git-ignored, ~35MB, nguồn
github.com/452/dahua-sdk — mirror bản SDK tải từ trang chủ Dahua).
"""
from __future__ import annotations

import ctypes as C
import json
import os
import threading

from server.util import load_secret, norm_mac

# NET_PTZ_ControlType (bản cơ bản của CLIENT_DHPTZControlEx2)
UP, DOWN, LEFT, RIGHT = 0, 1, 2, 3
SET_PRESET, DEL_PRESET, GOTO_PRESET = 11, 12, 10
DIRS = {"up": UP, "down": DOWN, "left": LEFT, "right": RIGHT}

_sdk = None
_sdk_lock = threading.Lock()   # NetSDK không đảm bảo thread-safe: gọi tuần tự như IR
_sessions: dict[str, int] = {}  # "host:port" -> handle login còn hạn


class _DeviceInfo(C.Structure):
    _fields_ = [
        ("sSerialNumber", C.c_byte * 48),
        ("byAlarmInPortNum", C.c_int), ("byAlarmOutPortNum", C.c_int),
        ("byDiskNum", C.c_int), ("byDVRType", C.c_int), ("byChanNum", C.c_int),
        ("byLimitLoginTime", C.c_ubyte), ("byLeftLogTimes", C.c_ubyte),
        ("bReserved", C.c_ubyte * 2), ("byLockLeftTime", C.c_int),
        ("Reserved", C.c_ubyte * 24),
    ]


class PtzError(Exception):
    pass


def _load():
    global _sdk
    if _sdk is not None:
        return _sdk
    from server.config import Config
    lib_dir = Config().data_path("netsdk/win64")
    dll = os.path.join(str(lib_dir), "dhnetsdk.dll")
    if not os.path.exists(dll):
        raise PtzError("Chưa có NetSDK DLL trong data/netsdk/win64")
    os.add_dll_directory(str(lib_dir))
    sdk = C.WinDLL(dll)
    sdk.CLIENT_Init.argtypes = [C.c_void_p, C.c_void_p]
    sdk.CLIENT_LoginEx2.argtypes = [C.c_char_p, C.c_uint16, C.c_char_p, C.c_char_p,
                                    C.c_int, C.c_void_p, C.POINTER(_DeviceInfo),
                                    C.POINTER(C.c_int)]
    sdk.CLIENT_LoginEx2.restype = C.c_longlong
    sdk.CLIENT_Logout.argtypes = [C.c_longlong]
    sdk.CLIENT_DHPTZControlEx2.argtypes = [C.c_longlong, C.c_int, C.c_int, C.c_int,
                                           C.c_int, C.c_int, C.c_int, C.c_void_p]
    sdk.CLIENT_DHPTZControlEx2.restype = C.c_bool
    sdk.CLIENT_Cleanup.argtypes = []
    sdk.CLIENT_Init(None, None)
    _sdk = sdk
    return sdk


def _parts(device) -> tuple[str, int, str]:
    extra = {}
    try:
        extra = json.loads(device["extra"] or "{}")
    except (ValueError, TypeError):
        pass
    return device["host"], int(extra.get("netsdk_port", 37777)), (device["mac"] or "")


def _creds(cfg, host: str, mac: str) -> tuple[str, str]:
    """Tài khoản NetSDK = tài khoản RTSP (secret 'rtsp', khóa host hoặc MAC)."""
    raw = ""
    for key in (host, norm_mac(mac)):
        if key:
            raw = load_secret(cfg, "rtsp", key) or ""
            if raw:
                break
    if not raw:
        raise PtzError("Camera chưa có mật khẩu (secret rtsp)")
    try:
        d = json.loads(raw)
        return d.get("user", "admin"), d.get("pass", "")
    except ValueError:
        return "admin", raw


def _login(cfg, host: str, port: int, mac: str) -> int:
    key = f"{host}:{port}"
    h = _sessions.get(key, 0)
    if h:
        return h
    sdk = _load()
    user, pwd = _creds(cfg, host, mac)
    err = C.c_int(0)
    info = _DeviceInfo()
    h = sdk.CLIENT_LoginEx2(host.encode(), port, user.encode(), pwd.encode(),
                            0, None, C.byref(info), C.byref(err))
    if h == 0:
        raise PtzError(f"Đăng nhập PTZ thất bại (mã {err.value})")
    _sessions[key] = h
    return h


def _drop(host: str, port: int) -> None:
    h = _sessions.pop(f"{host}:{port}", 0)
    if h and _sdk:
        try:
            _sdk.CLIENT_Logout(h)
        except Exception:
            pass


def _control(cfg, device, cmd: int, p1: int, stop: int = 0) -> bool:
    """Gửi 1 lệnh PTZ; phiên hỏng thì login lại đúng 1 lần rồi thử lại."""
    sdk = _load()
    host, port, mac = _parts(device)
    with _sdk_lock:
        for attempt in (0, 1):
            h = _login(cfg, host, port, mac)
            if sdk.CLIENT_DHPTZControlEx2(h, 0, cmd, p1, 0, 0, stop, None):
                return True
            _drop(host, port)
            if attempt:
                return False
    return False


def goto_preset(cfg, device, index: int) -> None:
    if not _control(cfg, device, GOTO_PRESET, int(index)):
        raise PtzError("Camera không nhận lệnh quay về góc đã lưu")


def save_preset(cfg, device, index: int) -> None:
    if not _control(cfg, device, SET_PRESET, int(index)):
        raise PtzError("Camera không lưu được góc này")


def delete_preset(cfg, device, index: int) -> None:
    _control(cfg, device, DEL_PRESET, int(index))


def move(cfg, device, direction: str, speed: int = 4, stop: bool = False) -> None:
    cmd = DIRS.get(direction)
    if cmd is None:
        raise PtzError(f"Hướng không hợp lệ: {direction}")
    if not _control(cfg, device, cmd, speed, stop=1 if stop else 0):
        raise PtzError("Camera không nhận lệnh xoay")
