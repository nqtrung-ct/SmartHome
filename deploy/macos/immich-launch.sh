#!/bin/sh
set -eu
docker="$HOME/.orbstack/bin/docker"
open -gj -a OrbStack >/dev/null 2>&1 || true
for attempt in $(seq 1 180); do
  if mount | grep -q ' on /Volumes/Data ' && mount | grep -q ' on /Volumes/SHARE ' && [ -f /Volumes/SHARE/Backup/Ti_Camera/Immich/upload/.immich ] && [ -d /Volumes/Data/immich/postgres-return-20261119 ] && "$docker" info >/dev/null 2>&1; then
    cd /Volumes/Data/immich
    exec "$docker" compose up -d
  fi
  sleep 5
done
exit 1
