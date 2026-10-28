# Sơ đồ mạng nhà

Ghi lại IP cố định đã reserve trong DHCP của router. Mọi thứ phụ thuộc vào các địa chỉ này không đổi.

| Thiết bị | IP cố định | MAC | Ghi chú |
|---|---|---|---|
| Server (PC Windows / sau này Linux) | 192.168.?.? | | port 8124 |
| Broadlink (phòng khách) | | | RM4 |
| Broadlink (phòng ngủ) | | | RM4 |
| Ổ cắm Tuya | | | local key trong data/secrets.json |
| Camera Imou | | | mật khẩu RTSP trong data/secrets.json |
| Điện thoại Android | | | không cần cố định |

Subnet: `192.168.?.0/24` — cấu hình discovery trong `config.yaml` (`discovery.subnet`) nếu tự dò sai.
