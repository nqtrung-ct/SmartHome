import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from server.api.admin import (
    CameraRecordingModeIn,
    get_camera_recording_mode,
    set_camera_recording_mode,
)
from server.db import Database


class FakeResponse:
    def __init__(self, payload=None):
        self.payload = payload or {"success": True}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeFrigateClient:
    config = {}
    calls = []

    def __init__(self, *args, **kwargs):
        self.calls = type(self).calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url):
        self.calls.append(("get", url))
        return FakeResponse(self.config)

    async def put(self, url, json):
        self.calls.append(("put", url, json))
        return FakeResponse()

    async def post(self, url):
        self.calls.append(("post", url))
        return FakeResponse()


class FrigateRecordingAdminTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.db.execute(
            "INSERT INTO devices(id,name,driver,host,created_at) VALUES (1,?,?,?,?)",
            ("Living room camera", "onvif", "192.0.2.10", "2026-11-11T12:00:00"),
        )
        self.db.execute(
            "INSERT INTO entities(id,device_id,name,entity_type,driver_data) VALUES (1,1,?,'camera','{}')",
            ("Living room camera",),
        )
        self.app = SimpleNamespace(state=SimpleNamespace(
            db=self.db,
            cfg=SimpleNamespace(get=lambda key, default=None: {
                "frigate.base_url": "http://frigate:5000",
            }.get(key, default)),
        ))
        self.request = SimpleNamespace(app=self.app)
        self.original_config = FakeFrigateClient.config
        self.original_calls = FakeFrigateClient.calls
        FakeFrigateClient.config = {
            "record": {
                "enabled": True,
                "motion": {"days": 3},
                "alerts": {"retain": {"days": 15, "mode": "all"}},
                "detections": {"retain": {"days": 10, "mode": "all"}},
            },
            "cameras": {
                "living_room": {
                    "ffmpeg": {"inputs": [{"path": "rtsp://192.0.2.10/stream"}]},
                    "objects": {"track": ["person"]},
                    "record": {
                        "enabled": True,
                        "motion": {"days": 3},
                        "alerts": {"retain": {"days": 15, "mode": "all"}},
                        "detections": {"retain": {"days": 10, "mode": "all"}},
                    },
                },
                "garage": {
                    "objects": {"track": ["car"]},
                    "record": {"enabled": True, "motion": {"days": 3}},
                },
            },
        }
        FakeFrigateClient.calls = []

    def tearDown(self):
        FakeFrigateClient.config = self.original_config
        FakeFrigateClient.calls = self.original_calls
        self.db.close()
        self.tmp.cleanup()

    async def test_get_maps_camera_by_host_and_returns_retention_mode(self):
        with patch("httpx.AsyncClient", FakeFrigateClient):
            result = await get_camera_recording_mode(self.request, 1, None)

        self.assertEqual(result["camera"], "living_room")
        self.assertTrue(result["cameras"][0]["motion_enabled"])
        self.assertTrue(result["cameras"][0]["person_enabled"])
        self.assertTrue(result["cameras"][0]["person_tracked"])
        self.assertEqual(result["cameras"][0]["retention_days"], 10)

    async def test_person_only_updates_camera_retention_and_restarts_frigate(self):
        with patch("httpx.AsyncClient", FakeFrigateClient):
            result = await set_camera_recording_mode(
                self.request, 1,
                CameraRecordingModeIn(
                    camera="living_room", motion_enabled=False, person_enabled=True,
                    retention_days=15,
                ), None,
            )

        update = next(call[2] for call in FakeFrigateClient.calls if call[0] == "put")
        record = update["config_data"]["cameras"]["living_room"]["record"]
        self.assertEqual(record["motion"]["days"], 0)
        self.assertEqual(record["alerts"]["retain"], {"days": 15, "mode": "motion"})
        self.assertEqual(record["detections"]["retain"]["days"], 0)
        self.assertTrue(result["restarting"])
        self.assertEqual(FakeFrigateClient.calls[-1][0], "post")
        self.assertEqual(result["retention_days"], 15)
        self.assertEqual(self.db.one("SELECT driver_data FROM entities WHERE id=1")["driver_data"],
                         '{"frigate": "living_room", "recording_retention_days": 15}')

    async def test_both_toggles_keep_motion_and_person_recordings(self):
        with patch("httpx.AsyncClient", FakeFrigateClient):
            await set_camera_recording_mode(
                self.request, 1,
                CameraRecordingModeIn(
                    camera="living_room", motion_enabled=True, person_enabled=True,
                    retention_days=10,
                ), None,
            )

        update = next(call[2] for call in FakeFrigateClient.calls if call[0] == "put")
        record = update["config_data"]["cameras"]["living_room"]["record"]
        self.assertEqual(record["motion"]["days"], 10)
        self.assertEqual(record["alerts"]["retain"], {"days": 10, "mode": "motion"})
        self.assertEqual(record["detections"]["retain"]["days"], 0)

    async def test_at_least_one_toggle_is_required(self):
        with patch("httpx.AsyncClient", FakeFrigateClient):
            with self.assertRaises(HTTPException) as raised:
                await set_camera_recording_mode(
                    self.request, 1,
                    CameraRecordingModeIn(
                        camera="living_room", motion_enabled=False, person_enabled=False,
                    ), None,
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertFalse(FakeFrigateClient.calls)

    async def test_motion_only_uses_selected_five_day_retention(self):
        with patch("httpx.AsyncClient", FakeFrigateClient):
            await set_camera_recording_mode(
                self.request, 1,
                CameraRecordingModeIn(
                    camera="living_room", motion_enabled=True, person_enabled=False,
                    retention_days=5,
                ), None,
            )

        update = next(call[2] for call in FakeFrigateClient.calls if call[0] == "put")
        record = update["config_data"]["cameras"]["living_room"]["record"]
        self.assertEqual(record["motion"]["days"], 5)
        self.assertEqual(record["alerts"]["retain"]["days"], 0)

    async def test_person_mode_requires_person_to_be_tracked(self):
        with patch("httpx.AsyncClient", FakeFrigateClient):
            with self.assertRaises(HTTPException) as raised:
                await set_camera_recording_mode(
                    self.request, 1,
                    CameraRecordingModeIn(
                        camera="garage", motion_enabled=False, person_enabled=True,
                    ), None,
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertFalse(any(call[0] == "put" for call in FakeFrigateClient.calls))


if __name__ == "__main__":
    unittest.main()
