import base64
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from server.api.assistant import AssistantConfigIn, update_config
from server.voice.stt import SttQwen, make_stt


class QwenSttTests(unittest.TestCase):
    def test_ollama_failure_uses_sherpa_and_retries_qwen(self):
        values = {"stt.engine": "qwen3_ollama", "stt.qwen_ollama_url": "http://192.0.2.1:11434", "stt.qwen_ollama_model": "qwen-asr"}
        cfg = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
        sherpa = Mock()
        sherpa.transcribe.return_value = "dự phòng"
        response = Mock()
        response.json.return_value = {"message": {"content": "language Vietnamese<asr_text>qwen"}}
        with patch("server.voice.stt.httpx.post", side_effect=[OSError("offline"), response]) as post, \
             patch("server.voice.stt.SttSherpa", return_value=sherpa):
            engine = make_stt(cfg)
            self.assertEqual(engine.transcribe(b"12"), "dự phòng")
            self.assertEqual(engine.transcribe(b"12"), "qwen")
            self.assertEqual(post.call_args.args[0], "http://192.0.2.1:11434/api/chat")
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["model"], "qwen-asr")
            wav = base64.b64decode(payload["messages"][0]["images"][0])
            with wave.open(io.BytesIO(wav), "rb") as audio:
                self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()), (16000, 1, 2))
                self.assertEqual(audio.readframes(1), b"12")

    def test_ollama_non_asr_response_falls_back(self):
        cfg = SimpleNamespace(get=lambda key, default=None: {"stt.engine": "qwen3_ollama"}.get(key, default))
        response = Mock()
        response.json.return_value = {"message": {"content": "I cannot listen"}}
        sherpa = Mock()
        sherpa.transcribe.return_value = "dự phòng"
        with patch("server.voice.stt.httpx.post", return_value=response), patch("server.voice.stt.SttSherpa", return_value=sherpa):
            self.assertEqual(make_stt(cfg).transcribe(b"12"), "dự phòng")

    def test_passes_pcm_and_reloads_markdown_context_each_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "python").touch()
            (root / "config.json").touch()
            (root / "model.safetensors").touch()
            context = root / "context.md"
            context.write_text("Phòng khách", encoding="utf-8")
            values = {"stt.qwen_torch_python": str(root / "python"),
                      "stt.qwen_torch_model_dir": str(root), "stt.qwen_context_file": str(context)}
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

    def test_windows_qwen_uses_its_own_model_and_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "python.exe").touch()
            (root / "config.json").touch()
            (root / "model.safetensors").touch()
            values = {"stt.engine": "qwen3_torch", "stt.qwen_torch_python": str(root / "python.exe"),
                      "stt.qwen_torch_model_dir": str(root)}
            engine = make_stt(SimpleNamespace(get=lambda key, default=None: values.get(key, default)))
            self.assertEqual(engine.qwen.model, root)
            self.assertEqual(engine.qwen.python, root / "python.exe")
            engine.close()

    def test_shared_keep_alive_warms_qwen_and_idle_timer_stops_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "python").touch()
            (root / "config.json").touch()
            (root / "model.safetensors").touch()
            values = {"stt.engine": "qwen3_torch", "stt.qwen_torch_python": str(root / "python"),
                      "stt.qwen_torch_model_dir": str(root), "llm.keep_alive": "-1"}
            cfg = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
            with patch("server.voice.stt.threading.Thread") as thread:
                engine = make_stt(cfg)
                thread.assert_called_once()
                thread.return_value.start.assert_called_once()
            engine = engine.qwen
            with patch.object(engine, "_start") as start:
                engine._warmup()
                start.assert_called_once()
            engine._proc = object()
            with patch("server.voice.stt.threading.Timer") as timer, patch.object(engine, "_stop_worker") as stop:
                engine.set_keep_alive(False)
                self.assertEqual(timer.call_args.args[0], 300)
                timer.return_value.start.assert_called_once()
                callback = timer.call_args.args[1]
                callback()
                stop.assert_called_once()

    def test_cancelled_idle_callback_does_not_stop_kept_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "python").touch()
            (root / "config.json").touch()
            (root / "model.safetensors").touch()
            values = {"stt.qwen_torch_python": str(root / "python"), "stt.qwen_torch_model_dir": str(root)}
            engine = SttQwen(SimpleNamespace(get=lambda key, default=None: values.get(key, default)))
            engine._proc = object()
            with patch("server.voice.stt.threading.Timer") as timer, \
                 patch("server.voice.stt.threading.Thread"), patch.object(engine, "_stop_worker") as stop:
                engine.set_keep_alive(False)
                callback = timer.call_args.args[1]
                engine.set_keep_alive(True)
                callback()
                timer.return_value.cancel.assert_called_once()
                stop.assert_not_called()

    def test_shared_checkbox_updates_running_stt(self):
        values = {"stt.engine": "qwen3_torch"}
        cfg = SimpleNamespace(get=lambda key, default=None: values.get(key, default),
                              _data={}, path=Path("unused.yaml"))
        stt = Mock()
        pipeline = SimpleNamespace(stt=stt, tts=None, _ack_audio=None)
        manager = Mock()
        app = SimpleNamespace(cfg=cfg, pipeline=pipeline, db=object(), ollama_keepalive=manager)
        request = SimpleNamespace(app=SimpleNamespace(state=app))
        with patch("server.api.assistant.set_yaml_value"), patch("server.api.assistant.make_tts"), \
             patch("server.api.assistant.log_event"):
            update_config(request, AssistantConfigIn(llm_keep_alive=True))
            update_config(request, AssistantConfigIn(llm_keep_alive=False))
        self.assertEqual([call.args[0] for call in stt.set_keep_alive.call_args_list], [True, False])
        manager.request_load.assert_called_once()


if __name__ == "__main__":
    unittest.main()
