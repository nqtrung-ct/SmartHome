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

- So khớp lệnh/alias đều qua `server.util.normalize()` (bỏ dấu, lowercase) — regex và alias viết cho bản đã normalize.
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
- PCM16 trước `np.frombuffer` phải chẵn byte (`keep &= ~1`).

## Android build

- SDK: `C:\Program Files (x86)\Android\android-sdk` (compileSdk 36, buildToolsVersion 36.0.0, AGP 8.9.1); gradle 8.13 trong cache wrapper; `JAVA_HOME` = JBR của Android Studio.
- Cài lên điện thoại: `adb install -r app-debug.apk`.
