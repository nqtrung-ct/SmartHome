"""Inverter Senergy ECO 6K (họ SAJ) qua WiFi dongle - Modbus TCP cổng 502.

Dongle ESP32 (MAC Espressif) làm gateway Modbus TCP, trả lời như nhau với mọi
unit id. Bản đồ thanh ghi dưới đây đo thực nghiệm 2026-10-31, không có tài liệu
chính thức - chỉ đọc, không ghi.

Lưu ý bẫy: dongle chỉ nhận các yêu cầu <=125 thanh ghi; đọc nhiều hơn trả
EXCEPTION 0x02 (illegal address) dễ tưởng lầm là vùng không tồn tại. Vài khối
(0x1300, đuôi 0x1600) là ring-buffer replay khung dữ liệu gửi cloud - giá trị
nhảy loạn, đừng dùng.

Thanh ghi ổn định đã kiểm chứng với app Senergy:
  0x1010  PV1: V (x0.1) / A (x0.01) / P uint32 (x0.1W)
  0x1014  PV2: V / A / P uint32
  0x101C  tần số lưới (x0.01 Hz), 0x101D work mode (3 = chạy bình thường)
  0x2000  SOC pin (%)
  0x2006  điện áp pin (x0.1 V)
  0x2008  dòng pin (x0.01 A), 0x200A công suất pin (x0.1 W) - chuẩn BMS: âm = SẠC, dương = XẢ

Tải nhà không có thanh ghi trực tiếp (đã thử thí nghiệm bước tải 600W: không
thanh ghi nào đáp ứng) - card hiển thị tải = PV + Ppin (xấp xỉ, sai số bằng
đúng công suất mua/bán lưới tại thời điểm đó; app Senergy tính bằng công thức
cloud riêng).
"""
from __future__ import annotations

import socket
import struct

from server.drivers.base import Driver, DriverError, Row

HOST_DEFAULT_PORT = 502
# các khối nhỏ, mỗi lần poll đúng 2 yêu cầu đọc - nhẹ cho CPU yếu của dongle
REG_PV = 0x1010        # 16 thanh ghi: PV1 V/I/P32 + PV2 V/I/P32 + tần số + mode
REG_BATTERY = 0x2000   # 16 thanh ghi: SOC ... Vpin, Ipin, Ppin
TIMEOUT = 4.0


def _s16(v: int) -> int:
    return v - 0x10000 if v > 0x7FFF else v


class SenergyDriver(Driver):
    name = "senergy"

    def adopt(self, device: Row, body: dict) -> list[dict]:
        return [{
            "name": "Điện mặt trời",
            "entity_type": "sensor",
            "aliases": "điện mặt trời,inverter,pin mặt trời",
            "driver_data": {},
        }]

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("inverter ngắt kết nối giữa chừng")
            buf += chunk
        return buf

    def _read(self, sock: socket.socket, start: int, count: int) -> list[int]:
        pdu = struct.pack(">BHH", 3, start, count)  # fc3 holding registers
        sock.sendall(struct.pack(">HHHB", 1, 0, len(pdu) + 1, 1) + pdu)
        head = self._recv_exact(sock, 6)
        _, _, length = struct.unpack(">HHH", head)  # length = unit(1) + PDU
        payload = self._recv_exact(sock, length)
        if payload[0] != 1 or payload[1] & 0x80:
            raise OSError(f"inverter từ chối đọc thanh ghi @{start:#06x}")
        nbytes = payload[2]
        return [_s16(v) for v in struct.unpack(f">{nbytes // 2}H", payload[3:3 + nbytes])]

    def get_state(self, entity: Row, device: Row) -> dict:
        host = device["host"] or ""
        if not host:
            raise DriverError("Thiết bị inverter chưa có địa chỉ IP")
        try:
            with socket.create_connection((host, HOST_DEFAULT_PORT), timeout=TIMEOUT) as sock:
                sock.settimeout(TIMEOUT)
                pv = self._read(sock, REG_PV, 16)
                bat = self._read(sock, REG_BATTERY, 16)
        except OSError as exc:
            raise DriverError(f"Không nối được inverter ({exc})") from exc

        pv1 = (pv[2] << 16 | pv[3]) * 0.1
        pv2 = (pv[6] << 16 | pv[7]) * 0.1
        pv_total = pv1 + pv2
        bat_power = bat[10] * 0.1
        state = {
            "pv1_power": round(pv1),
            "pv2_power": round(pv2),
            "pv_power": round(pv_total),
            "pv1_voltage": round(pv[0] * 0.1, 1),
            "pv2_voltage": round(pv[4] * 0.1, 1),
            "battery_soc": bat[0],
            "battery_voltage": round(bat[6] * 0.1, 1),
            "battery_current": round(bat[8] * 0.01, 2),
            "battery_power": round(bat_power),  # âm = sạc, dương = xả (chuẩn BMS)
            # xấp xỉ: PV + Ppin (phần sạc âm tự trừ); chưa có thanh ghi lưới/tải,
            # sai số = công suất mua/bán lưới
            "load_power": round(max(pv_total + bat_power, 0)),
            "frequency": pv[12],  # Hz nguyên (thanh ghi lượng tử hóa 1Hz, nhấp nhô 49/50)
            "work_mode": pv[13],
        }
        if not any(v is not None and v != 0 for v in state.values()):
            raise DriverError("Inverter trả dữ liệu rỗng")
        return state

    def set_state(self, entity: Row, device: Row, command: str, params: dict) -> dict:
        raise DriverError("Inverter chỉ đọc, không điều khiển được")
