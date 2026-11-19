#!/bin/sh
set -eu
project=/Volumes/Data/Projects/SmartHome
for attempt in $(seq 1 180); do
  if mount | grep -q ' on /Volumes/Data ' && [ -f "$project/config.yaml" ] && [ -f "$project/data/smarthome.db" ]; then
    cd "$project"
    "$project/.venv/bin/python3" "$project/deploy/macos/configure-ollama-lan.py"
    exec "$project/.venv/bin/python3" -X utf8 server.py
  fi
  sleep 5
done
exit 1
