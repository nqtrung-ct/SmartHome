# Mac Apple Silicon + OrbStack

Các file trong thư mục này là mẫu triển khai không chứa tài khoản camera.
Database, model, video và cấu hình có mật khẩu vẫn nằm trong `data/` hoặc
`/Volumes/Camera/frigate`, được Git bỏ qua.

1. Cài và khởi động OrbStack. Sao chép các file mẫu vào `data/frigate/`;
   giữ `docker-compose.mac.yml` và `ptz_proxy.py` cùng thư mục.
2. Chuẩn bị `/Volumes/Camera/frigate/config/config.yml` với nguồn RTSP
   của camera và model ONNX tại `config/model_cache/yolo.onnx`. Cấu hình detector
   Frigate dùng `type: zmq`, `endpoint: tcp://host.docker.internal:5555`.
   Ghi video vào `/Volumes/Camera/frigate/media`; sao chép
   `mosquitto.conf` tới `/Volumes/Camera/frigate/mosquitto.conf`.
3. Cài detector native từ <https://github.com/frigate-nvr/apple-silicon-detector>.
   Sửa đường dẫn người dùng trong `dev.smarthome.frigate-detector.plist` trước
   khi đặt vào `~/Library/LaunchAgents/`. Detector lắng nghe localhost:5555.
4. Chạy `docker compose -f data/frigate/docker-compose.mac.yml up -d`.

Để tự đồng bộ IP camera sang Frigate, đặt `frigate.config_path` là
`/Volumes/Camera/frigate/config/config.yml` và `frigate.docker_command` là
`/usr/local/bin/docker` trong `config.yaml`. Camera cần có tên Frigate trong
`driver_data.frigate`. Sau khi SmartHome cập nhật IP, vòng kiểm tra camera sẽ
cập nhật các nguồn RTSP tương ứng và khởi động lại container; nếu khởi động
lại thất bại, hệ thống thử lại ở lần kiểm tra tiếp theo.
   Frigate UI ở cổng 8971; API nội bộ ở localhost:5001. IP trong compose là IP
   của bản triển khai hiện tại, cần sửa nếu địa chỉ camera/thiết bị thay đổi.
5. Dùng cấu hình `camera.ptz_proxy` và `network.tcp_proxies` trong
   `config.example.yaml` khi server nền trên Mac không truy cập được LAN.
   Đổi `frigate.base_url` thành `http://127.0.0.1:5001` và bật MQTT localhost.

Relay inverter tự cập nhật IP: tạo `data/frigate/solar/inverter-host` chứa IP
inverter hiện tại, rồi cấu hình `solar.tcp_proxy: 127.0.0.1:15020` và
`solar.proxy_target_file: data/frigate/solar/inverter-host`. Khi mất dữ liệu
liên tục 30 phút, server quét theo MAC và cập nhật tệp; lần quét tiếp theo
cách ít nhất 30 phút. Sau khi tìm thấy IP mới, lần đọc theo lịch sẽ thử lại.

## Tự khởi động sau khi đăng nhập/reboot

Chạy một lần trên Mac từ thư mục checkout:

```sh
chmod +x deploy/macos/install-autostart.sh deploy/macos/smarthome-launch.sh
./deploy/macos/install-autostart.sh
```

Script cài LaunchAgent `dev.smarthome` vào `~/Library/LaunchAgents`. Agent sẽ
tự mở OrbStack, chờ Docker sẵn sàng, chạy `docker compose up -d`, rồi khởi động
`server.py`; nếu server chết thì macOS tự chạy lại. Log nằm trong
`Logs/` trong thư mục project (ví dụ `/Volumes/Data/Projects/SmartHome/Logs`). Nếu detector nằm ở
`~/Library/SmartHome/apple-silicon-detector`, script cũng tự cài LaunchAgent
cho detector và sửa lỗi username hard-code trong plist mẫu. Cài lại script sau
khi đổi vị trí thư mục project.

PTZ cần bộ thư viện NetSDK Mach-O ARM64 trong SmartPSS ARM64 của Dahua;
không dùng DLL Windows. Đường dẫn mặc định:
`~/Library/SmartHome/SmartPSS.app/Contents/Frameworks`.

`model-build.Dockerfile` lưu công thức xuất YOLOv9-t 320px đã dùng khi triển khai.
Đặt mã nguồn YOLOv9 ở `model-build-src/yolov9/` và trọng số
`model-build-src/yolov9-t-converted.pt` trong build context. Công thức dùng wheel
Torch CPU cho Linux amd64, nên build với `--platform linux/amd64`; chỉ chạy
bước xuất model trong container. Model ONNX chạy bằng CoreML trên Mac.
