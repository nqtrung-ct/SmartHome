import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api.assistant import router
from server.api.deps import require_admin


class MicrophoneSttTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[require_admin] = lambda: None
        self.stt = Mock()
        self.stt.transcribe.return_value = "bật đèn"
        self.app.state.pipeline = SimpleNamespace(stt=self.stt)
        self.client = TestClient(self.app)

    def test_transcribes_audio_without_running_commands(self):
        pcm = b"\xff\x7f" * 800
        response = self.client.post("/assistant/test-stt-mic", content=pcm)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["transcript"], "bật đèn")
        self.stt.transcribe.assert_called_once_with(pcm)

    def test_rejects_invalid_or_oversized_audio_and_silence(self):
        self.assertEqual(self.client.post("/assistant/test-stt-mic", content=b"x").status_code, 400)
        self.assertEqual(self.client.post("/assistant/test-stt-mic", content=b"x" * 640002).status_code, 413)
        result = self.client.post("/assistant/test-stt-mic", content=b"\0" * 320).json()
        self.assertEqual(result["transcript"], "")
        self.stt.transcribe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
