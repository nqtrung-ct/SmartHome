#!/bin/zsh
# Install/update the per-user macOS LaunchAgent for SmartHome.
# Run this once on the Mac from the repository checkout.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL="dev.smarthome"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$PROJECT_ROOT/Logs"
PLIST="$AGENTS_DIR/$LABEL.plist"
UID_VALUE="$(id -u)"
RUNTIME_DIR="$HOME/Library/SmartHome"
RUNTIME_SCRIPT="$RUNTIME_DIR/smarthome-launch.sh"
DETECTOR_LABEL="dev.smarthome.frigate-detector"
DETECTOR_ROOT="$HOME/Library/SmartHome/apple-silicon-detector"
DETECTOR_PLIST="$AGENTS_DIR/$DETECTOR_LABEL.plist"

mkdir -p "$AGENTS_DIR" "$LOG_DIR" "$RUNTIME_DIR"
cp "$PROJECT_ROOT/deploy/macos/smarthome-launch.sh" "$RUNTIME_SCRIPT"
chmod +x "$RUNTIME_SCRIPT"

# Unload an older copy if present; bootout returns non-zero when it was absent.
launchctl bootout "gui/$UID_VALUE/$LABEL" >/dev/null 2>&1 || true

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>$RUNTIME_SCRIPT</string>
    <string>$PROJECT_ROOT</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/Applications/OrbStack.app/Contents/MacOS:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/SmartHome-launchd.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/SmartHome-launchd.err.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST"
launchctl bootstrap "gui/$UID_VALUE" "$PLIST"
launchctl enable "gui/$UID_VALUE/$LABEL"
launchctl kickstart -k "gui/$UID_VALUE/$LABEL"

# Install the detector too when the optional native detector checkout exists.
# This replaces the old sample plist that contained a hard-coded username.
if [[ -x "$DETECTOR_ROOT/venv/bin/python3" && -f "$DETECTOR_ROOT/detector/zmq_onnx_client.py" ]]; then
  launchctl bootout "gui/$UID_VALUE/$DETECTOR_LABEL" >/dev/null 2>&1 || true
  cat > "$DETECTOR_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$DETECTOR_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DETECTOR_ROOT/venv/bin/python3</string>
    <string>$DETECTOR_ROOT/detector/zmq_onnx_client.py</string>
    <string>--model</string>
    <string>AUTO</string>
    <string>--endpoint</string>
    <string>tcp://127.0.0.1:5555</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/SmartHome-detector.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/SmartHome-detector.err.log</string>
</dict>
</plist>
EOF
  plutil -lint "$DETECTOR_PLIST"
  launchctl bootstrap "gui/$UID_VALUE" "$DETECTOR_PLIST"
  launchctl enable "gui/$UID_VALUE/$DETECTOR_LABEL"
  launchctl kickstart -k "gui/$UID_VALUE/$DETECTOR_LABEL"
  print "Frigate detector autostart installed: $DETECTOR_LABEL"
fi

print "SmartHome autostart installed: $LABEL"
print "Logs: $LOG_DIR"
