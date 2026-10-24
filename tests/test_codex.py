import asyncio
import json
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

from harnesslink.codex import CodexClient


class CodexTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.client = CodexClient(
            [sys.executable, str(Path(__file__).with_name('fake_codex.py'))],
            self.state, Path.cwd(), approval=self.decline,
        )
        await self.client.start()

    async def decline(self, method, params):
        return {'decision': 'decline'}

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_persists_thread_and_only_returns_final_answer(self):
        self.assertEqual(await self.client.ask('hello'), 'Hello back.')
        self.assertEqual(json.loads((self.state / 'thread.json').read_text())['threadId'], 'thread-test')
        self.assertIn('Working...', (self.state / 'transcript.jsonl').read_text())
        await self.client.close()
        await self.client.start()
        self.assertEqual(await self.client.ask('again'), 'Hello back.')

    async def test_failed_turn_is_not_spoken_as_success(self):
        with self.assertRaisesRegex(RuntimeError, 'test failure'):
            await self.client.ask('fail')

    async def test_approval_response_does_not_block_rpc_reader(self):
        self.assertEqual(await asyncio.wait_for(self.client.ask('approval'), 2), 'Decision: decline')

    async def test_process_death_fails_pending_turn(self):
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            await asyncio.wait_for(self.client.ask('crash'), 2)

    async def test_interrupt_waits_for_turn_to_end(self):
        task = asyncio.create_task(self.client.ask('wait'))
        while self.client.turn_id is None:
            await asyncio.sleep(0)
        await self.client.interrupt()
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            await task
        self.assertEqual(await self.client.ask('next'), 'Hello back.')

    async def test_failed_interrupt_blocks_future_turns(self):
        task = asyncio.create_task(self.client.ask('wait-interrupt-error'))
        while self.client.turn_id is None:
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            await asyncio.wait_for(self.client.ask('next'), 2)

    async def test_shutdown_closes_descendants_holding_stdout_open(self):
        child_pid = int(await self.client.ask('spawn-child'))
        try:
            await asyncio.sleep(0.05)  # fixture parent exits, child still owns the stdout pipe
            await asyncio.wait_for(self.client.close(), 2)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, 9)
