# CLAUDE.md

Ghi chú làm việc trong repo này (cho Claude Code và lập trình viên). Chi tiết bài bản: `docs/setup-windows.md`, `instructions/`.

## Chạy & kiểm thử

- Python venv: gọi thẳng `.venv\Scripts\python.exe` (không cần activate).
- Server: `.venv\Scripts\python.exe -m server` → `http://127.0.0.1:8124`. Admin token nằm trong `config.yaml` (khóa `admin_token`); API dùng header `Authorization: Bearer <token>`.
- TTS sidecar (bắt buộc khi test giọng nói): `.venv\Scripts\python.exe scripts\tts_http_example.py` → port 8125 (piper vi_VN-vais1000).
- LLM: Ollama (`qwen2.5:3b-instruct`) phải đang chạy.
- Smoke test sau mọi thay đổi server:
  `.venv\Scripts\python.exe -X utf8 scripts\smoke_test.py http://127.0.0.1:8124 <admin_token>` — phải đạt 35/35.
- Test WebSocket voice: `scripts/ws_voice_test.py`; hands-free (loopback piper làm mic ảo): `scripts/ws_handsfree_test.py`.
- In tiếng Việt trên console Windows: luôn thêm `-X utf8` cho python (mặc định cp1252 sẽ UnicodeEncodeError).

## Git — QUAN TRỌNG

- Git **không có trên PATH**. Dùng bản kèm SourceTree:
  `"C:\Users\nqtga\AppData\Local\Atlassian\SourceTree\git_local\bin\git.exe"`
- Remote: `https://github.com/nqtrung-ct/SmartHome` (tài khoản `nqtrung-ct`, khác tài khoản mặc định của máy).
- **PAT nằm trong `git_settings.md` — file đã git-ignore. Tuyệt đối không commit file đó và không dán token vào bất kỳ file nào trong repo.** Push bằng credential helper một lần (không lưu vào config):
  ```powershell
  & $git -c "credential.helper=!f() { echo username=nqtrung-ct; echo password=$pat; }; f" push origin main
  ```
  (`$pat` đọc từ `git_settings.md` lúc chạy, không ghi vào file).
- Không bao giờ commit: `config.yaml`, `data/` (DB, `secrets.json`, models, mã IR), `git_settings.md` — đều đã có trong `.gitignore`.

## Bản đồ code

- `server/core/text_command.py` — não lệnh: fast-path (regex trên bản normalized) + tool-calls LLM + các hàm `act_*` dùng chung. Mọi câu trả lời người dùng bằng tiếng Việt.
- `server/core/scheduler.py` — hẹn giờ + announce qua WS; poll Tuya: LAN 60 giây (`tuya.poll_seconds`), cloud 180 giây (`tuya.cloud_seconds`).
- `server/core/discovery.py` — các scanner (Broadlink/Tuya/mDNS/SSDP/ONVIF/ping). Danh sách mặc định chỉ hiện thiết bị hỗ trợ (`?all=1` xem tất cả).
- `server/drivers/` — mẫu `Driver.adopt/set_state/get_state`; `driver_data` (JSON trong DB) là nơi cấu hình từng entity (dps Tuya, `readonly`, `cloud_meter`...).
- `server/voice/` — pipeline STT→lệnh→TTS; `handsfree.py` = wake word server-side (wake "trợ lý ơi", kết thúc bằng keyword hoặc im lặng 3 giây).
- `server/static/admin/` — SPA admin (dashboard/tab); `server/api/` — các router REST.
- `android/` — app Kotlin push-to-talk + chế độ "Luôn nghe".

## Quy ước

