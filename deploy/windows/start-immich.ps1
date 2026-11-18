$ErrorActionPreference = 'Stop'
$dockerDir = 'C:\Program Files\Docker\Docker\resources\bin'
$docker = Join-Path $dockerDir 'docker.exe'
$env:PATH = "$dockerDir;$env:PATH"
$env:COMPOSE_ANSI = 'never'
$compose = 'D:\Immich\docker-compose.yml'
$share = '\\192.168.1.100\share'
$marker = 'I:\backup\Ti_Camera\Immich\upload\.immich'
$logDir = 'D:\Immich\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Start-Transcript -Path (Join-Path $logDir 'startup.log') -Append | Out-Null
try {
    $ready = $false
    for ($i = 0; $i -lt 180; $i++) {
        $oldPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $null = & $docker info --format '{{.ServerVersion}}' 2>&1
            $dockerReady = $LASTEXITCODE -eq 0
            if (-not (Test-Path -LiteralPath $marker)) {
                $null = & net.exe use 'I:' $share /persistent:yes 2>&1
            }
        } finally { $ErrorActionPreference = $oldPreference }
        if ($dockerReady -and (Test-Path -LiteralPath $marker)) {
            $oldPreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = 'Continue'
                $mountScript = 'if ! mountpoint -q /mnt/host/i; then mkdir -p /mnt/host/i && mount -t drvfs I: /mnt/host/i; fi; test -f /mnt/host/i/backup/Ti_Camera/Immich/upload/.immich'
                $null = & wsl.exe -d docker-desktop -u root -- sh -c $mountScript 2>&1
                $mountReady = $LASTEXITCODE -eq 0
                if ($mountReady) {
                    $null = & $docker run --rm --mount 'type=bind,source=/run/desktop/mnt/host/i/backup/Ti_Camera/Immich,target=/data,readonly' docker.io/valkey/valkey:9 sh -c 'test -f /data/upload/.immich' 2>&1
                    $ready = $LASTEXITCODE -eq 0
                }
            } finally { $ErrorActionPreference = $oldPreference }
            if ($ready) { break }
        }
        Start-Sleep -Seconds 5
    }
    if (-not $ready) { throw 'Docker or the Immich network library did not become available; Immich was not started.' }
    & $docker compose -f $compose up -d
    if ($LASTEXITCODE -ne 0) { throw 'Immich compose failed to start.' }
} finally { Stop-Transcript | Out-Null }
