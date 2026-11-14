"""Imou LAN visualtalk: AAC/DHAV over the camera's authenticated HTTP stream.

Protocol research: https://github.com/home-assistant-plugins/imou-life
Tested audibly on the room camera; a 200 response alone does not prove sound.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

from server.drivers.dahua_ptz import _creds, _parts

_lock = threading.Lock()
_SDP = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Talk\r\nc=IN IP4 0.0.0.0\r\n"
        "t=0 0\r\nm=video 0 RTP/AVP 96\r\na=control:trackID=31\r\n"
        "m=audio 0 RTP/AVP 8 96\r\na=rtpmap:8 PCMA/8000\r\n"
        "a=rtpmap:96 MPEG4-GENERIC/16000/1\r\na=control:trackID=5\r\na=sendrecv\r\n").encode()


class TalkError(Exception):
    pass


class _Session:
    def __init__(self, host: str, user: str, password: str):
        self.host, self.user, self.password = host, user, password
        self.nonce = secrets.token_hex(16)
        self.created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.buffer = b""
        self.sock = socket.create_connection((host, 8086), timeout=8)
        self.sock.settimeout(8)

    def _fill(self, size):
        while len(self.buffer) < size:
            data = self.sock.recv(8192)
            if not data:
                raise TalkError("Camera đóng kênh âm thanh")
            self.buffer += data

    def _response(self):
        while True:
            self._fill(1)
            if self.buffer.startswith(b"$"):
                self._fill(6)
                length = int.from_bytes(self.buffer[2:6], "big")
                if length > 4_000_000:
                    raise TalkError("Khung camera không hợp lệ")
                self._fill(6 + length)
                self.buffer = self.buffer[6 + length:]
                continue
            while b"\r\n\r\n" not in self.buffer:
                self._fill(len(self.buffer) + 1)
                if len(self.buffer) > 65536:
                    raise TalkError("Phản hồi camera quá dài")
            header, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
            text = header.decode("latin1")
            match = re.search(r"(?im)^(?:private|content)-length:\s*(\d+)", text)
            length = int(match.group(1)) if match else 0
            if length > 1_000_000:
                raise TalkError("Phản hồi camera quá dài")
            self._fill(length)
            self.buffer = self.buffer[length:]
            return text

    def play(self, track, seq, realm=None, offer=b"", talk=False):
        path = ("/live/visualtalk.xav?channel=1&subtype=0&encrypt=3&imagesize=18"
                f"&audioType=1&trackID={track}&method=0" + ("&talktype=talk" if talk else ""))
        key = (hashlib.md5(f"{self.user}:{realm}:{self.password}".encode()).hexdigest().upper()
               if realm else self.password)
        digest = base64.b64encode(hashlib.sha1((self.nonce + self.created + key).encode()).digest()).decode()
        wsse = (f'UsernameToken Username="{self.user}", PasswordDigest="{digest}", '
                f'Nonce="{self.nonce}", Created="{self.created}"')
        headers = (f"PLAY {path} HTTP/1.1\r\nHost: {self.host}:8086\r\nConnect-Type: P2P\r\n"
                   f"Connection: keep-alive\r\nCseq: {seq}\r\nSpeed: 1.000000\r\n"
                   'User-Agent: Http Stream Client/1.0\r\nAuthorization: WSSE profile="UsernameToken"\r\n'
                   f"WSSE: {wsse}\r\n")
        if offer:
            headers += f"Accpet-Sdp: Private\r\nPrivate-Type: application/sdp\r\nPrivate-Length: {len(offer)}\r\n"
        self.sock.sendall((headers + "\r\n").encode() + offer)
        return self._response()

    def start(self):
        response = self.play(31, 0, offer=_SDP)
        challenge = re.search(r'(?i)realm="([^"]+)"', response)
        if not response.startswith("HTTP/1.1 401 ") or not challenge:
            raise TalkError("Camera không trả yêu cầu xác thực đàm thoại")
        realm = challenge.group(1)
        for track, seq, offer, talk in ((31, 0, _SDP, False), (6, 1, b"", False), (64, 2, b"", True)):
            response = self.play(track, seq, realm, offer, talk)
            if not response.startswith("HTTP/1.1 200 "):
                raise TalkError(f"Camera từ chối kênh đàm thoại ({response.splitlines()[0]})")


def _aac_frames(audio):
    offset = 0
    while offset < len(audio):
        if offset + 7 > len(audio) or audio[offset] != 0xff or audio[offset + 1] & 0xf0 != 0xf0:
            raise TalkError("Dữ liệu AAC không hợp lệ")
        size = ((audio[offset + 3] & 3) << 11) | (audio[offset + 4] << 3) | (audio[offset + 5] >> 5)
        if size < 7 or offset + size > len(audio):
            raise TalkError("Khung AAC không đầy đủ")
        yield audio[offset:offset + size]
        offset += size


def speak(cfg, device, wav: bytes) -> None:
    """Gửi WAV qua loa camera, trả về khi dữ liệu đã gửi xong."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise TalkError("Không tìm thấy ffmpeg để mã hóa âm thanh camera")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                           "-af", "volume=3", "-ar", "16000", "-ac", "1", "-c:a", "aac",
                           "-b:a", "32k", "-f", "adts", "pipe:1"], input=wav,
                          capture_output=True, timeout=30, creationflags=flags)
    if proc.returncode or not proc.stdout:
        raise TalkError("Không mã hóa được âm thanh camera")
    frames = list(_aac_frames(proc.stdout))
    host, _, mac = _parts(device)
    user, password = _creds(cfg, host, mac)
    with _lock:
        session = _Session(host, user, password)
        try:
            session.start()
            stop = threading.Event()
            def drain():
                try:
                    while not stop.is_set() and session.sock.recv(8192):
                        pass
                except OSError:
                    pass
            threading.Thread(target=drain, daemon=True).start()
            timestamp, tick = int(time.time()), int(time.monotonic() * 1000)
            start = time.monotonic()
            for seq, payload in enumerate(frames):
                size = len(payload) + 36
                header = bytearray(28)
                struct.pack_into("<4sB3xII", header, 0, b"DHAV", 0xf0, seq, size)
                struct.pack_into("<I", header, 16, timestamp)
                struct.pack_into("<H", header, 20, (tick + 64 * seq) & 0xffff)
                header[22] = 4
                header[23] = sum(header[:23]) & 255
                header[24:28] = b"\x83\x01\x1a\x04"
                frame = bytes(header) + payload + b"dhav" + struct.pack("<I", size)
                session.sock.sendall(b"$\x0a" + struct.pack(">I", len(frame)) + frame)
                time.sleep(max(0, start + (seq + 1) * 0.064 - time.monotonic()))
            time.sleep(1)
            stop.set()
        finally:
            session.sock.close()