- So khớp lệnh/alias đều qua `server.util.normalize()` (bỏ dấu, lowercase) — regex và alias viết cho bản đã normalize. Alias khớp **theo từ riêng** (`_alias_in`) — không dùng substring trần, kẻo alias ngắn "ac" trúng "khách" rồi tắt nhầm điều hòa thật.
- Câu trả lời giọng nói **không nêu phòng** khi lệnh không nói rõ phòng (mic ở phòng nào thì thiết bị phòng đó là mặc định); chỉ thêm tên phòng khi lệnh nêu rõ (xem `_room_where()` trong `text_command.py`).
- **Phòng của thực thể**: `entities.room_id` (NULL = theo phòng thiết bị gốc); mọi nơi nhóm/theo phòng dùng phòng hiệu dụng `COALESCE(e.room_id, d.room_id)` — xem `_entity_room()` trong `text_command.py`. Tab Thiết bị nhóm theo phòng; sửa tên/phòng qua modal ✏️; modal của hub (entity `controller`) còn thêm/sửa/xóa **thiết bị con** (UI gọi entity là "thiết bị con" với hub). Xóa hết thiết bị của một phần cứng thì phần cứng bị xóa theo (`DELETE /entities/{id}` tự dọn).
- **Entity readonly** (`"readonly": true` trong `driver_data`): UI ẩn nút, `act_switch` và `POST /entities/{id}/command` từ chối — áp dụng cho mọi driver. Dùng cho thiết bị chỉ-trạng-thái (breaker, hub luôn bật). Hub luôn bật còn dùng entity_type `controller` (chỉ tồn tại trong DB, không chọn được trong UI — badge "điều khiển", chấm xanh, từ chối bật/tắt).
- **Cảm biến**: `entities.state` là JSON các metric (`temperature`, `humidity`, `voltage`, `power`, `energy`...); quá 1 tiếng không có số liệu mới (`updated_at`) thì UI hiện "—" và voice trả lời "không lấy được thông tin". Không bao giờ ghi đè state tốt bằng giá trị rỗng.
- Thêm cấu hình: vào `DEFAULTS` trong `server/config.py`, đọc bằng `cfg.get("nhom.khoa")`.
- Secret (Tuya cloud, RTSP...) lưu trong `data/secrets.json` qua `store_secret()/load_secret()` trong `server/util.py`.

## Bẫy đã biết (đã trả giá)

