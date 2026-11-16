import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Response

from server.api.admin import DisplayCommandIn, display, display_command
from server.db import Database
from server.util import now


class DisplayTuyaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.db.execute("INSERT INTO rooms(id, name) VALUES (1, 'Phòng thử')")
        self.db.execute("INSERT INTO rooms(id, name, floor) VALUES (2, 'Ngoài', 'Ngoài nhà')")
        for device_id, driver in ((1, "tuya"), (2, "cozylife")):
            self.db.execute(
                "INSERT INTO devices(id, name, driver, room_id, created_at) VALUES (?, ?, ?, 1, '')",
                (device_id, driver, driver),
            )
        for entity_id, device_id, state in ((1, 1, 'on'), (2, 1, 'off'), (3, 2, 'on')):
            self.db.execute(
                "INSERT INTO entities(id, device_id, name, entity_type, state) VALUES (?, ?, ?, 'switch', ?)",
                (entity_id, device_id, f'Switch {entity_id}', '{"state":"' + state + '"}'),
            )
        self.db.execute("INSERT INTO entities(id, device_id, name, entity_type, room_id) VALUES (4, 1, 'Cầu dao', 'switch', 2)")
        self.db.execute("INSERT INTO entities(id, device_id, name, entity_type, room_id) VALUES (5, 1, 'Đèn ngoài', 'light', 2)")
        self.app = SimpleNamespace(state=SimpleNamespace(
            db=self.db, cfg=SimpleNamespace(get=lambda key, default=None: default),
            display_tokens={"test": now() + timedelta(minutes=1)},
        ))
        self.request = SimpleNamespace(app=self.app, headers={"X-Display-Token": "test"})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_snapshot_includes_indoor_switches_as_read_only(self):
        with patch("server.api.admin.solar_snapshot", return_value=None):
            result = asyncio.run(display(self.request, Response()))
        entities = result["rooms"][0]["entities"]
        self.assertEqual(len(result["rooms"]), 1)
        self.assertEqual([(e["id"], e["state"]["state"], e["controllable"]) for e in entities],
                         [(1, "on", False), (2, "off", False), (3, "on", False)])

    def test_display_command_rejects_switches(self):
        for entity_id in (1, 3):
            with self.subTest(entity_id=entity_id), self.assertRaises(HTTPException) as error:
                asyncio.run(display_command(self.request, DisplayCommandIn(token="test", entity=entity_id, command="off")))
            self.assertEqual(error.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
