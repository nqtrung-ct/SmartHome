#!/bin/sh
set -eu
project=/Volumes/Data/Projects/SmartHome
docker="$HOME/.orbstack/bin/docker"
compose="$project/deploy/macos/docker-compose.return.yml"
open -gj -a OrbStack >/dev/null 2>&1 || true
until mount | grep -q ' on /Volumes/Data ' && "$docker" info >/dev/null 2>&1; do sleep 5; done
"$docker" compose -f "$compose" up -d mosquitto
while :; do
  if mount | grep -q ' on /Volumes/SHARE ' && [ -f /Volumes/SHARE/Backup/Camera/.smarthome-frigate-volume ] && \
    "$docker" run --rm --mount 'type=bind,source=/Volumes/SHARE/Backup/Camera,target=/camera,readonly' \
      docker.io/valkey/valkey:9 sh -c 'test "$(cat /camera/.smarthome-frigate-volume)" = SmartHome-Frigate-SHARE-20261119' >/dev/null 2>&1; then
    "$project/.venv/bin/python3" "$project/deploy/macos/update-frigate-ip.py"
    "$docker" compose -f "$compose" up -d frigate
    exit 0
  fi
  sleep 10
done