- Máy có adapter VirtualBox/WSL → mọi discovery/broadcast UDP phải truyền `server.util.lan_ip()` làm local_ip, đừng để thư viện tự đoán.
- `server.db.Database()` nhận `pathlib.Path`, không nhận `str`.
- Tuya: cloud region `sg`; phải subscribe gói miễn phí **IoT Core** (lỗi `28841001` = chưa); map dp_id↔code bằng `cloud.getdps()`, đừng đoán; firmware 3.5 **giấu các dps đo điện qua LAN** (điện áp/công suất/kWh phải qua cloud); `tinytuya.deviceScan` nhận tham số `maxretry` (chính là số giây nghe), viết `maxRetry` sẽ TypeError im lặng.
- Thiết bị Tuya chạy pin ngủ sâu giữa hai lần báo — chỉ broadcast khi thức; poll thất bại giữ số liệu cũ.
- Cầu dao Tuya (category `dlq`): `phase_a` là blob 8 byte `[V×0.1: 2B][dòng mA: 3B][công suất W: 3B]` big-endian (không phải V+P 4 byte — gộp sai ra 3.9 kW ảo). Kiểm chứng bằng `total_forward_energy`: công suất thật = delta kWh theo giờ. Thiết bị sau cầu dao có thể chỉ là inverter (điều hòa/đèn không nằm trên meter này).
- PCM16 trước `np.frombuffer` phải chẵn byte (`keep &= ~1`).
- Camera Imou: RTSP dùng user `admin` + **mật khẩu thiết bị riêng** (mã bảo mật trên tem, khác mật khẩu tài khoản app Imou Life) — lưu trong `data/secrets.json` nhóm `rtsp.<ip>` dạng JSON `{"user","pass"}`. Sub stream: `/cam/realmonitor?channel=1&subtype=1` (H264 640×352); trình duyệt không mở RTSP → server chuyển MJPEG qua ffmpeg (`/api/display/camera/{id}/stream`). **PTZ bị khóa hoàn toàn qua local**: ONVIF chỉ là stub (GetPresets/AbsoluteMove/ContinuousMove đều "NotImplemented" — preset tạo trong app Imou nằm trên cloud, không lộ ra), HTTP RPC/CGI và cổng 8086 treo không trả lời; chỉ còn giao thức Dahua binary ở cổng 37777 (chưa test — cần SmartPSS/NetSDK).
- **Frigate NVR** (Docker): compose + config tại `data/frigate/` (git-ignored, chứa mật khẩu RTSP), bản chạy thật ở `D:\frigate` (config/media/cache). UI `https://<ip-máy>:8971` (cert tự ký, auth đã tắt — LAN-only), API nội bộ `127.0.0.1:5000` (không map ra ngoài). Chỉ ghi sự kiện (alerts 30 ngày, detections 10 ngày, mode motion, pre/post 5s), không quay 24/7 (`record.continuous.days: 0`). Frigate stable hiện là 0.18: **không còn preset `preset-rtsp-tcp`** (truyền vào sẽ thành tên file output → ffmpeg chết vòng lặp), schema record mới là `continuous.days / alerts.retain / detections.retain`. **IP camera đổi thì phải sửa tay 2 file config** (`data/frigate/config.yml` + `D:\frigate\config\config.yml`) rồi `docker restart smarthome-frigate` — Frigate không tự chữa IP như server chính.
- **PTZ camera Imou qua NetSDK** (`server/drivers/dahua_ptz.py`): mọi đường mở bị khóa (ONVIF stub, CGI/HTTP treo, RPC2 treo) nhưng **giao thức binary Dahua 37777 chạy** qua NetSDK DLL chính hãng (đặt tại `data/netsdk/win64`, ~35MB, nguồn mirror github.com/452/dahua-sdk). Login = user `admin` + mã bảo mật (cùng secret nhóm `rtsp`). **Imou nhận-OK-nhưng-bỏ-qua** các lệnh GOTO/SET preset cổ điển (cmd 10/11) và EXTPTZ 0x47; cái chạy thật: **di chuyển hướng cmd 0-3** và **FASTGOTO 0x33 (tọa độ tuyệt đối, thang 0-3600, wrap 3600)** + **đọc vị trí QueryDevState 0x36** (pan 0-3600 / tilt -1800..1800 / zoom 1-128). Vì vậy **preset = tọa độ tự lưu**: save = đọc vị trí hiện tại ghi vào `driver_data.presets=[{name,pan,tilt,zoom}]`, goto = FASTGOTO bắn lại đúng số (index = vị trí trong list). Preset trong app Imou Life nằm trên cloud, đọc không ra (QueryDevState 0x57 không hỗ trợ). Wall display chỉ được `goto` (endpoint chặn action khác); thêm/xóa/quay-đến ở admin (nút 🎯 tab Thiết bị). **Remount/flip camera: preset phải tạo lại** (tọa độ là vị trí motor, không phải vị trí trong phòng). Imou Life app chặn simulator.
- **Tự chữa IP bằng MAC** (`_cleanup_network_rows` trong `discovery.py`): cuối mỗi lần quét, dòng quét có MAC khớp `devices.mac` sẽ kéo thiết bị sang IP mới, gắn `adopted_device_id`, xóa dòng cũ (đúng cho mọi loại thiết bị). MAC camera lấy từ **đuôi UUID WS-Discovery**; Broadlink có sẵn; các scanner khác mượn MAC từ quét ping+ARP theo IP. Secret RTSP lưu khóa theo **IP lẫn MAC** (`norm_mac`); stream endpoint fallback host → MAC → entry duy nhất của nhóm rtsp. Bẫy: sqlite3.Row chọn thiếu cột trong SELECT sẽ raise "No item with that key" và job quét vẫn hiện done (error nằm riêng trong field `error`).

## Android build

- SDK: `C:\Program Files (x86)\Android\android-sdk` (compileSdk 36, buildToolsVersion 36.0.0, AGP 8.9.1); gradle 8.13 trong cache wrapper; `JAVA_HOME` = JBR của Android Studio.
- Cài lên điện thoại: `adb install -r app-debug.apk`.
