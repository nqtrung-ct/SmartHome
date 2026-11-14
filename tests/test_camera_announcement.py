import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from server.core.camera_talk import announce
from server.core.text_command import fast_path
from server.core.scheduler import scheduler_loop
from server.db import Database


class CameraAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.db.execute("INSERT INTO rooms(id,name,aliases) VALUES (1,'Phòng khách',''),(2,'Nhà bếp','')")
        self.db.execute("INSERT INTO devices(id,name,driver,host,room_id,created_at) VALUES (1,'Cam bếp','onvif','192.0.2.1',2,'now')")
        self.db.execute("INSERT INTO entities(id,device_id,name,entity_type,aliases) VALUES (20,1,'Camera nhà bếp','camera','camera bếp')")
        self.db.execute("INSERT INTO xiaozhi_devices(id,device_id,name,room_id,token,approved,created_at) VALUES (30,'esp1','ESP khách',1,'test1',1,'now'),(31,'esp2','ESP khác',1,'test2',1,'now')")
        self.app = SimpleNamespace(state=SimpleNamespace(db=self.db, cfg=object(), pipeline=SimpleNamespace(tts_audio=AsyncMock(return_value=(b'wav', 22050)))))

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_immediate_command_extracts_exact_speech_and_camera(self):
        with patch("server.core.camera_talk.announce", new_callable=AsyncMock, return_value="camera") as send:
            result = await fast_path(self.db, {}, None,
                                     "phát câu đến giờ học rồi đến camera nhà bếp", 1,
                                     app=self.app, source_device_id=30)
        self.assertTrue(result["action"]["ok"])
        send.assert_awaited_once_with(self.app, 2, "đến giờ học rồi", 20, 30)

    async def test_message_may_include_camera_when_destination_is_later(self):
        with patch("server.core.camera_talk.announce", new_callable=AsyncMock, return_value="camera") as send:
            await fast_path(self.db, {}, None, "phát câu thử phát qua camera nhà bếp đến camera nhà bếp", 1,
                            app=self.app, source_device_id=30)
        send.assert_awaited_once_with(self.app, 2, "thử phát qua camera nhà bếp", 20, 30)

    async def test_timer_preserves_camera_and_speaking_esp(self):
        result = await fast_path(self.db, {}, None,
                                 "phát câu đến giờ học rồi đến camera nhà bếp sau 1 phút", 1,
                                 app=self.app, source_device_id=30)
        self.assertTrue(result["action"]["ok"])
        timer = self.db.one("SELECT * FROM timers")
        self.assertEqual(timer["room_id"], 2)
        self.assertEqual(timer["message"], "raw:đến giờ học rồi")
        self.assertEqual(json.loads(timer["action"]), {"kind": "announcement", "camera_id": 20, "source_device_id": 30})

    async def test_timer_before_camera_destination(self):
        result = await fast_path(self.db, {}, None,
                                 "phát câu đến giờ học rồi sau 1 phút đến camera nhà bếp", 1,
                                 app=self.app, source_device_id=30)
        self.assertTrue(result["action"]["ok"])
        timer = self.db.one("SELECT * FROM timers")
        self.assertEqual(timer["message"], "raw:đến giờ học rồi")
        self.assertEqual(json.loads(timer["action"])["camera_id"], 20)

    async def test_no_camera_falls_back_only_to_originating_esp(self):
        with patch("server.api.xiaozhi.connections.enqueue_announce") as enqueue:
            result = await announce(self.app, 1, "Câu thử", source_device_id=30)
        self.assertEqual(result, "esp")
        enqueue.assert_called_once_with(30, "Câu thử")

    async def test_camera_does_not_also_announce_to_esp(self):
        with patch("server.core.camera_talk.imou_talk.speak") as speak, \
                patch("server.api.xiaozhi.connections.enqueue_announce") as enqueue:
            result = await announce(self.app, 2, "Câu thử", camera_id=20, source_device_id=30)
        self.assertEqual(result, "camera")
        speak.assert_called_once()
        enqueue.assert_not_called()

    async def test_camera_failure_falls_back_to_originating_esp(self):
        with patch("server.core.camera_talk.imou_talk.speak", side_effect=OSError("offline")), \
                patch("server.api.xiaozhi.connections.enqueue_announce") as enqueue:
            result = await announce(self.app, 2, "Câu thử", camera_id=20, source_device_id=30)
        self.assertEqual(result, "esp")
        enqueue.assert_called_once_with(30, "Câu thử")

    async def test_unknown_camera_does_not_pick_a_different_camera(self):
        result = await fast_path(self.db, {}, None, "phát câu xin chào đến camera sân thượng", 2,
                                 app=self.app, source_device_id=30)
        self.assertFalse(result["action"]["ok"])
        self.assertIn("không tìm thấy", result["reply"].lower())

    async def test_named_camera_wins_over_another_in_same_room(self):
        self.db.execute("INSERT INTO devices(id,name,driver,host,room_id,created_at) VALUES (2,'Cam tủ','onvif','192.0.2.2',2,'now')")
        self.db.execute("INSERT INTO entities(id,device_id,name,entity_type,aliases) VALUES (21,2,'Camera tủ bếp','camera','')")
        with patch("server.core.camera_talk.announce", new_callable=AsyncMock, return_value="camera") as send:
            result = await fast_path(self.db, {}, None, "phát câu xin chào đến camera tủ bếp", 1,
                                     app=self.app, source_device_id=30)
        self.assertTrue(result["action"]["ok"])
        send.assert_awaited_once_with(self.app, 2, "xin chào", 21, 30)

    async def test_current_room_announcement_uses_receiving_esp_if_no_camera(self):
        with patch("server.api.xiaozhi.connections.enqueue_announce") as enqueue:
            result = await fast_path(self.db, {}, None, "phát câu đến giờ học rồi", 1,
                                     app=self.app, source_device_id=30)
        self.assertTrue(result["action"]["ok"])
        enqueue.assert_called_once_with(30, "đến giờ học rồi")

    async def test_due_timer_dispatches_recorded_camera_and_origin(self):
        await fast_path(self.db, {}, None,
                        "phát câu đến giờ học rồi đến camera nhà bếp sau 1 phút", 1,
                        app=self.app, source_device_id=30)
        self.db.execute("UPDATE timers SET target_at='2000-01-01T00:00:00'")
        self.app.state.cfg = SimpleNamespace(get=lambda key, default=None: default)
        self.app.state.announcement_tasks = set()
        with patch("server.core.camera_talk.announce", new_callable=AsyncMock, return_value="camera") as send, \
                patch("server.core.scheduler._poll_tuya_sensors", new_callable=AsyncMock), \
                patch("server.core.scheduler._poll_tuya_cloud", new_callable=AsyncMock), \
                patch("server.core.scheduler._poll_solar", new_callable=AsyncMock), \
                patch("server.core.scheduler._poll_cozylife", new_callable=AsyncMock), \
                patch("server.core.scheduler._poll_androidtv", new_callable=AsyncMock):
            task = asyncio.create_task(scheduler_loop(self.app))
            try:
                await asyncio.sleep(1.2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        send.assert_awaited_once_with(self.app, 2, "đến giờ học rồi", 20, 30)
        self.assertEqual(self.db.one("SELECT done FROM timers")["done"], 1)


if __name__ == "__main__":
    unittest.main()
