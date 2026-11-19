#!/bin/sh
set -eu
config=/Volumes/Data/syncthing/config/config.xml
sed -i '' 's|path="I:.*Vo_Camera"|path="/Volumes/SHARE/Backup/Vo_Camera"|' "$config"
grep -q 'path="/Volumes/SHARE/Backup/Vo_Camera"' "$config"
