import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.core.discovery import _cleanup_network_rows, scan_network
from server.db import Database


class DiscoveryTests(unittest.TestCase):
    def test_senergy_ip_heals_from_modbus_host_when_ping_misses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            db.execute(
                "INSERT INTO devices(id,name,driver,host,mac,created_at) "
                "VALUES (1,?,?,?,?,?)",
                ("Inverter", "senergy", "192.0.2.1", "A0:F2:62:90:9A:B0",
                 "2026-11-11T12:00:00"),
            )
            cfg = SimpleNamespace(get=lambda key, default=None: {
                "discovery.network_scan": True,
                "discovery.subnet": "192.0.2.0/30",
            }.get(key, default))

            def tcp_open(ip, port, timeout=0.5):
                return ip == "192.0.2.2" and port == 502

            with patch("server.core.discovery._ping", return_value=False), \
                    patch("server.core.discovery._tcp_open", side_effect=tcp_open), \
                    patch("server.core.discovery._arp_table", return_value={
                        "192.0.2.2": "A0:F2:62:90:9A:B0",
                    }):
                scan_network(db, cfg)
                _cleanup_network_rows(db)

            self.assertEqual(
                db.one("SELECT host FROM devices WHERE id=1")["host"],
                "192.0.2.2",
            )
            db.close()


if __name__ == "__main__":
    unittest.main()
