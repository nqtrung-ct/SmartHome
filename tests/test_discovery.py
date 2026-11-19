import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.core.discovery import _cleanup_network_rows, scan_androidtv, scan_network, upsert
from server.api.discovery_api import AdoptIn, adopt
from server.db import Database
from server.drivers.base import Driver
from server.util import now_iso


class CameraAdoptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_onvif_adoption_creates_camera_and_stores_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            db.execute("INSERT INTO rooms(id,name) VALUES (?,?)", (9, "Phòng sinh hoạt chung"))
            db.execute(
                "INSERT INTO discovered(method,ident,ip,mac,name,raw,first_seen,last_seen) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("wsdiscovery", "camera-1", "192.0.2.10", "AA:BB:CC:DD:EE:FF",
                 "Camera", "{}", "2026-11-12", "2026-11-12"),
            )
            cfg = SimpleNamespace()
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
                db=db, cfg=cfg, drivers={"generic": Driver()},
            )))
            with patch("server.api.discovery_api.store_secret") as store:
                result = await adopt(request, 1, AdoptIn(room_id=9, rtsp_password="test"))
            entity = db.one("SELECT * FROM entities WHERE id=?", (result["entities"][0],))
            self.assertEqual((entity["entity_type"], entity["name"]), ("camera", "Camera"))
            self.assertEqual(db.one("SELECT room_id FROM devices WHERE id=?", (result["device_id"],))["room_id"], 9)
            self.assertEqual(store.call_count, 2)
            self.assertTrue(all(call.args[0] is cfg for call in store.call_args_list))
            self.assertIsNone(result["warning"])
            db.close()


