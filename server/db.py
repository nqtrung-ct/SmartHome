"""SQLite storage. One file under data/, WAL mode, safe across threads."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  floor TEXT NOT NULL DEFAULT '',
  aliases TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS floors(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS devices(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  driver TEXT NOT NULL,
  host TEXT NOT NULL DEFAULT '',
  mac TEXT NOT NULL DEFAULT '',
  extra TEXT NOT NULL DEFAULT '{}',
  room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  entity_type TEXT NOT NULL,          -- switch | light | climate | sensor | camera | controller (hệ thống, không chọn trong UI)
  aliases TEXT NOT NULL DEFAULT '',
  room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,  -- NULL = theo phòng của thiết bị
  driver_data TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS voice_clients(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  token TEXT NOT NULL UNIQUE,
  room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  last_seen TEXT
);
CREATE TABLE IF NOT EXISTS pairing_codes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS timers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_at TEXT NOT NULL,
  room_id INTEGER REFERENCES rooms(id) ON DELETE CASCADE,
  message TEXT NOT NULL,
  done INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovered(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  method TEXT NOT NULL,               -- broadlink | tuya | zigbee | mdns | ssdp | wsdiscovery | network
  ident TEXT NOT NULL,
  ip TEXT NOT NULL DEFAULT '',
  mac TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  brand TEXT NOT NULL DEFAULT '',
  supported INTEGER NOT NULL DEFAULT 0,
  raw TEXT NOT NULL DEFAULT '{}',
  adopted_device_id INTEGER,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  UNIQUE(method, ident)
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'info',
  message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_device ON entities(device_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        floors_existed = bool(
            self.one("SELECT name FROM sqlite_master WHERE type='table' AND name='floors'")
        )
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate(seed_floors=not floors_existed)

    def _migrate(self, seed_floors: bool = False) -> None:
        """Lightweight migrations for databases created before an upgrade."""
        cols = {row["name"] for row in self.query("PRAGMA table_info(rooms)")}
        if "floor" not in cols:
            self.execute("ALTER TABLE rooms ADD COLUMN floor TEXT NOT NULL DEFAULT ''")
        ecols = {row["name"] for row in self.query("PRAGMA table_info(entities)")}
        if "room_id" not in ecols:
            self.execute(
                "ALTER TABLE entities ADD COLUMN room_id INTEGER "
                "REFERENCES rooms(id) ON DELETE SET NULL"
            )
        if seed_floors:
            # first run with the floors table: sensible defaults + keep any floor
            # names already assigned to rooms
            names = ["Trệt", "Tầng 1", "Tầng 2", "Tầng 3", "Tầng 4", "Tầng 5", "Mái / Sân thượng"]
            for row in self.query("SELECT DISTINCT floor FROM rooms WHERE floor != ''"):
                if row["floor"] not in names:
                    names.append(row["floor"])
            for sort, name in enumerate(names):
                self.execute("INSERT INTO floors(name, sort) VALUES (?,?)", (name, sort))

    @contextmanager
    def conn(self):
        with self._lock:
            yield self._conn
            self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.conn() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid

    def close(self) -> None:
        self._conn.close()


def log_event(db: Database, source: str, message: str, level: str = "info") -> None:
    from server.util import now_iso

    db.execute(
        "INSERT INTO events(ts, source, level, message) VALUES (?,?,?,?)",
        (now_iso(), source, level, message),
    )


def cleanup_old_rows(db: Database, days: int) -> dict[str, int]:
    """Drop stale rows: events older than `days`, pairing codes used/expired
    over a day ago, timers done over a week ago. Returns deleted counts."""
    from datetime import datetime, timedelta

    from server.util import now_iso

    events_before = datetime.now() - timedelta(days=days)
    day_ago = datetime.now() - timedelta(days=1)
    week_ago = datetime.now() - timedelta(days=7)
    counts: dict[str, int] = {}
    with db.conn() as c:
        for table, where, cutoff in (
            ("events", "ts < ?", events_before.isoformat(timespec="seconds")),
            (
                "pairing_codes",
                "(used=1 OR expires_at < ?) AND expires_at < ?",
                (now_iso(), day_ago.isoformat(timespec="seconds")),
            ),
            (
                "timers",
                "done=1 AND target_at < ?",
                week_ago.isoformat(timespec="seconds"),
            ),
        ):
            if isinstance(cutoff, tuple):
                cur = c.execute(f"DELETE FROM {table} WHERE {where}", cutoff)
            else:
                cur = c.execute(f"DELETE FROM {table} WHERE {where}", (cutoff,))
            if cur.rowcount:
                counts[table] = cur.rowcount
    return counts
