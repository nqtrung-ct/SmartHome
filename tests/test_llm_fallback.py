import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from server.core.text_command import _call_llm


class LlmFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_ollama_failure_uses_configured_cloud(self):
        values = {"llm.engine": "ollama", "llm.base_url": "https://example.com/v1",
                  "llm.api_key": "test", "llm.cloud_model": "cloud"}
        cfg = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
        with patch("server.core.text_command._ollama_chat", new_callable=AsyncMock, side_effect=OSError), \
             patch("server.core.text_command._openai_chat", new_callable=AsyncMock, return_value={"content": "ok"}):
            result, engine = await _call_llm(cfg, [])
        self.assertEqual((result["content"], engine), ("ok", "openai-fallback"))

    async def test_no_cloud_does_not_call_it(self):
        cfg = SimpleNamespace(get=lambda key, default=None: {"llm.engine": "ollama"}.get(key, default))
        with patch("server.core.text_command._ollama_chat", new_callable=AsyncMock, side_effect=OSError), \
             patch("server.core.text_command._openai_chat", new_callable=AsyncMock) as cloud:
            with self.assertRaises(OSError):
                await _call_llm(cfg, [])
            cloud.assert_not_awaited()
