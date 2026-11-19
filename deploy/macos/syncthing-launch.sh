#!/bin/sh
set -eu
for attempt in $(seq 1 180); do
  if mount | grep -q ' on /Volumes/Data ' && mount | grep -q ' on /Volumes/SHARE ' && [ -d /Volumes/SHARE/Backup/Vo_Camera/.stfolder ]; then
    exec /opt/homebrew/bin/syncthing serve --no-browser --home /Volumes/Data/syncthing/config --log-file /Users/ti/Library/Logs/SmartHome-syncthing.log
  fi
  sleep 5
done
exit 1
