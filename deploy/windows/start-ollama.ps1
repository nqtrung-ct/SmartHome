$ErrorActionPreference = 'Stop'
$env:OLLAMA_HOST = '0.0.0.0:11434'
$env:OLLAMA_KEEP_ALIVE = '-1'
$ollama = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
$url = 'http://127.0.0.1:11434'
try { $null = Invoke-RestMethod -Uri "$url/api/tags" -TimeoutSec 2; $ready = $true }
catch { $ready = $false }
if (-not $ready) {
    Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
}
for ($i = 0; $i -lt 60; $i++) {
    try {
        $null = Invoke-RestMethod -Uri "$url/api/tags" -TimeoutSec 2
        $body = @{ model = 'qwen3.5:9b'; prompt = ''; keep_alive = -1 } | ConvertTo-Json -Compress
        $null = Invoke-RestMethod -Uri "$url/api/generate" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 300
        exit 0
    } catch { Start-Sleep -Seconds 2 }
}
throw 'Ollama did not become ready or the model could not be loaded.'
