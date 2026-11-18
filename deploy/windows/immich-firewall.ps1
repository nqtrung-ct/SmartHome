$ErrorActionPreference = 'Stop'
if (-not (Get-NetFirewallRule -DisplayName 'Immich LAN TCP' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName 'Immich LAN TCP' -Direction Inbound -Action Allow -Profile Private -Protocol TCP -LocalPort 2283 -RemoteAddress 192.168.1.0/24 | Out-Null
}
