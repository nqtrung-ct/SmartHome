import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.voice.stt import SttQwen


class QwenSttTests(unittest.TestCase):
    def test_passes_pcm_and_reloads_markdown_context_each_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "python").touch()
            (root / "config.json").touch()
            (root / "model.safetensors").touch()
            context = root / "context.md"
            context.write_text("Phòng khách", encoding="utf-8")
            values = {"stt.qwen_python": str(root / "python"),
                      "stt.qwen_model_dir": str(root), "stt.qwen_context_file": str(context)}
            engine = SttQwen(SimpleNamespace(get=lambda key, default=None: values.get(key, default)))
            sink = io.StringIO()
            engine._proc = SimpleNamespace(stdin=sink)
            with patch.object(engine, "_start"), patch.object(engine, "_read", return_value={"text": "bật đèn"}):
                self.assertEqual(engine.transcribe(b"\x01\x00"), "bật đèn")
                context.write_text("Nhà bếp", encoding="utf-8")
                self.assertEqual(engine.transcribe(b"\x02\x00"), "bật đèn")
            first, second = [json.loads(line) for line in sink.getvalue().splitlines()]
            self.assertEqual(base64.b64decode(first["audio"]), b"\x01\x00")
            self.assertEqual((first["context"], second["context"]), ("Phòng khách", "Nhà bếp"))

    def test_missing_model_is_rejected_before_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(get=lambda key, default=None: tmp)
            with self.assertRaisesRegex(RuntimeError, "Chưa cài"):
                SttQwen(cfg)


if __name__ == "__main__":
    unittest.main()
