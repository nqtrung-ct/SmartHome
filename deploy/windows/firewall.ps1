$ErrorActionPreference = 'Stop'
if (-not (Get-NetFirewallRule -DisplayName 'SmartHome LAN TCP' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName 'SmartHome LAN TCP' -Direction Inbound -Action Allow -Profile Private -Protocol TCP -LocalPort 8124,1984,8555,8971 -RemoteAddress 192.168.1.0/24 | Out-Null
}
if (-not (Get-NetFirewallRule -DisplayName 'SmartHome LAN WebRTC UDP' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName 'SmartHome LAN WebRTC UDP' -Direction Inbound -Action Allow -Profile Private -Protocol UDP -LocalPort 8555 -RemoteAddress 192.168.1.0/24 | Out-Null
}
