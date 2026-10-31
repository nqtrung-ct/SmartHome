# firmware/ — môi trường build thiết bị phòng (xiaozhi-esp32)

> Ghi chú để biết trong này có gì, thứ gì mất rồi tải lại được và từ đâu.
> Cả ba thư mục con đều nằm trong `.gitignore` (tổng >7GB) — chỉ file README này nằm trong git.
> (Move từ `F:\esp` về đây ngày 2026-11-01; `F:\esp` đã bỏ.)

| Thư mục | Là gì | Nếu mất thì |
|---|---|---|
| `xiaozhi-esp32/` | Clone <https://github.com/78/xiaozhi-esp32> tại commit `addb1ed` **+ các chỉnh sửa chưa commit** (danh sách bên dưới) | Tải lại được phần upstream; **phần chỉnh sửa KHÔNG tải lại được** — phải tự backup |
| `esp-idf/` | ESP-IDF **v6.1.0** (clone git sạch, không chỉnh sửa gì) | `git clone -b v6.1 --recursive https://github.com/espressif/esp-idf.git` rồi chạy `install.ps1` |
| `esp-tools/` | Toolchain (xtensa/riscv gcc, cmake, ninja, openocd...) + python env (`python_env/idf6.1_py3.12_env`) + bộ cache `dist/` | Chạy lại `install.ps1` của esp-idf — cache `dist/` giúp khỏi tải lại phần lớn; thiếu tool nào thì tự tải từ releases của Espressif trên GitHub |

## Chỉnh sửa chưa commit trong xiaozhi-esp32 — PHẦN QUAN TRỌNG NHẤT, MẤT LÀ MẤT

- `main/boards/quandong/esp32-s3-spi-2.8/` — board Thiết bị phòng ES3C28P:
  - **file mới**: `room_display.cc` / `room_display.h` (màn hình phòng), `font_vn_20.c`, `font_vn_30.c` (font tiếng Việt)
  - **file sửa**: `config.h`, `config.json`, `quandong_s3_dev_board.cc`
- **file sửa** ở ngoài board: `main/application.cc` / `.h`, `main/display/display.h`, `main/audio/codecs/es8311_audio_codec.cc` / `.h`, `main/audio/engines/afe_audio_engine.cc`

Xem danh sách mới nhất bất cứ lúc nào: `git -C firmware/xiaozhi-esp32 status --short`.
Khuyến nghị: commit phần này vào fork riêng trên GitHub (repo này đang trỏ thẳng upstream `78/xiaozhi-esp32`, không push nổi).

## Build

```powershell
$env:IDF_TOOLS_PATH = "F:\Working\projects\SmartHome\firmware\esp-tools"
. "F:\Working\projects\SmartHome\firmware\esp-idf\export.ps1"
cd firmware\xiaozhi-esp32
python -X utf8 scripts\build.py quandong/esp32-s3-spi-2.8 --language vi-VN --wake-word wn9_alexa
```

(build thô không qua wrapper: `idf.py build` — nhưng wrapper trên đặt đúng target/board/ngôn ngữ.)

**Nạp (đã trả giá):** chỉ nạp **app** vào offset `0x20000` (`build\xiaozhi.bin`).
Tuyệt đối KHÔNG nạp merged-binary vào `0x0` — image raw phủ 0xFF qua NVS (0x9000)
làm **mất WiFi** mỗi lần nạp, phải cấu hình lại từ đầu. Đổi wake word model thì
phải flash cả partition assets `0x800000` (chỉ flash app là model không đổi).

Lưu ý:

- Git không nằm trên PATH của máy — nếu idf.py phàn nàn, thêm `C:\Users\nqtga\AppData\Local\Atlassian\SourceTree\git_local\bin` vào PATH của phiên.
- Python venv trong `esp-tools` chứa **đường dẫn tuyệt đối** — di chuyển thư mục `firmware/` là venv hỏng (lỗi `Activation script failed` khi export). Chữa: chạy lại `install.ps1` của esp-idf (đã gặp và chữa được đúng thế).
- Giữ đường dẫn ngắn, không dấu, không khoảng trắng — hãng board cảnh báo ESP-IDF build fail nếu vi phạm.
- Kế hoạch tổng quan thiết bị phòng: `instructions/room-device-es3c28p-plan.md`.
