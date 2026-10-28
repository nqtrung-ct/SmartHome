# Setup trên Windows (bản MVP)

Từ ngày đầu tiên: mọi thứ chạy **native Python trên Windows** (không Docker), mọi dữ liệu nằm trong `data/`, mọi cấu hình trong `config.yaml` + biến môi trường `SMARTHOME_*`. Khi chuyển sang Linux sau này, việc cần làm là: `git clone`, `pip install -r requirements.txt`, copy thư mục `data/`, chạy lại.

## 1. Chuẩn bị

1. Python 3.11+ (`python --version`), Git, ffmpeg (để convert audio nếu cần).
2. Cho phép port qua firewall (chỉ cần 1 lần, chạy PowerShell **admin**):

```powershell
netsh advfirewall firewall add rule name="SmartHome Server" dir=in action=allow protocol=TCP localport=8124
```

3. Đặt IP cố định cho máy tính và các thiết bị (Broadlink, Tuya, camera) trong DHCP của router. Ghi lại vào `docs/network.md`.
4. Đặt mạng WiFi/LAN là **Private** trong Windows Settings → Network.

## 2. Cài server

```powershell
cd F:\Working\projects\SmartHome
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
notepad config.yaml   # đổi admin_token!
```

Chạy:

```powershell
python -m server
```

Mở `http://localhost:8124` (từ điện thoại: `http://<IP-máy-tính>:8124`). Đăng nhập bằng `admin_token`.

### Kiểm tra nhanh không cần phần cứng

1. Tab **Tổng quan** → **➕ Thêm phòng** (đặt tên + chọn tầng).
2. Tab **Quét mạng** → bấm **➕ Thêm tay** → loại *Ảo (test)*, gán vào phòng vừa tạo, Lưu.
3. Tab **Thiết bị** → với thiết bị ảo, thêm thực thể: Đèn (light, bí danh "đèn"), Quạt (switch, "quạt"), Điều hòa (climate, "điều hòa")...
4. Tab **Lệnh** → chọn phòng, gõ `bật đèn`, `điều hòa 26 độ`, `15 phút nữa nhắc tôi tắt bếp`.
5. Chạy `python scripts/smoke_test.py http://127.0.0.1:8124 <admin_token>` — tự tạo dữ liệu test qua API rồi dọn sạch sau khi xong.

## 3. Mô hình AI (M1 — từng cái riêng, đo thời gian + VRAM bằng `nvidia-smi`)

| Vai trò | Cài đặt | Ghi chú |
|---|---|---|
| LLM (hiểu lệnh) | **Ollama** (ollama.com) → `ollama pull qwen3:4b` | Tool calling tốt, tiếng Việt tốt. Thử thêm `gemma3:4b`, `qwen2.5:3b-instruct` rồi chọn theo 20–30 câu lệnh thật của bạn. Server tự dùng khi Ollama đang chạy ở `127.0.0.1:11434`. |
| STT (nhanh) | `pip install -r optional-requirements.txt` (sherpa-onnx) + model **Zipformer-vi** → giải nén vào `data/models/stt/` (tokens.txt + encoder/decoder/joiner .onnx, bản int8 ~25MB là đủ). Model tải từ [sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models) (tìm `zipformer-vi`). Hoặc đổi engine + xem hướng dẫn ngay trong tab **Trợ lý** của admin UI. | Chạy CPU, để GPU cho LLM+TTS (6GB của GTX 1660). |
| TTS | Nhanh nhất: `pip install piper-tts`, tải giọng [vi_VN-vais1000-medium](https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN/vais1000/medium/vi_VN-vais1000-medium.onnx) (+ file `.onnx.json`) vào `data/models/tts/<tên>/`, chạy `python scripts/tts_http_example.py`, rồi trong tab **Trợ lý** đặt engine `http` + URL `http://127.0.0.1:8125/tts`. Muốn chất lượng hơn thì thay bằng **VieNeu-TTS** — giữ nguyên giao diện POST `{"text"}` → wav, chỉ sửa hàm `synthesize()` trong script. | Không có TTS thì app vẫn chạy, chỉ hiện text. |

**Mục tiêu M1:** bảng thời gian + VRAM cho từng model; tổng thời gian từ *hết câu nói* đến *bắt đầu câu trả lời* < 2–3 giây.

## 4. Phần cứng thật

- **Broadlink**: tab *Quét mạng* → quét → *Thêm vào hệ thống* (hoặc **Thêm tay** nhập IP nếu quét không thấy) → gán phòng. Tab *Thiết bị* → thêm thực thể (VD: Điều hòa) → **📡 Học mã** từng nút (`off`, `cool_25`, `cool_26`...). Nhớ: remote điều hòa gửi **toàn bộ trạng thái** mỗi lần bấm — đặt remote đúng trạng thái rồi mới học.
- **Zigbee**: chạy Mosquitto + Zigbee2MQTT (theo `instructions/phase-1-refer.md`, bản Windows dùng Node.js + COM port của dongle), cấu hình `mqtt.host` trỏ đúng. Sensor đã pair sẽ tự xuất hiện trong danh sách quét để thêm vào.
- **Tuya**: cài `tinytuya`, lấy local key qua tài khoản Tuya IoT (tinytuya wizard). Khi "thêm vào hệ thống" sẽ hỏi local key. Sau đó điều khiển hoàn toàn local.
- **Camera Imou**: quét tìm qua ONVIF (wsdiscovery). Khi thêm, nhập mật khẩu RTSP — server lưu vào `data/secrets.json` dùng sau này.

## 5. App Android

1. Mở thư mục `android/` bằng Android Studio (lần đầu nó tự tải Gradle 8.7 + SDK 35).
2. Build → Install lên điện thoại (Android 8+).
3. Trong admin UI: tab **Máy thoại** → *Tạo mã ghép nối*.
4. Trên app: bấm *Tự tìm server* (hoặc nhập `http://<ip>:8124`), nhập mã → Ghép nối.
5. Quay lại admin UI, gán máy vào một phòng. Bây giờ "bật điều hòa" sẽ hiểu là điều hòa **của phòng đó**.

## 6. Vận hành

- Nhật ký server: tab *Nhật ký* + console. File DB: `data/smarthome.db`.
- Đặt Windows không sleep khi test. Sau này trên Linux: systemd service.
- Bảo mật: chỉ nghe trên LAN, **không** mở port trên router. Remote thì dùng Tailscale/WireGuard.