class DiscoveryTests(unittest.TestCase):
    def test_stale_specific_row_does_not_hide_current_network_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            db.execute("INSERT INTO devices(id,name,driver,host,mac,created_at) VALUES (1,?,?,?,?,?)",
                       ("Camera", "onvif", "192.0.2.1", "AA:BB:CC:DD:EE:FF", now_iso()))
            db.execute("INSERT INTO discovered(method,ident,ip,mac,first_seen,last_seen) VALUES (?,?,?,?,?,?)",
                       ("androidtv", "old", "192.0.2.2", "11:22:33:44:55:66", "2020-01-01", "2020-01-01"))
            db.execute("INSERT INTO discovered(method,ident,ip,mac,first_seen,last_seen) VALUES (?,?,?,?,?,?)",
                       ("network", "192.0.2.2", "192.0.2.2", "AA:BB:CC:DD:EE:FF", now_iso(), now_iso()))
            _cleanup_network_rows(db)
            self.assertEqual(db.one("SELECT host FROM devices WHERE id=1")["host"], "192.0.2.2")
            db.close()

    def test_cast_id_is_not_network_mac_and_keeps_link_on_ip_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            db.execute("INSERT INTO devices(id,name,driver,host,mac,created_at) VALUES (1,?,?,?,?,?)",
                       ("TV", "androidtv", "192.0.2.1", "C4:8B:66:0B:76:25", now_iso()))
            upsert(db, "androidtv", "EE:C6:53:1B:39:D6", ip="192.0.2.1", mac="C4:8B:66:0B:76:25")
            db.execute("UPDATE discovered SET adopted_device_id=1 WHERE method='androidtv'")
            upsert(db, "ssdp", "cast", ip="192.0.2.2", raw={"server": "Chromecast"})
            cfg = SimpleNamespace(get=lambda key, default=None: 0 if key == "discovery.mdns_seconds" else default)
            desc = {"friendlyName": "Smart TV Pro", "manufacturer": "TCL", "modelName": "Smart TV Pro",
                    "UDN": "uuid:6cf971e9-5a7f-138b-c090-eec6531b39d6"}
            with patch.dict("sys.modules", {"zeroconf": None}), \
                    patch("server.core.discovery._port_open", return_value=True), \
                    patch("server.core.discovery._cast_desc", return_value=desc):
                scan_androidtv(db, cfg)
            row = db.one("SELECT ip,mac,adopted_device_id FROM discovered WHERE method='androidtv'")
            self.assertEqual((row["ip"], row["mac"], row["adopted_device_id"]), ("192.0.2.2", "", 1))
            self.assertEqual(db.one("SELECT host FROM devices WHERE id=1")["host"], "192.0.2.2")
            db.close()

    def test_duplicate_device_macs_do_not_move_either_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            for device_id in (1, 2):
                db.execute("INSERT INTO devices(id,name,driver,host,mac,created_at) VALUES (?,?,?,?,?,?)",
                           (device_id, "Device", "generic", f"192.0.2.{device_id}", "AA:BB:CC:DD:EE:FF", now_iso()))
            db.execute("INSERT INTO discovered(method,ident,ip,mac,first_seen,last_seen) VALUES (?,?,?,?,?,?)",
                       ("network", "192.0.2.3", "192.0.2.3", "AA:BB:CC:DD:EE:FF", now_iso(), now_iso()))
            _cleanup_network_rows(db)
            self.assertEqual([r["host"] for r in db.query("SELECT host FROM devices ORDER BY id")],
                             ["192.0.2.1", "192.0.2.2"])
            self.assertIsNone(db.one("SELECT adopted_device_id FROM discovered")["adopted_device_id"])
            db.close()

    def test_camera_recovers_incorrect_device_mac_from_linked_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            db.execute("INSERT INTO devices(id,name,driver,host,mac,created_at) VALUES (1,?,?,?,?,?)",
                       ("Camera", "onvif", "192.0.2.1", "11:22:33:44:55:66", now_iso()))
            for ip, linked, stamp in [("192.0.2.1", 1, "2020-01-01"),
                                      ("192.0.2.2", None, now_iso())]:
                db.execute("INSERT INTO discovered(method,ident,ip,mac,raw,first_seen,last_seen,adopted_device_id) VALUES (?,?,?,?,?,?,?,?)",
                           ("wsdiscovery", ip, ip, "AA:BB:CC:DD:EE:FF",
                            '{"address":"urn:uuid:1234-aabbccddeeff"}', stamp, stamp, linked))
            _cleanup_network_rows(db)
            dev = db.one("SELECT host,mac FROM devices WHERE id=1")
            self.assertEqual((dev["host"], dev["mac"]), ("192.0.2.2", "AA:BB:CC:DD:EE:FF"))
            self.assertEqual(db.one("SELECT adopted_device_id FROM discovered WHERE ip='192.0.2.2'")["adopted_device_id"], 1)
            db.close()

    def test_changed_ip_does_not_keep_borrowed_mac(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            upsert(db, "mdns", "service", ip="192.0.2.1", mac="AA:BB:CC:DD:EE:FF")
            upsert(db, "mdns", "service", ip="192.0.2.2")
            self.assertEqual(db.one("SELECT mac FROM discovered")["mac"], "")
            db.close()

    def test_scanner_borrows_mac_before_network_row_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            db.execute("INSERT INTO devices(id,name,driver,host,mac,created_at) VALUES (1,?,?,?,?,?)",
                       ("TV", "androidtv", "192.0.2.1", "aa-bb-cc-dd-ee-ff", now_iso()))
            for method, ident, mac in [("androidtv", "tv", ""),
                                       ("network", "192.0.2.2", "AA:BB:CC:DD:EE:FF")]:
                db.execute("INSERT INTO discovered(method,ident,ip,mac,first_seen,last_seen) VALUES (?,?,?,?,?,?)",
                           (method, ident, "192.0.2.2", mac, now_iso(), now_iso()))
            _cleanup_network_rows(db)
            self.assertEqual(db.one("SELECT host FROM devices WHERE id=1")["host"], "192.0.2.2")
            row = db.one("SELECT mac, adopted_device_id FROM discovered WHERE method='androidtv'")
            self.assertEqual((row["mac"], row["adopted_device_id"]), ("AA:BB:CC:DD:EE:FF", 1))
            self.assertIsNone(db.one("SELECT id FROM discovered WHERE method='network'"))
            db.close()

    def test_macos_arp_entries_with_short_octets(self):
        from server.core.discovery import _arp_table
        output = "? (192.0.2.2) at a0:f2:62:0:9a:b0 on en0 ifscope [ethernet]\n"
        with patch("server.core.discovery.subprocess.run", return_value=SimpleNamespace(stdout=output)):
            self.assertEqual(_arp_table(), {"192.0.2.2": "A0:F2:62:00:9A:B0"})

    def test_macos_arp_falls_back_to_independent_launchd_job(self):
        from server.core.discovery import _arp_table
        output = "? (192.0.2.2) at a0:f2:62:90:9a:b0 on en0\n"
        with patch("server.core.discovery.sys.platform", "darwin"), \
                patch("server.core.discovery.subprocess.run", return_value=SimpleNamespace(stdout="")), \
                patch("server.core.discovery._macos_arp_from_launchd", return_value=output) as fallback:
            self.assertEqual(_arp_table(), {"192.0.2.2": "A0:F2:62:90:9A:B0"})
            fallback.assert_called_once()

    def test_old_discovery_does_not_revert_current_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            db.execute("INSERT INTO devices(id,name,driver,host,mac,created_at) VALUES (1,?,?,?,?,?)",
                       ("Inverter", "senergy", "192.0.2.2", "AA:BB:CC:DD:EE:FF", "2026-01-01"))
            db.execute("INSERT INTO discovered(method,ident,ip,mac,first_seen,last_seen,adopted_device_id) VALUES (?,?,?,?,?,?,?)",
                       ("senergy", "inverter", "192.0.2.1", "AA:BB:CC:DD:EE:FF", "2020-01-01", "2020-01-01", 1))
            _cleanup_network_rows(db)
            self.assertEqual(db.one("SELECT host FROM devices WHERE id=1")["host"], "192.0.2.2")
            db.close()

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
