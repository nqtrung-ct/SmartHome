import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server.core.text_command import fast_path
from server.db import Database


class ClimateTemperatureCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.db.execute("INSERT INTO rooms(id,name,aliases) VALUES (1,'Phòng ngủ',''),(2,'Phòng khách','')")
        self.db.execute("INSERT INTO devices(id,name,driver,host,room_id,created_at) VALUES (1,'Điều hòa','test','',1,'now'),(2,'Cảm biến','test','',1,'now'),(3,'Cảm biến khách','test','',2,'now')")
        self.db.execute("INSERT INTO entities(id,device_id,name,entity_type,aliases) VALUES (1,1,'Điều hòa','climate','máy lạnh'),(2,2,'Nhiệt độ','sensor','nhiệt độ'),(3,3,'Nhiệt độ','sensor','nhiệt độ')")

    async def asyncTearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_temperature_adjustment_goes_to_climate(self):
        for text in ("tăng nhiệt độ máy lạnh lên hai mươi tám độ",
                     "chỉnh nhiệt độ máy lạnh lên hai mươi tám độ",
                     "giảm nhiệt độ điều hòa xuống 26 độ"):
            with self.subTest(text=text), patch("server.core.text_command.act_climate", new_callable=AsyncMock, return_value={"reply": "climate"}) as climate, \
                    patch("server.core.text_command.act_sensor", new_callable=AsyncMock, return_value={"reply": "sensor"}) as sensor:
                result = await fast_path(self.db, {}, None, text, 1)
                self.assertEqual(result["reply"], "climate")
                self.assertEqual(climate.await_args.kwargs["temperature"], 26 if "26" in text else 28)
                sensor.assert_not_awaited()

    async def test_temperature_question_still_reads_sensor(self):
        with patch("server.core.text_command.act_sensor", new_callable=AsyncMock, return_value={"reply": "sensor"}) as sensor, \
                patch("server.core.text_command.act_climate", new_callable=AsyncMock) as climate:
            result = await fast_path(self.db, {}, None, "nhiệt độ phòng bao nhiêu", 1)
            self.assertEqual(result["reply"], "sensor")
            sensor.assert_awaited_once()
            climate.assert_not_awaited()

    async def test_climate_temperature_question_does_not_send_command(self):
        with patch("server.core.text_command.act_sensor", new_callable=AsyncMock, return_value={"reply": "sensor"}), \
                patch("server.core.text_command.act_climate", new_callable=AsyncMock) as climate:
            await fast_path(self.db, {}, None, "nhiệt độ máy lạnh tăng lên 28 độ chưa", 1)
            climate.assert_not_awaited()

    async def test_climate_question_without_sensor_words_does_not_send_command(self):
        with patch("server.core.text_command.act_climate", new_callable=AsyncMock) as climate:
            await fast_path(self.db, {}, None, "máy lạnh đang 28 độ không", 1)
            climate.assert_not_awaited()

    async def test_multiple_requests_are_not_partially_executed(self):
        self.db.execute("INSERT INTO entities(id,device_id,name,entity_type,aliases) VALUES (4,1,'Quạt','fan','quạt'),(5,1,'TV','media','tv,tivi'),(6,1,'Loa','media','loa')")
        for text in ("chỉnh máy lạnh 28 độ và bật đèn", "tăng âm lượng và bật đèn",
                     "cho quạt xoay và bật đèn", "mở youtube và bật đèn",
                     "tăng âm lượng loa và cho biết thời tiết", "cho quạt xoay và cho biết thời tiết",
                     "mở youtube trên tv và cho biết thời tiết"):
            with self.subTest(text=text), patch("server.core.text_command.act_climate", new_callable=AsyncMock) as climate, \
                    patch("server.core.text_command.act_button", new_callable=AsyncMock) as button, \
                    patch("server.core.text_command.act_tv_app", new_callable=AsyncMock) as tv:
                self.assertIsNone(await fast_path(self.db, {}, None, text, 1))
                climate.assert_not_awaited()
                button.assert_not_awaited()
                tv.assert_not_awaited()

    async def test_device_status_question_does_not_switch_it(self):
        self.db.execute("INSERT INTO entities(id,device_id,name,entity_type,aliases) VALUES (4,1,'Đèn','light','đèn')")
        with patch("server.core.text_command.act_switch", new_callable=AsyncMock) as switch:
            await fast_path(self.db, {}, None, "đèn đang bật không", 1)
            switch.assert_not_awaited()

    async def test_adjustment_sends_requested_temperature_to_driver(self):
        with patch("server.core.text_command._set_state", new_callable=AsyncMock,
                   return_value={"state": "on", "temperature": 28, "mode": "cool"}) as send:
            result = await fast_path(self.db, {}, None, "chỉnh nhiệt độ máy lạnh lên hai mươi tám độ", 1)
            self.assertTrue(result["action"]["ok"])
            self.assertEqual(send.await_args.args[3:], ("set", {"mode": "cool", "temperature": 28}))

    async def test_missing_climate_in_microphone_room_does_not_use_other_room(self):
        with patch("server.core.text_command.act_climate", new_callable=AsyncMock) as climate, \
                patch("server.core.text_command.act_sensor", new_callable=AsyncMock) as sensor:
            result = await fast_path(self.db, {}, None, "chỉnh nhiệt độ máy lạnh lên 28 độ", 2)
            self.assertIn("không có máy lạnh", result["reply"])
            climate.assert_not_awaited()
            sensor.assert_not_awaited()
