$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$dockerDir = 'C:\Program Files\Docker\Docker\resources\bin'
$docker = Join-Path $dockerDir 'docker.exe'
$env:PATH = "$dockerDir;$env:PATH"
$logDir = 'D:\SmartHome\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Start-Transcript -Path (Join-Path $logDir 'startup.log') -Append | Out-Null
function Test-DockerReady {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $null = & $docker info --format '{{.ServerVersion}}' 2>&1
        return $LASTEXITCODE -eq 0
    } finally { $ErrorActionPreference = $previousPreference }
}
try {
    if (-not (Get-NetTCPConnection -LocalPort 8124 -State Listen -ErrorAction SilentlyContinue)) {
        Start-Process -FilePath (Join-Path $project '.venv\Scripts\python.exe') -ArgumentList '-X','utf8','server.py' -WorkingDirectory $project -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'server.out.log') -RedirectStandardError (Join-Path $logDir 'server.err.log')
    }
    $marker = 'E:\Camera\.smarthome-camera-volume'
    $markerValue = 'SmartHome-Camera-E-20261118'
    if (-not (Test-DockerReady)) {
        Start-Process -FilePath 'C:\Program Files\Docker\Docker\Docker Desktop.exe' -WindowStyle Hidden
    }
    $dockerReady = $false
    for ($i = 0; $i -lt 120; $i++) {
        if (Test-DockerReady) { $dockerReady = $true; break }
        Start-Sleep -Seconds 3
    }
    if ($dockerReady) {
        $cameraReady = $false
        for ($i = 0; $i -lt 180; $i++) {
            try {
                $cameraReady = (Test-Path -LiteralPath $marker) -and ((Get-Content -Raw -LiteralPath $marker).Trim() -eq $markerValue)
            } catch { $cameraReady = $false }
            if ($cameraReady) { break }
            if ($i -eq 0) { Write-Warning 'Waiting for the Camera volume before starting Frigate.' }
            Start-Sleep -Seconds 5
        }
        if (-not $cameraReady) { throw 'Camera volume missing or incorrect; Frigate was not started.' }
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $seen = (& $docker run --rm --mount 'type=bind,source=E:/Camera,target=/camera,readonly' docker.io/valkey/valkey:9 sh -c 'cat /camera/.smarthome-camera-volume' 2>$null | Out-String).Trim()
            $mountReady = $LASTEXITCODE -eq 0
        } finally { $ErrorActionPreference = $previousPreference }
        if ($mountReady -and $seen -eq $markerValue) {
            & $docker compose -f (Join-Path $project 'data\frigate\docker-compose.yml') up -d
            if ($LASTEXITCODE -ne 0) { Write-Warning 'Failed to start Frigate.' }
        } else { Write-Warning 'Docker cannot see the Camera volume; Frigate will not start.' }
    } elseif (-not $dockerReady) { Write-Warning 'Docker engine is unavailable.' }

} finally { Stop-Transcript | Out-Null }
