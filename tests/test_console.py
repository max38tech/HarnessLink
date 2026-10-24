import asyncio
from contextlib import redirect_stdout
import io
import os
import unittest
from unittest.mock import patch

from harnesslink.console import Console


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def answer(self, response, method, params):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, response.encode())
            with os.fdopen(read_fd) as stream, patch('sys.stdin', stream), redirect_stdout(io.StringIO()):
                return await Console().approve(method, params)
        finally:
            os.close(write_fd)

    async def test_only_explicit_yes_accepts_once(self):
        for text, expected in [('y\n', 'accept'), ('yes\n', 'decline'), ('\n', 'decline')]:
            result = await self.answer(text, 'item/commandExecution/requestApproval', {'command': 'touch example'})
            self.assertEqual(result, {'decision': expected})

    async def test_numbered_answer_returns_option_label(self):
        result = await self.answer('2\n', 'item/tool/requestUserInput', {
            'questions': [{'id': 'mode', 'question': 'Choose mode', 'options': [
                {'label': 'First', 'description': 'One'}, {'label': 'Second', 'description': 'Two'},
            ]}],
        })
        self.assertEqual(result, {'answers': {'mode': {'answers': ['Second']}}})

    async def test_cancelled_prompt_does_not_consume_next_answer(self):
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(read_fd) as stream, patch('sys.stdin', stream), redirect_stdout(io.StringIO()):
                console = Console()
                first = asyncio.create_task(console.line('first'))
                await asyncio.sleep(0)
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
                os.write(write_fd, b'next\n')
                self.assertEqual(await asyncio.wait_for(console.line('second'), 1), 'next')
        finally:
            os.close(write_fd)
