import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Response

from server.api.admin import DisplayUnlockIn, _display_role, _require_display_entity, display, display_logout, display_unlock
from server.db import Database


class DisplayRoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        for i, floor in ((1, "Tầng 1"), (2, "Tầng 2")):
            self.db.execute("INSERT INTO rooms(id, name, floor) VALUES (?, ?, ?)", (i, f"Room {i}", floor))
            self.db.execute("INSERT INTO devices(id, name, driver, room_id, created_at) VALUES (?, ?, 'broadlink', ?, '')", (i, f"Device {i}", i))
            self.db.execute("INSERT INTO entities(id, device_id, name, entity_type) VALUES (?, ?, ?, 'light')", (i, i, f"Light {i}"))
        pins = {"display.pin_floor1": "1234", "display.pin": "5678"}
        self.app = SimpleNamespace(state=SimpleNamespace(db=self.db, cfg=SimpleNamespace(get=lambda k, default=None: pins.get(k, default)), display_tokens={}))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def request(self, token=""):
        return SimpleNamespace(app=self.app, headers={"X-Display-Token": token} if token else {})

    def snapshot(self, token=""):
        with patch("server.api.admin.solar_snapshot", return_value=None):
            return asyncio.run(display(self.request(token), Response()))

    def test_role_snapshot_and_entity_access(self):
        self.assertEqual(self.snapshot()["rooms"], [])
        with patch("server.api.admin.log_event"):
            floor = asyncio.run(display_unlock(self.request(), DisplayUnlockIn(role="floor1", pin="1234")))["token"]
            admin = asyncio.run(display_unlock(self.request(), DisplayUnlockIn(role="admin", pin="5678")))["token"]
        self.assertEqual([r["id"] for r in self.snapshot(floor)["rooms"]], [1])
        self.assertEqual(len(self.snapshot(admin)["rooms"]), 2)
        _require_display_entity(self.app, floor, 1)
        with self.assertRaises(HTTPException) as error:
            _require_display_entity(self.app, floor, 2)
        self.assertEqual(error.exception.status_code, 403)
        _require_display_entity(self.app, admin, 2)
        self.assertNotIn("expires", self.app.state.display_tokens[floor])
        self.assertEqual(_display_role(self.app, floor), "floor1")
        asyncio.run(display_logout(self.request(floor)))
        with self.assertRaises(HTTPException) as error:
            self.snapshot(floor)
        self.assertEqual(error.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
