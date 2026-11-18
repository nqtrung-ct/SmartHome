$ErrorActionPreference = 'Stop'
$exe = 'D:\Syncthing\app\syncthing-windows-amd64-v2.1.7\syncthing.exe'
$syncthingHome = 'D:\Syncthing\config'
$marker = 'I:\Backup\Vo_Camera\.stfolder'
$share = '\\192.168.1.100\share'

for ($i = 0; $i -lt 180; $i++) {
    if (-not (Test-Path -LiteralPath $marker)) {
        $oldPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $null = & net.exe use 'I:' $share /persistent:yes 2>&1
        } finally { $ErrorActionPreference = $oldPreference }
    }
    if (Test-Path -LiteralPath $marker) {
        if (-not (Get-Process -Name 'syncthing' -ErrorAction SilentlyContinue)) {
            Start-Process -FilePath $exe -ArgumentList @('serve', '--no-browser', '--home', $syncthingHome, '--log-file', 'D:\Syncthing\logs\syncthing.log') -WindowStyle Hidden
        }
        exit 0
    }
    Start-Sleep -Seconds 5
}
throw 'The Syncthing network folder is unavailable; Syncthing was not started.'
