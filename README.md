# SmartHome

Nhà thông minh MVP: nói tiếng Việt vào điện thoại Android → server hiểu → điều khiển thiết bị (Broadlink IR, Zigbee, Tuya) → trả lời bằng giọng. Kèm admin web UI để quản lý phòng/thiết bị, quét mạng tìm thiết bị, học mã IR, thử lệnh văn bản.

Theo lộ trình trong [`instructions/mvp-server-and-android-app.md`](instructions/mvp-server-and-android-app.md) (xem mục Milestones).

## Kiến trúc

```
                ┌──────────────── Server (FastAPI, Python) ────────────────┐
Android app ──► │ Voice gateway (WebSocket audio)                          │
   (Kotlin)     │   → STT (sherpa-onnx Zipformer-vi) → Lệnh (fast-path     │
                │     hoặc LLM Ollama tool-calls) → Drivers → TTS trả lời  │
Admin UI ─────► │ REST API  (http://<ip>:8124, token)                      │
                │ SQLite: rooms/devices/entities/voice_clients/timers      │
                │ Discovery: Broadlink/Tuya/mDNS/SSDP/ONVIF/Zigbee/ping    │
                │ Drivers: Broadlink | Zigbee (MQTT) | Tuya local | dummy  │
                │ Scheduler: hẹn giờ → phát thông báo qua máy trong phòng  │
                └──────────────────────────────────────────────────────────┘
```

- **Device** = một món phần cứng; **Entity** = một chức danh điều khiển được (điều hòa, đèn, cảm biến); **Room** gom entity + máy thoại, có **tầng** để nhóm trên dashboard (giúp "bật điều hòa" hiểu đúng phòng); **Voice client** = điện thoại (token + phòng).
- Lệnh thường gặp đi **fast path** (regex, không cần LLM); câu phức tạp / nhiều việc / hội thoại rơi sang **LLM** (Ollama, tool calling). Thời tiết: open-meteo, không cần key.

## Chạy thử trong 2 phút (không phần cứng)

```powershell
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml   # đổi admin_token
python -m server
```

Mở `http://localhost:8124` — **màn hiển thị treo tường** (thời tiết, trạng thái phòng, hẹn giờ; tự cập nhật 30 giây/lần, không cần đăng nhập, có nút toàn màn hình). Quản trị ở `http://localhost:8124/admin`: thêm phòng (kèm tầng) → **Quét mạng → ➕ Thêm tay** tạo thiết bị gán vào phòng → thêm thực thể (đèn, quạt...) trong tab **Thiết bị** → tab **Lệnh**: thử `bật đèn`, `điều hòa 26 độ`, `15 phút nữa nhắc tôi tắt bếp`, `thời tiết hôm nay thế nào`.

Chi tiết đầy đủ (mô hình AI, Broadlink/Zigbee/Tuya, app Android, firewall): [`docs/setup-windows.md`](docs/setup-windows.md).

## Cấu trúc

```
server/            FastAPI app (api/, core/, drivers/, voice/, static admin UI)
android/           app Kotlin push-to-talk
scripts/           smoke_test.py, tts_http_example.py
data/              (git-ignored) DB, models, secrets, IR codes
config.yaml        (git-ignored) copy từ config.example.yaml
instructions/      tài liệu định hướng dự án
```

## Trạng thái milestone

| # | Milestone | Trạng thái |
|---|---|---|
| M2 | Server skeleton + admin UI | ✅ xong, smoke 35/35 |
| M3 | Discovery + adoption | ✅ xong + đã chạy với phần cứng thật (Broadlink RM4MINI, 2 thiết bị Tuya) |
| M4 | Text commands | ✅ fast path + LLM path + timers + thời tiết theo ngày |
| M5 | Android push-to-talk | ✅ đã cài trên điện thoại thật, tự kết nối lại |
| M6 | Hands-free + wake word | ✅ xong — "Trợ lý ơi … over", server-side wake, đã đo độ trễ |
| M1 | Models benchmark | ⏳ bạn tự đo theo docs/setup-windows.md §3 |

## Tiến độ chi tiết (cập nhật 2026-10-28)

**Đang chạy thật trong nhà:**
- **Broadlink RM4MINI** (192.168.1.17) — quét ra, chưa học mã IR (nên "mở máy lạnh" vẫn chưa điều khiển được điều hòa thật)
- **Tuya breaker đo điện** (192.168.1.2, firmware 3.5) — công tắc chung cả nhà, **chỉ xem** (readonly): trạng thái bật/tắt qua LAN mỗi 60 giây; **điện áp / công suất / số điện (kWh)** qua Tuya cloud mỗi 3 phút (firmware giấu các chỉ số đo qua LAN; nhà có điện mặt trời nên công suất chỉ hiển thị, không đọc bằng giọng)
- **Tuya cảm biến nhiệt độ-độ ẩm** (192.168.1.30) — đọc LAN, cập nhật mỗi 60 giây, hỏi bằng giọng *"nhiệt độ bao nhiêu"* / *"độ ẩm"* (trọng tâm theo câu hỏi); quá 1 tiếng không báo số liệu thì UI hiện "—" và trợ lý nói không lấy được thông tin

**Hoàn thành các hạng mục phụ:**
- Trợ lý trả lời trọng tâm: hỏi mưa → trả lời theo % khả năng mưa ("rất thấp, chỉ 6%"), "tối nay" dùng dữ liệu 18h–23h theo giờ
- Dọn dữ liệu tự động: sự kiện giữ 30 ngày (chỉnh được trong Thiết đặt), mã ghép nối cũ/hẹn giờ xong cũng tự dọn
- Danh sách quét chỉ hiện thiết bị hỗ trợ (ping sweep cả mạng vẫn chạy nền)
- Readonly là cơ chế chung cho mọi driver: ẩn nút trên UI, chặn cả voice lẫn API

**Chưa làm:** học mã IR Broadlink (chờ làm với điều hòa thật), Zigbee + MQTT (chờ phần cứng), camera/Frigate, tablet treo tường, mạch ESP32 vệ tinh (dự kiến dùng lại phần wake word server-side của hands-free), phân biệt người dùng theo giọng.
