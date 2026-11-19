#!/bin/sh
set -eu
docker="$HOME/.orbstack/bin/docker"
for attempt in $(seq 1 30); do
  if "$docker" exec immich_postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then break; fi
  sleep 2
done
"$docker" cp /Volumes/Data/immich/immich-mac-migration.dump immich_postgres:/tmp/migration.dump
"$docker" exec immich_postgres sh -c 'pg_restore --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB" /tmp/migration.dump'
"$docker" exec immich_postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select count(*) from asset"'
