import asyncio
import unittest
from unittest.mock import AsyncMock

from server.api.xiaozhi import _command_with_wait


class WaitingMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_command_does_not_announce(self):
        task = asyncio.create_task(asyncio.sleep(0, result="answer"))
        announce = AsyncMock()
        self.assertEqual(await _command_with_wait(task, announce, 1), "answer")
        announce.assert_not_awaited()

    async def test_slow_command_runs_while_waiting_message_plays(self):
        release = asyncio.Event()
        async def command():
            await release.wait()
            return "answer"
        async def announce():
            release.set()
            await asyncio.sleep(0)
        callback = AsyncMock(side_effect=announce)
        task = asyncio.create_task(command())
        self.assertEqual(await _command_with_wait(task, callback, 0), "answer")
        callback.assert_awaited_once()

    async def test_cancel_also_cancels_command(self):
        task = asyncio.create_task(asyncio.Event().wait())
        waiting = asyncio.create_task(_command_with_wait(task, AsyncMock(), 60))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertTrue(task.cancelled())
