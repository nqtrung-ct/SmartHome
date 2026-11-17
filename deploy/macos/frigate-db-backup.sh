#!/bin/sh
# Run inside Frigate so launchd does not need direct Data volume access.
set -eu
exec /usr/local/bin/docker exec -i smarthome-frigate python3 - <<'PY'
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

base = Path('/frigate-db')
db = base / 'frigate.db'
backups = base / 'backups'
if not db.is_file():
    raise SystemExit('Frigate database is not available')
backups.mkdir(exist_ok=True)
fd, name = tempfile.mkstemp(prefix='.snapshot.', dir=backups)
os.close(fd)
tmp = Path(name)
try:
    with sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=10) as source:
        with sqlite3.connect(tmp, timeout=10) as target:
            source.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')
            if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise RuntimeError('Frigate database snapshot failed integrity check')
    for old, new in (('previous.db', 'older.db'), ('latest.db', 'previous.db')):
        if (backups / old).is_file():
            os.replace(backups / old, backups / new)
    os.replace(tmp, backups / 'latest.db')
    print(f'Frigate database backup verified: {datetime.now():%Y-%m-%d %H:%M:%S}')
finally:
    tmp.unlink(missing_ok=True)
    Path(str(tmp) + '-wal').unlink(missing_ok=True)
    Path(str(tmp) + '-shm').unlink(missing_ok=True)
PY
