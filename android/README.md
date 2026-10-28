# SmartHome Voice (Android)

App push-to-talk cho server SmartHome. Kotlin, Android 8.0+ (API 26).

## Build

1. Mở thư mục `android/` bằng **Android Studio** (Hedgehog trở lên). Lần đầu sync
   nó sẽ tự tải Gradle 8.7 và Android SDK 35 — chỉ cần internet.
2. Build → Run lên điện thoại.

Không có Android Studio mà có gradle + Android SDK: vào thư mục này chạy
`gradle wrapper` một lần để sinh gradle-wrapper.jar, rồi `./gradlew assembleDebug`.

## Dùng

1. Điện thoại và máy tính server phải cùng mạng WiFi/LAN.
2. Admin UI (máy tính) → tab **Máy thoại** → **Tạo mã ghép nối** (mã 6 số, 15 phút).
3. Mở app:
   - **Tự tìm server** (mDNS) hoặc nhập tay `http://<IP-máy-tính>:8124`
   - nhập mã ghép nối → **Ghép nối**
4. Admin UI: gán máy vào phòng. App hiện "Phòng: ..." sau khi kết nối lại.
5. **NHẤN & NÓI** — giữ nút nói, thả để gửi. Màn hình hiện cả câu bạn nói
   (để phát hiện lỗi nhận dạng) và câu trả lời; nếu server có TTS thì kèm tiếng.

## Ghi chú kỹ thuật

- Âm thanh: 16 kHz mono PCM16 stream qua WebSocket `/ws/voice?token=...`.
- Giao thức: `{"type":"start"}` → binary frames → `{"type":"stop"}`;
  server trả `transcript` → `reply` → `audio`(+binary) → `end`.
  Timer nhắc việc gửi kiểu `announce` + audio.
- Manifest bật `usesCleartextTraffic` vì server chạy http trong LAN.
- Bản v2 (hands-free, wake word, foreground service) xem
  `instructions/mvp-server-and-android-app.md` mục 6.
