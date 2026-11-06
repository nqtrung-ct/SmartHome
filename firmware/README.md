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

### Sinh lại font tiếng Việt (đã trả giá: "Mưa rào nhẹ" hiện "Ma rào nhẹ")

Font gen bằng `lv_font_conv` với range `-r 0x20-0x7F,0xA1-0xFF,0x101-0x17F,0x1AF-0x1B0,0x1EA0-0x1EF9,0x2013-0x2026`.
**Bẫy**: "ư/Ư" là U+01B0/U+01AF — nằm NGOÀI 0x101-0x17F và 0x1EA0-0x1EF9; thiếu range
`0x1AF-0x1B0` là LVGL lặng lẽ bỏ glyph đó → "Mưa" hiện "Ma" (mọi chữ khác vẫn đúng). Nguồn font:
`firmware/fonts/NotoSans-Regular.ttf` (đã copy vào repo). Sinh lại (PowerShell, phải **quote**
range kẻo bóc phẩy thành mảng):

```powershell
npx lv_font_conv --font firmware\fonts\NotoSans-Regular.ttf --size 20 --bpp 4 `
  --format lvgl --lv-include lvgl.h --lv-font-name font_vn_20 `
  -r "0x20-0x7F,0xA1-0xFF,0x101-0x17F,0x1AF-0x1B0,0x1EA0-0x1EF9,0x2013-0x2026" `
  -o firmware\xiaozhi-esp32\main\boards\quandong\esp32-s3-spi-2.8\font_vn_20.c --no-compress --no-prefilter
```

(đổi `--size 20`/`font_vn_20` thành 30 cho font to)

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

**Bẫy ccache nổi lên GIỮA chừng build.py (đã gặp 01/11, board waveshare 1.54)**: build.py
không truyền cờ qua idf.py được — khi lỗi `CreateProcess failed: ccache ...gcc` xuất hiện
trong lúc chạy build.py, đã configure xong rồi thì **bỏ dở build.py, tiếp bằng idf.py
thẳng** với `idf.py --no-ccache -DCMAKE_NINJA_FORCE_RESPONSE_FILE=ON reconfigure` rồi
`build` với cùng đôi cờ. Board/language/wake-word đã nằm sẵn trong sdkconfig + CMakeCache
sau configure của build.py — không mất.

## Board đã build

- `build-quandong/` — Thiết bị phòng ES3C28P (đang chạy ở phòng ngủ chính)
- `build/` — **Waveshare ESP32-S3-Touch-LCD-1.54 bản không touch** (build 01/11 cho
  phòng khách, chờ board về nạp): vi-VN + wn9_alexa + AEC phần cứng
  (`CONFIG_USE_DEVICE_AEC=y` có sẵn trong config board), codec ES8311 (loa) + ES7210
  (mic). Board không kèm loa — cần loa 8Ω 1-2W cắm cổng speaker. Nạp lần đầu dùng
  lệnh esptool full (kể cả assets wake word 0x800000) — xem output cuối build.

**Nạp (đã trả giá):** chỉ nạp **app** vào offset `0x20000` (`build\xiaozhi.bin`).
Tuyệt đối KHÔNG nạp merged-binary vào `0x0` — image raw phủ 0xFF qua NVS (0x9000)
làm **mất WiFi** mỗi lần nạp, phải cấu hình lại từ đầu. Đổi wake word model thì
phải flash cả partition assets `0x800000` (chỉ flash app là model không đổi).

Lưu ý:

- **Bẫy đường dẫn dài sau khi move từ `F:\esp` (đã trả giá 2026-11-01)**: đường dẫn dài hơn làm dòng lệnh compile vượt giới hạn 32K của Windows — ninja chết với `CreateProcess: The parameter is incorrect. (is the command line too long?)`; qua ccache thì ccache chết trước với "CreateProcess failed" cho gcc (báo quá sớm, dễ chẩn đoán sai). Chữa (cả hai cần): (1) `idf.py --no-ccache -DCMAKE_NINJA_FORCE_RESPONSE_FILE=ON reconfigure` rồi build với **cùng đôi cờ đó** — buộc argument vào `@rsp`; (2) bỏ ccache vì idf.py mặc định tự thêm `-DCCACHE_ENABLE=True` (cờ `--no-ccache`; lưu ý chỉ hét ccache khỏi PATH KHÔNG đủ — `CCACHE_FOUND` nằm sẵn trong CMakeCache). Sau khi reconfigure với FORCE_RESPONSE_FILE thì các lần build sau nhớ mang theo cờ, kẻo configure lại mất.
- Git không nằm trên PATH của máy — nếu idf.py phàn nàn, thêm `C:\Users\nqtga\AppData\Local\Atlassian\SourceTree\git_local\bin` vào PATH của phiên.
- Python venv trong `esp-tools` chứa **đường dẫn tuyệt đối** — di chuyển thư mục `firmware/` là venv hỏng (lỗi `Activation script failed` khi export). Chữa: chạy lại `install.ps1` của esp-idf (đã gặp và chữa được đúng thế).
- Giữ đường dẫn ngắn, không dấu, không khoảng trắng — hãng board cảnh báo ESP-IDF build fail nếu vi phạm.
- Kế hoạch tổng quan thiết bị phòng: `instructions/room-device-es3c28p-plan.md`.
