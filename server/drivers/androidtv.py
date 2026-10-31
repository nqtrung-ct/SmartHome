"""Android TV / Google TV (TCL, Sony, Xiaomi...) qua giao thức Android TV Remote v2.

Đúng giao thức app "Google TV" chính hãng (TLS, port 6466/6467) — không cần ADB.
Thư viện androidtvremote2 là asyncio thuần nên driver giữ một event loop riêng
trong thread nền để duy trì kết nối lâu dài (TV tự báo bật/tắt/app/âm lượng);
các hàm Driver (set_state/get_state/pair...) chuyển tiếp coroutine sang loop đó.
Ghép nối 1 lần: pair_start() làm TV hiện mã 6 số, pair_finish(code) ghi nhớ
chứng chỉ client vào data/androidtv/ — sau này kết nối lại không cần mã.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading

from server.db import Database, log_event
from server.drivers.base import Driver, DriverError, Row
from server.util import normalize, now_iso

# lệnh Driver -> phím Android: bật = WAKEUP. Tắt: TCL bỏ qua SLEEP nên dùng POWER
# ("off" chỉ đến khi lệnh là tắt; "on" vẫn WAKEUP — không lộn xộn tắt/bật)
KEYS = {
    "on": "KEYCODE_WAKEUP",
    "off": "KEYCODE_POWER",
    "toggle": "KEYCODE_POWER",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "mute": "KEYCODE_VOLUME_MUTE",  # toggle mute/unmute
    "home": "KEYCODE_HOME",
    "back": "KEYCODE_BACK",
    "pause": "KEYCODE_MEDIA_PLAY_PAUSE",  # toggle tạm dừng/phát tiếp
}

# app hay dùng: package Android (send_launch_app_command tự ghép market://launch?id=)
TV_APPS = {
    "youtube": ("YouTube", "com.google.android.youtube.tv"),
    "netflix": ("Netflix", "com.netflix.ninja"),
    "spotify": ("Spotify", "com.spotify.tv.android"),
}
APP_LABELS = {pkg: label for _, (label, pkg) in TV_APPS.items()}
APP_LABELS.update({
    "com.google.android.tvlauncher": "Màn hình chính",
    "com.android.systemui": "Màn hình chính",
})


class AndroidTVDriver(Driver):
    name = "androidtv"

    def __init__(self, cfg, db: Database):
        self._client_name = str(cfg.get("androidtv.client_name", "Nhà thông minh"))
        self._db = db
        self._dir = cfg.data_path("androidtv")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._remotes: dict[int, object] = {}
        self._connected: dict[int, bool] = {}
        self._keepalive: set[int] = set()  # device đã có task tự nối lại
        self._lock = threading.Lock()

    def start(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="androidtv", daemon=True).start()

    def stop(self) -> None:
        for remote in self._remotes.values():
            self._on_loop(remote.disconnect)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # -- plumbing: chạy coroutine của thư viện trên loop riêng ----------------

    def _on_loop(self, fn, *args) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(fn, *args)

    def _call(self, coro, timeout: float = 15.0):
        if not self._loop:
            raise DriverError("Driver TV chưa khởi động (restart server?)")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise DriverError("TV không phản hồi kịp — TV đang bật và cùng mạng chứ?") from None
        except Exception as exc:  # map lỗi thư viện sang tiếng Việt
            raise self._translate(exc) from exc

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        from androidtvremote2 import CannotConnect, ConnectionClosed, InvalidAuth

        if isinstance(exc, InvalidAuth):
            return DriverError(
                "TV chưa ghép nối (hoặc chứng chỉ đã đổi) — vào tab Thiết bị, "
                "sửa TV rồi bấm \"Bắt đầu ghép nối\"."
            )
        if isinstance(exc, CannotConnect):
            return DriverError("Không kết nối được TV — TV đang tắt hoặc ngủ sâu.")
        if isinstance(exc, (ConnectionClosed, OSError)):
            return DriverError("Kết nối tới TV bị ngắt — thử lại sau giây lát.")
        if isinstance(exc, ValueError):
            # bên kia mở cổng 6466/6467 nhưng không nói giao thức Android TV
            # (dịch vụ khác chiếm cổng). LƯU Ý: lỗi "DNSName values..." khi SINH
            # chứng chỉ client đã xử lý ở _ensure_cert — lỗi đó từng bị chẩn đoán
            # nhầm thành "không phải TV Android"
            return DriverError("Thiết bị ở địa chỉ này không phản hồi đúng giao thức TV Android.")
        return DriverError(f"Lỗi TV: {exc}")

    def _ensure_cert(self, dev_id: int) -> None:
        """Sinh chứng chỉ client nếu chưa có, với hostname ASCII.

        Thư viện sinh cert bằng x509.DNSName(client_name) — client_name tiếng Việt
        "Nhà thông minh" làm ValueError ("DNSName values..."), trước đây bị
        _translate() đổ lỗi sai là "không phải TV Android". Sinh sẵn cert với
        tên đã bỏ dấu; client_name tiếng Việt vẫn hiện trên TV lúc ghép nối.
        """
        cert = self._dir / f"tv{dev_id}.crt"
        key = self._dir / f"tv{dev_id}.key"
        if cert.exists() and key.exists():
            return
        from androidtvremote2.certificate_generator import generate_selfsigned_cert

        import re

        hostname = re.sub(r"[^a-z0-9-]+", "-", normalize(self._client_name)).strip("-") or "smarthome"
        cert_pem, key_pem = generate_selfsigned_cert(hostname)
        cert.write_bytes(cert_pem)
        key.write_bytes(key_pem)

    def _remote(self, device: Row, connect: bool = True):
        from androidtvremote2 import AndroidTVRemote

        with self._lock:
            remote = self._remotes.get(device["id"])
            if remote is None or remote.host != device["host"]:
                if remote is not None:
                    self._on_loop(remote.disconnect)
                self._keepalive.discard(device["id"])
                dev_id = device["id"]
                remote = AndroidTVRemote(
                    client_name=self._client_name,
                    certfile=str(self._dir / f"tv{dev_id}.crt"),
                    keyfile=str(self._dir / f"tv{dev_id}.key"),
                    host=device["host"],
                    loop=self._loop,
                )
                # TV báo trạng thái mới (bật/tắt, app, âm lượng) -> ghi vào DB
                for cb in (
                    remote.add_is_on_updated_callback,
                    remote.add_current_app_updated_callback,
                    remote.add_volume_info_updated_callback,
                ):
                    cb(lambda _v, d=dev_id: self._push_state(d))
                # TV vào standby là rớt kết nối — phải biết NGAY kẻo cờ connected
                # cũ khiến lệnh "bật" viết vào transport chết rồi mất im lặng
                remote.add_is_available_updated_callback(
                    lambda avail, d=dev_id: self._on_availability(d, avail)
                )
                self._remotes[dev_id] = remote
                self._connected[dev_id] = False
                self._ensure_cert(dev_id)  # không để thư viện sinh cert (bẫy DNSName tiếng Việt)
        if connect and not self._connected.get(device["id"]):
            try:
                self._connect(device, remote)
            except DriverError:
                # không nối được (TV standby/tắt nguồn): chốt "off" vào DB —
                # màn hình đọc state từ DB, để nguyên là treo "on" cũ mãi
                self._write_state(device["id"], {"state": "off"})
                raise
        return remote

    def _connect(self, device: Row, remote) -> None:
        # 8s đủ cho bắt tay LAN (~1-2s); TV ở standby chấp nhận TCP nhưng treo
        # bắt tay — chờ lâu hơn chỉ làm người dùng đợi info lỗi thêm thôi
        self._call(remote.async_connect(), timeout=8)
        self._connected[device["id"]] = True
        # task nền nối lại mãi (backoff tới 30s) — chỉ tạo 1 lần mỗi remote;
        # InvalidAuth (chứng chỉ mất tin cậy) thì dừng, chờ ghép nối lại
        dev_id = device["id"]
        if dev_id not in self._keepalive:
            self._keepalive.add(dev_id)
            self._on_loop(
                lambda d=dev_id, r=remote: r.keep_reconnecting(
                    invalid_auth_callback=lambda: self._on_availability(d, False)
                )
            )
        self._push_state(dev_id)

    def _on_availability(self, dev_id: int, avail: bool) -> None:
        """Kết nối còn sống không. Rớt là chốt "off" vào DB ngay — TV standby
        đóng kết nối sau vài giây, không ghi thì DB treo "on" cũ vô hạn."""
        self._connected[dev_id] = avail
        if not avail and dev_id in self._remotes:
            self._write_state(dev_id, {"state": "off"})

    def _push_state(self, dev_id: int) -> None:
        """Ghi trạng thái TV mới nhất vào thực thể (gọi từ callback của loop)."""
        remote = self._remotes.get(dev_id)
        if remote is None or not self._connected.get(dev_id):
            return
        self._write_state(dev_id, self._state_of(remote))

    def _write_state(self, dev_id: int, state: dict) -> None:
        try:
            self._db.execute(
                "UPDATE entities SET state=?, updated_at=? WHERE device_id=?",
                (json.dumps(state, ensure_ascii=False), now_iso(), dev_id),
            )
        except Exception as exc:  # không để callback chết vì DB
            log_event(self._db, "androidtv", f"Cập nhật trạng thái TV lỗi: {exc}", level="warn")

    @staticmethod
    def _state_of(remote) -> dict:
        state: dict = {"state": "on" if remote.is_on else "off"}
        if remote.current_app:
            state["app"] = APP_LABELS.get(remote.current_app, remote.current_app)
        vol = remote.volume_info
        if vol:
            state["volume"] = vol.get("level")
            state["muted"] = bool(vol.get("muted"))
        return state

    # -- ghép nối (pair) — gọi từ admin API -----------------------------------

    def pair_start(self, device: Row) -> None:
        """TV hiện mã 6 số; người dùng đọc mã rồi gọi pair_finish."""
        remote = self._remote(device, connect=False)
        self._call(remote.async_start_pairing(), timeout=20)

    def pair_finish(self, device: Row, code: str) -> dict:
        remote = self._remote(device, connect=False)
        self._call(remote.async_finish_pairing(code.strip()), timeout=20)
        self._connect(device, remote)
        info = remote.device_info or {}
        log_event(
            self._db, "androidtv",
            f"Đã ghép nối TV '{device['name']}'"
            + (f" ({info.get('manufacturer')} {info.get('model')})" if info else ""),
        )
        return dict(info)

    def pair_status(self, device: Row) -> dict:
        out = {"connected": False, "reachable": False, "tv_name": "", "model": "", "is_on": None}
        remote = self._remote(device, connect=False)
        if self._connected.get(device["id"]):
            out["connected"] = True
            out["reachable"] = True
            info = remote.device_info or {}
            out["model"] = " ".join(x for x in (info.get("manufacturer"), info.get("model")) if x)
            out["is_on"] = bool(remote.is_on)
            return out
        # chưa ghép nối: thử bắt tay TLS đọc tên TV từ chứng chỉ của nó —
        # thành công là TV đang bật, sẵn sàng pair (không cần mã)
        try:
            name, _mac = self._call(remote.async_get_name_and_mac(), timeout=8)
            out["reachable"] = True
            out["tv_name"] = name
        except DriverError:
            pass
        return out

    # -- Driver interface ------------------------------------------------------

    def adopt(self, device: Row, body: dict) -> list[dict]:
        return [{
            "name": "TV",
            "entity_type": "media",
            "aliases": "tivi,tv",
            "driver_data": {},
        }]

    def set_state(self, entity: Row, device: Row, command: str, params: dict) -> dict:
        try:
            remote = self._remote(device)  # rớt kết nối là nối lại TRƯỚC khi gửi
        except DriverError as exc:
            if command == "on":
                raise DriverError(
                    f"{exc} Để bật được TV từ chế độ chờ, vào Cài đặt TV bật "
                    "mục 'Bật mạng khi ở chế độ chờ' (network standby)."
                ) from None
            raise

        if command == "set" and params.get("app"):
            pkg = str(params["app"])
            label = APP_LABELS.get(pkg, pkg)
            self._send(remote, lambda r: r.send_launch_app_command(pkg))
            return {"state": "on", "app": label}

        if command == "key" and params.get("key"):
            # gửi phím bất kỳ (test/admin): chỉ nhận tên KEYCODE_ hợp lệ
            key = str(params["key"]).upper()
            if not re.fullmatch(r"[A-Z0-9_]+", key):
                raise DriverError("Tên phím không hợp lệ")
            self._send(remote, lambda r, k=key: r.send_key_command(k))
            return self._state_of(remote)

        key = KEYS.get(command)
        if not key:
            raise DriverError(f"TV không hỗ trợ lệnh '{command}'")
        self._send(remote, lambda r: r.send_key_command(key))
        # phím gửi không đồng bộ; trả trạng thái đích — callback sẽ cập nhật lại
        state = {"state": "on"} if command == "on" else self._state_of(remote)
        return state

    def get_state(self, entity: Row, device: Row) -> dict:
        remote = self._remote(device)
        return self._state_of(remote)

    def _send(self, remote, fn) -> None:
        async def run() -> None:
            fn(remote)  # ConnectionClosed nổi lên qua _call -> DriverError tiếng Việt

        self._call(run(), timeout=5)
