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
Màn treo  ───► │ /display (không đăng nhập; PIN 4 số để điều khiển)        │
   tường       │ SQLite: rooms/devices/entities/voice_clients/timers      │
                │ Discovery: Broadlink/Tuya/mDNS/SSDP/ONVIF/Zigbee/ping    │
                │ Drivers: Broadlink | Zigbee (MQTT) | Tuya local | dummy  │
                │ Camera: ffmpeg RTSP → MJPEG cho web                     │
                │ Scheduler: hẹn giờ + poll Tuya/camera → thông báo WS     │
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

Mở `http://localhost:8124` — **màn hiển thị treo tường** (đồng hồ + âm lịch, thời tiết, trạng thái từng phòng, hẹn giờ, cảnh báo điện lưới; bấm chip camera xem video; điều khiển thiết bị bằng mã PIN; tự cập nhật 30 giây/lần, không cần đăng nhập, có nút toàn màn hình). Quản trị ở `http://localhost:8124/admin`: thêm phòng (kèm tầng) → **Quét mạng → ➕ Thêm tay** tạo thiết bị gán vào phòng → thêm thực thể (đèn, quạt...) trong tab **Thiết bị** → tab **Lệnh**: thử `bật đèn`, `điều hòa 26 độ`, `15 phút nữa nhắc tôi tắt bếp`, `thời tiết hôm nay thế nào`.

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
| M3 | Discovery + adoption | ✅ xong + đã chạy với phần cứng thật (Broadlink RM4MINI, 2 thiết bị Tuya, camera ONVIF) |
| M4 | Text commands | ✅ fast path + LLM path + timers + thời tiết theo ngày |
| M5 | Android push-to-talk | ✅ đã cài trên điện thoại thật, tự kết nối lại |
| M6 | Hands-free + wake word | ✅ xong — "Trợ lý ơi … over", server-side wake, đã đo độ trễ |
| — | Màn hình treo tường + camera IP (ngoài lộ trình gốc) | ✅ xong |
| M1 | Models benchmark | ⏳ bạn tự đo theo docs/setup-windows.md §3 |

## Tiến độ chi tiết (cập nhật 2026-10-29)

**Đang chạy thật trong nhà:**
- **Broadlink RM4MINI** — hub hồng ngoại, đã học 300+ mã: điều hòa Panasonic (bật/tắt, nhiệt độ 16–30, chế độ lạnh/khô/gió/ổn, mức quạt), quạt đứng, loa Kanto. Hub chỉ phát được 1 mã mỗi lúc nên lệnh được xếp hàng, cách nhau ~1 giây. Điều hòa là IR một chiều (không đọc ngược trạng thái) → server nhớ thiết lập lần cuối; "bật máy lạnh" không kèm thông tin thì dùng lại lần trước và **trả lời kèm trạng thái** ("Đã bật điều hòa, nhiệt độ 28 độ, quạt mức yên lặng")
- **Cầu dao Tuya** (firmware 3.5, readonly) — bật/tắt qua LAN mỗi 60 giây; điện áp/dòng/công suất qua Tuya cloud mỗi 3 phút, đã sửa đúng cách giải mã `phase_a` (bố cục 8 byte chuẩn Home Assistant — trước gộp sai ra công suất ảo 3.9 kW). Nhà có điện mặt trời: sau cầu dao chỉ còn inverter nên công suất thường chỉ vài chục W
- **Cảm biến nhiệt độ-độ ẩm Tuya** — LAN mỗi 60 giây; hỏi *"nhiệt độ bao nhiêu"* / *"độ ẩm"*; quá 1 tiếng không có số mới thì UI hiện "—"
- **Camera Imou (ONVIF)** — tự sinh thực thể camera khi thêm thiết bị; scheduler kiểm tra online mỗi 60 giây (socket RTSP); **bấm vào chip camera trên màn hình treo tường xem được video trực tiếp** (server dùng ffmpeg đổi RTSP sub stream 640×352 → MJPEG, đóng popup là dừng); mật khẩu RTSP lưu trong `data/secrets.json`

**Màn hình treo tường** (`http://<ip>:8124`, không đăng nhập, tự cập nhật 30 giây):
- Đồng hồ 12h kèm buổi (Sáng/Trưa/Chiều/Tối) bên trái, ngày dương (đậm) + **âm lịch**; icon khóa cạnh nút toàn màn hình
- Card thời tiết (cache 10 phút; đổi lat/lng trong Thiết đặt là áp dụng ngay), min–max in đậm
- Card từng phòng: nhiệt/độ ẩm (độ ẩm đỏ khi ngoài 40–70%), icon SVG trắng đồng bộ (quạt 3 cánh, loa bookshelf, tuyết, giọt nước, camera); phòng không có cảm biến nhiệt vẫn hiện
- **PIN 4 số** mở điều khiển (hiệu lực 5 phút) — chặn trẻ vọc; popup camera chỉ xem nên không cần PIN
- Cảnh báo điện lưới (mất điện đỏ / sụt áp < 200V vàng) + danh sách hẹn giờ sắp tới

**Giọng nói:**
- Loại thực thể mới: **quạt** (fan), **loa** (media) — "bật quạt đứng", "tăng âm lượng loa", "quạt xoay đi"
- Lệnh trực tiếp không cần LLM ngày càng nhiều: "chỉnh máy lạnh 25 độ", "bật lại máy lạnh", "tăng lên 28 độ"...
- "Bật máy lạnh" (không kèm thông tin) → dùng lại thiết lập lần cuối và đọc lại trạng thái đang chạy

**Admin** (tiêu đề "Nhà Thông Minh"): dashboard theo tầng, hàng/cột; gộp cầu dao (công tắc + số đo V/W) thành một dòng; sửa tên/bí danh/phòng qua modal ✏️; hub Broadlink thêm/sửa/xóa thiết bị con và học mã IR ngay trong modal; tab Thiết đặt đổi lat/lng, PIN, retention...

**Chưa làm:** đảo cánh gió điều hòa (chờ remote vật lý để học mã), Zigbee + MQTT (chờ phần cứng), mạch ESP32 vệ tinh (dự kiến dùng lại phần wake word server-side của hands-free), phân biệt người dùng theo giọng, xem camera trong admin (hiện chỉ có trên màn hình treo tường).
