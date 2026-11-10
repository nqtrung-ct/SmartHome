import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from server.core.scheduler import _poll_solar, _poll_tuya_sensors
from server.db import Database
from server.drivers.base import DriverError
from server.drivers.tuya import TuyaDriver


class DevicePollingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.db = Database(self.path)
        self.cfg = SimpleNamespace(get=lambda key, default=None: {
            "solar.poll_seconds": 300,
        }.get(key, default))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def device(self, driver):
        self.db.execute(
            "INSERT INTO devices(id,name,driver,host,created_at) VALUES (1,?,?,?,?)",
            ("test", driver, "192.0.2.1", "2026-11-10T12:00:00"),
        )

    def entity(self, eid, kind, data, state):
        self.db.execute(
            "INSERT INTO entities(id,device_id,name,entity_type,driver_data,state) VALUES (?,1,?,?,?,?)",
            (eid, "test", kind, json.dumps(data), json.dumps(state)),
        )

    def test_tuya_error_never_turns_live_breaker_off_or_zeros_meter(self):
        self.device("tuya")
        self.entity(1, "switch", {"dps": 16}, {"state": "on"})
        self.entity(2, "sensor", {"cloud_meter": True}, {"voltage": 230, "power": 500})
        driver = TuyaDriver(self.db)
        dev = Mock()
        dev.status.return_value = {"Error": "Network Error", "Err": "901"}
        app = SimpleNamespace(state=SimpleNamespace(db=self.db, cfg=self.cfg, drivers={"tuya": driver}))
        with patch.object(driver, "_device", return_value=dev), patch(
                "server.core.scheduler.asyncio.sleep", new=AsyncMock()):
            asyncio.run(_poll_tuya_sensors(app))
        self.assertEqual(json.loads(self.db.one("SELECT state FROM entities WHERE id=1")["state"]), {"state": "on"})
        self.assertEqual(json.loads(self.db.one("SELECT state FROM entities WHERE id=2")["state"])["voltage"], 230)

    def test_tuya_requires_explicit_switch_channel(self):
        driver = TuyaDriver(self.db)
        entity = {"entity_type": "switch", "driver_data": '{"dps":16}'}
        for response in ({"1": 100}, {}, {"16": None}, {"16": "false"}):
            with self.subTest(response=response), patch.object(driver, "_status_dps", return_value=response):
                with self.assertRaises(DriverError):
                    driver.get_state(entity, {})
        for value, expected in ((False, "off"), (True, "on")):
            with patch.object(driver, "_status_dps", return_value={"16": value}):
                self.assertEqual(driver.get_state(entity, {}), {"state": expected})

    def test_inverter_failed_attempt_survives_restart_and_waits_five_minutes(self):
        self.device("senergy")
        self.entity(1, "sensor", {}, {"battery_soc": 60})
        driver = Mock()
        driver.get_state.side_effect = DriverError("offline")
        app = SimpleNamespace(state=SimpleNamespace(db=self.db, cfg=self.cfg, drivers={"senergy": driver}))
        with patch("server.core.scheduler.time.time", return_value=1000), patch(
                "server.core.scheduler._heal_device_ip", new=AsyncMock()):
            asyncio.run(_poll_solar(app))
        self.assertEqual(driver.get_state.call_count, 1)
        self.assertEqual(json.loads(self.db.one("SELECT state FROM entities WHERE id=1")["state"]), {"battery_soc": 60})
        self.db.close()
        self.db = Database(self.path)
        app.state.db = self.db
        with patch("server.core.scheduler.time.time", return_value=1179):
            asyncio.run(_poll_solar(app))
        self.assertEqual(driver.get_state.call_count, 1)
        driver.get_state.side_effect = None
        driver.get_state.return_value = {"battery_soc": 61}
        with patch("server.core.scheduler.time.time", return_value=1300):
            asyncio.run(_poll_solar(app))
        self.assertEqual(driver.get_state.call_count, 2)
        self.assertEqual(json.loads(self.db.one("SELECT state FROM entities WHERE id=1")["state"]), {"battery_soc": 61})

    def test_inverter_api_and_voice_use_snapshot_without_connecting_or_changing_age(self):
        from server.api.devices import entity_state
        from server.core.text_command import _set_stateless_read

        self.device("senergy")
        self.entity(1, "sensor", {}, {"battery_soc": 55})
        stamp = "2026-11-10T12:00:00"
        self.db.execute("UPDATE entities SET updated_at=? WHERE id=1", (stamp,))
        driver = Mock()
        entity = self.db.one("SELECT * FROM entities WHERE id=1")
        device = self.db.one("SELECT * FROM devices WHERE id=1")
        state = SimpleNamespace(db=self.db, drivers={"senergy": driver})
        request = SimpleNamespace(app=SimpleNamespace(state=state))
        self.assertEqual(asyncio.run(entity_state(request, 1)), {
            "state": {"battery_soc": 55}, "updated_at": stamp,
        })
        self.assertEqual(asyncio.run(_set_stateless_read(self.db, state.drivers, entity, device)), {"battery_soc": 55})
        driver.get_state.assert_not_called()
        self.assertEqual(self.db.one("SELECT updated_at FROM entities WHERE id=1")["updated_at"], stamp)


if __name__ == "__main__":
    unittest.main()
