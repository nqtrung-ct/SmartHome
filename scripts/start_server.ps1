# Khởi động SmartHome server ẨN (không cửa sổ) — dùng cho Task Scheduler.
# Idempotent: nếu server cũ đang chiếm 8124 thì dọn trước khi chạy lại.
# Log ghi đè mỗi lần start vào data/server.log.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

# dọn instance cũ (chỉ giết tiến trình python của repo này đứng trên 8124)
$conn = Get-NetTCPConnection -LocalPort 8124 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $($conn.OwningProcess)"
    if ($proc -and $proc.CommandLine -match "SmartHome") {
        Stop-Process -Id $conn.OwningProcess -Force -Confirm:$false
        Start-Sleep -Seconds 2
    } else {
        # cổng bị tiến trình lạ chiếm — thoát kẻo sinh đụng độ
        "[start_server] port 8124 bi chiem bo tien trinh khac (pid $($conn.OwningProcess)) - khong start" |
            Out-File -Append -Encoding utf8 "$root\data\server.log"
        exit 1
    }
}

"[start_server] $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') khoi dong server" |
    Out-File -Append -Encoding utf8 "$root\data\server.log"
& "$root\.venv\Scripts\python.exe" -X utf8 server.py *>> "$root\data\server.log"
