import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.core.frigate_sync import sync_frigate, update_sources
from server.db import Database


CONFIG = '''# preserve this comment
cameras:
  kitchen:
    ffmpeg:
      inputs:
        - path: rtsp://user:secret@192.0.2.1:554/video
  living:
    ffmpeg:
      inputs:
        - path: rtsp://user:secret@192.0.2.2:554/video
go2rtc:
  streams:
    kitchen:
      - ffmpeg:rtsp://user:secret@192.0.2.1:554/video#video=h264
    living: rtsp://user:secret@192.0.2.2:554/video
  webrtc:
    candidates: [192.0.2.1:8555]
'''


class FrigateSyncTests(unittest.TestCase):
    def test_swapped_ips_are_scoped_to_each_camera_and_stream(self):
        result = update_sources(CONFIG, {"kitchen": "192.0.2.2", "living": "192.0.2.1"})
        self.assertEqual(result.count("rtsp://user:secret@192.0.2.2:554/video"), 2)
        self.assertEqual(result.count("rtsp://user:secret@192.0.2.1:554/video"), 2)
        self.assertIn("candidates: [192.0.2.1:8555]", result)
        self.assertTrue(result.startswith("# preserve this comment"))

    def test_failed_restart_is_retried_without_rewriting_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.yml"
            path.write_text(CONFIG, encoding="utf-8")
            db = Database(root / "test.db")
            db.execute("INSERT INTO devices(id,name,driver,host,created_at) VALUES (1,?,?,?,?)",
                       ("Camera", "onvif", "192.0.2.7", "2026-01-01"))
            db.execute("INSERT INTO entities(device_id,name,entity_type,driver_data) VALUES (?,?,?,?)",
                       (1, "Camera", "camera", '{"frigate":"kitchen"}'))
            cfg = SimpleNamespace(get=lambda key, default=None: {"frigate.config_path": str(path)}.get(key, default))
            with patch("server.core.frigate_sync.subprocess.run", side_effect=[
                SimpleNamespace(returncode=1), SimpleNamespace(returncode=0),
            ]) as restart:
                sync_frigate(db, cfg)
                pending = root / "config.yml.smarthome-restart-pending"
                self.assertTrue(pending.exists())
                updated = path.read_text(encoding="utf-8")
                self.assertEqual(updated.count("rtsp://user:secret@192.0.2.7:554/video"), 2)
                sync_frigate(db, cfg)
                self.assertFalse(pending.exists())
                self.assertEqual(path.read_text(encoding="utf-8"), updated)
                sync_frigate(db, cfg)
                self.assertEqual(restart.call_count, 2)
            db.close()


if __name__ == "__main__":
    unittest.main()
