import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from server.api.command_api import overview
from server.core.text_command import fast_path
from server.db import Database


class OverviewVisionTests(unittest.TestCase):
    def test_vision_states_are_hidden_without_deleting_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            try:
                db.execute("INSERT INTO rooms(id, name) VALUES (1, 'Phòng khách')")
                db.execute("INSERT INTO devices(id, name, driver, room_id, created_at) VALUES (1, 'Camera', 'onvif', 1, '')")
                for eid, name, kind, data in (
                    (1, "Camera", "camera", {"vision": [{"model": "Cửa chính"}]}),
                    (2, "Cửa chính", "sensor", {"vision": True}),
                    (3, "TV", "sensor", {"vision": True}),
                    (4, "Nhiệt độ", "sensor", {}),
                ):
                    db.execute("INSERT INTO entities(id, device_id, name, entity_type, driver_data) VALUES (?, 1, ?, ?, ?)",
                               (eid, name, kind, json.dumps(data)))
                request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db, drivers={})))
                result = asyncio.run(overview(request))
                self.assertEqual([e["name"] for e in result["rooms"][0]["entities"]], ["Camera", "Nhiệt độ"])
                self.assertEqual(db.one("SELECT COUNT(*) AS n FROM entities")["n"], 4)
            finally:
                db.close()

    def test_tv_on_or_off_question_uses_vision(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            try:
                db.execute("INSERT INTO rooms(id, name) VALUES (1, 'Phòng khách')")
                db.execute("INSERT INTO devices(id, name, driver, room_id, created_at) VALUES (1, 'Camera', 'onvif', 1, '')")
                db.execute("INSERT INTO entities(id, device_id, name, entity_type, driver_data) VALUES (1, 1, 'TV', 'sensor', ?)",
                           (json.dumps({"vision": True}),))
                answer = {"reply": "Đã kiểm tra", "action": {"tool": "vision_state"}}
                with patch("server.core.text_command._vision_answer", new=AsyncMock(return_value=answer)) as check:
                    result = asyncio.run(fast_path(db, {}, None, "TV phòng khách đang bật hay tắt?", 1))
                self.assertEqual(result, answer)
                check.assert_awaited_once()
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
