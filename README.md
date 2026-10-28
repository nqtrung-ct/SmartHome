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

Mở `http://localhost:8124` → **Tổng quan** thêm phòng (kèm tầng) → **Quét mạng → ➕ Thêm tay** tạo thiết bị ảo gán vào phòng → thêm thực thể (đèn, quạt...) trong tab **Thiết bị** → tab **Lệnh**: thử `bật đèn`, `điều hòa 26 độ`, `15 phút nữa nhắc tôi tắt bếp`, `thời tiết hôm nay thế nào`.

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
| M2 | Server skeleton + admin UI | ✅ xong, smoke test pass |
| M3 | Discovery + adoption | ✅ code xong (Broadlink/Tuya/mDNS/SSDP/ONVIF/ping + adopt) — cần test với phần cứng thật |
| M4 | Text commands | ✅ fast path + LLM path + timers + thời tiết — timers đã test tự chạy |
| M5 | Android push-to-talk | ✅ code xong — cần build & thử trên máy thật + STT model |
| M1 | Models benchmark | ⏳ bạn tự đo theo docs/setup-windows.md §3 |

Chưa làm (theo kế hoạch): hands-free + wake word (M6), nhận diện giọng nói người dùng, camera Frigate, kiểm tra trạng thái AC qua công tắc đo công suất.
