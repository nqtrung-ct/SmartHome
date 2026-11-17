#!/bin/sh
set -eu

root="$(cd "$(dirname "$0")/../.." && pwd)"
label="dev.smarthome.frigate-db-backup"
agent="$HOME/Library/LaunchAgents/$label.plist"
runtime="$HOME/Library/SmartHome/frigate-db-backup.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/SmartHome" "$HOME/Library/Logs"
cp "$root/deploy/macos/frigate-db-backup.sh" "$runtime"
chmod 700 "$runtime"
cat > "$agent" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>$runtime</string></array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>900</integer>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/SmartHome-frigate-db-backup.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/SmartHome-frigate-db-backup.err.log</string>
</dict></plist>
EOF
plutil -lint "$agent"
uid="$(id -u)"
launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$uid" "$agent"
launchctl enable "gui/$uid/$label"
echo "Installed $label (every 15 minutes after login)"
