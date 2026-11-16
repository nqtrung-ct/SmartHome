import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.drivers import dahua_ptz as ptz


class Config:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, key, default=None):
        return self.mapping.get(key, default)


class PtzIPChangeTests(unittest.TestCase):
    def test_credentials_prefer_mac_over_reassigned_ip(self):
        secrets = {"AABBCCDDEEFF": "camera-secret", "192.0.2.2": "other-secret"}
        with patch.object(ptz, "load_secret", side_effect=lambda cfg, group, key: secrets.get(key)):
            self.assertEqual(ptz._creds(None, "192.0.2.2", "aa:bb:cc:dd:ee:ff"),
                             ("admin", "camera-secret"))

    def test_relay_target_follows_camera_mac(self):
        class SDK:
            def CLIENT_LoginEx2(self, host, port, *args):
                self.last = (host, port)
                return 123

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "camera"
            cfg = Config({"camera.ptz_devices": {"AABBCCDDEEFF": {
                "proxy": "127.0.0.1:13779", "target_file": str(target)}}})
            sdk = SDK()
            ptz._sessions.clear()
            try:
                with patch.object(ptz, "_load", return_value=sdk), patch.object(ptz, "_creds", return_value=("admin", "secret")):
                    ptz._login(cfg, "192.0.2.2", 37777, "aa:bb:cc:dd:ee:ff")
                    self.assertEqual(target.read_text().strip(), "192.0.2.2")
                    ptz._login(cfg, "192.0.2.3", 37777, "aa:bb:cc:dd:ee:ff")
                    self.assertEqual(target.read_text().strip(), "192.0.2.3")
                    self.assertEqual(sdk.last, (b"127.0.0.1", 13779))
            finally:
                ptz._sessions.clear()


if __name__ == "__main__":
    unittest.main()
