import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from server.core.ollama_keepalive import OllamaKeepAlive, _restart_local_ollama


class KeepAliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_force_restart_before_loading(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"done": True})
        values = {"llm.engine": "ollama", "llm.model": "local:4b", "llm.keep_alive": "-1"}
        manager = OllamaKeepAlive.__new__(OllamaKeepAlive)
        manager.cfg = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
        manager.db = object()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("server.core.ollama_keepalive.httpx.AsyncClient", return_value=client), \
                patch("server.core.ollama_keepalive._restart_local_ollama") as restart, \
                patch("server.core.ollama_keepalive.log_event"):
            await manager._load(restart=True)
        restart.assert_called_once()
        self.assertEqual(calls, ["/api/tags", "/api/generate"])

    async def test_remote_restart_is_refused(self):
        values = {"llm.engine": "ollama", "llm.model": "local:4b", "llm.ollama_url": "http://192.0.2.1:11434"}
        manager = OllamaKeepAlive.__new__(OllamaKeepAlive)
        manager.cfg = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
        with self.assertRaisesRegex(RuntimeError, "máy khác"):
            await manager._load(restart=True)

    async def test_start_local_ollama_when_connection_is_refused(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if len(calls) == 1:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"done": True})

        values = {"llm.engine": "ollama", "llm.model": "local:4b", "llm.keep_alive": "-1"}
        manager = OllamaKeepAlive.__new__(OllamaKeepAlive)
        manager.cfg = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
        manager.db = object()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("server.core.ollama_keepalive.httpx.AsyncClient", return_value=client), \
                patch("server.core.ollama_keepalive.shutil.which", return_value="ollama"), \
                patch("server.core.ollama_keepalive.subprocess.Popen") as spawn, \
                patch("server.core.ollama_keepalive.asyncio.sleep", new_callable=AsyncMock), \
                patch("server.core.ollama_keepalive.log_event"):
            await manager._load()
        spawn.assert_called_once()
        self.assertEqual(calls, ["/api/tags", "/api/tags", "/api/generate"])

    async def test_load_uses_fallback_model_and_keeps_it(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"done": True})

        values = {"llm.engine": "openai", "llm.local_model": "local:4b", "llm.keep_alive": "-1"}
        manager = OllamaKeepAlive.__new__(OllamaKeepAlive)
        manager.cfg = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
        manager.db = object()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("server.core.ollama_keepalive.httpx.AsyncClient", return_value=client), \
                patch("server.core.ollama_keepalive.log_event"):
            await manager._load()
        self.assertEqual([r.url.path for r in calls], ["/api/tags", "/api/generate"])
        self.assertEqual(json.loads(calls[-1].content), {"model": "local:4b", "prompt": "", "keep_alive": -1})

    async def test_startup_toggle_and_midnight_only_load_when_enabled(self):
        values = {"llm.keep_alive": "5m"}
        cfg = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
        with patch.object(OllamaKeepAlive, "_load", new_callable=AsyncMock) as load:
            manager = OllamaKeepAlive(cfg, object())
            await asyncio.sleep(0.02)
            load.assert_not_awaited()
            values["llm.keep_alive"] = "-1"
            manager.request_load()
            await asyncio.sleep(0.02)
            load.assert_awaited_once()
            # A timeout is the same path used at midnight.
            calls = 0
            async def timeout_then_cancel(awaitable, timeout):
                nonlocal calls
                awaitable.close()
                calls += 1
                if calls == 1:
                    raise asyncio.TimeoutError()
                raise asyncio.CancelledError()

            with patch("server.core.ollama_keepalive.asyncio.wait_for", side_effect=timeout_then_cancel):
                manager.task.cancel()
                await asyncio.gather(manager.task, return_exceptions=True)
                with self.assertRaises(asyncio.CancelledError):
                    await manager._run()
            self.assertEqual(load.await_count, 3)
            self.assertEqual(load.await_args_list[-1].kwargs, {"restart": True})


class RestartSafetyTests(unittest.TestCase):
    def test_refuses_an_unrelated_listener(self):
        output = [Mock(returncode=0, stdout="1234\n"),
                  Mock(returncode=0, stdout="1234 1 /usr/bin/other\n")]
        with patch("server.core.ollama_keepalive.sys.platform", "darwin"), \
                patch("server.core.ollama_keepalive.subprocess.run", side_effect=output), \
                patch("server.core.ollama_keepalive.os.kill") as kill:
            with self.assertRaisesRegex(RuntimeError, "tiến trình khác"):
                _restart_local_ollama()
        kill.assert_not_called()
