import asyncio
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

from harnesslink.codex import CodexClient
from harnesslink.session import DeviceSession


class MemorySocket:
    def __init__(self):
        self.inbox = asyncio.Queue()
        self.outbox = asyncio.Queue()
        self.closed = None

    async def recv(self):
        return await self.inbox.get()

    async def send(self, data):
        await self.outbox.put(data)

    async def close(self, code=1000, reason=''):
        self.closed = code

    async def put(self, msg):
        await self.inbox.put(msg if isinstance(msg, bytes) else json.dumps(msg))

    async def until(self, kind):
        async with asyncio.timeout(3):
            while True:
                value = await self.outbox.get()
                if isinstance(value, str):
                    message = json.loads(value)
                    if message.get('type') == kind:
                        return message


class SpeechFixture:
    def __init__(self):
        self.prompts = []

    async def transcribe(self, pcm):
        self.prompts.append(pcm)
        return 'hello'

    async def synthesize(self, text):
        return b'\x10\0' * 2000


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.codex = CodexClient([sys.executable, str(Path(__file__).with_name('fake_codex.py'))], Path(self.tmp.name), Path.cwd())
        await self.codex.start()
        self.socket = MemorySocket()
        self.speech = SpeechFixture()
        self.session = DeviceSession(self.socket, 'a' * 32, self.codex, self.speech)
        self.task = asyncio.create_task(self.session.run())

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.codex.close()
        self.tmp.cleanup()

    async def hello(self):
        await self.socket.put({'type': 'hello', 'version': 1, 'token': 'a' * 32})
        await self.socket.until('ready')

    async def record(self, pcm, ident=1):
        await self.socket.put({'type': 'record_start', 'id': ident})
        await self.socket.until('recording')
        await self.socket.put(pcm)
        await self.socket.put({'type': 'record_end', 'id': ident})

    async def test_wrong_token_never_starts_recording(self):
        await self.socket.put({'type': 'hello', 'version': 1, 'token': 'wrong'})
        await asyncio.wait_for(self.task, 2)
        self.assertEqual(self.socket.closed, 1008)
        self.assertEqual(self.speech.prompts, [])

    async def test_full_exchange_requires_ack_before_second_audio_chunk(self):
        await self.hello()
        await self.record(struct.pack('<hh', 2000, -2000) * 4000)
        start = await self.socket.until('play_start')
        first = await self.socket.outbox.get()
        self.assertIsInstance(first, bytes)
        self.assertEqual(len(first), 3200)
        await asyncio.sleep(0.01)
        self.assertTrue(self.socket.outbox.empty())
        await self.socket.put({'type': 'play_ack', 'id': start['id'], 'seq': 0})
        self.assertEqual(len(await asyncio.wait_for(self.socket.outbox.get(), 2)), 800)
        await self.socket.put({'type': 'play_ack', 'id': start['id'], 'seq': 1})
        await self.socket.until('play_end')
        await self.socket.put({'type': 'play_done', 'id': start['id']})
        await self.socket.until('ready')
        self.assertEqual(len(self.speech.prompts), 1)

    async def test_silence_does_not_invoke_transcription(self):
        await self.hello()
        await self.record(b'\0' * 16000)
        await self.socket.until('ready')
        self.assertEqual(self.speech.prompts, [])

    async def test_diagnostic_capture_saves_silent_microphone_for_inspection(self):
        import wave
        path = Path(self.tmp.name) / 'mic.wav'
        self.session.capture = path
        await self.hello()
        await self.record(b'\0' * 16000)
        await self.socket.until('ready')
        with wave.open(str(path), 'rb') as wav:
            self.assertEqual(wav.getnframes(), 8000)
            self.assertEqual(wav.getframerate(), 16000)
        self.assertEqual(self.speech.prompts, [])

    async def test_tone_finishes_after_final_ack_without_extra_message(self):
        from harnesslink.cli import play_test_tone
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        playback = asyncio.create_task(play_test_tone(self.session))
        start = await self.socket.until('play_start')
        for seq in range(10):
            self.assertIsInstance(await asyncio.wait_for(self.socket.outbox.get(), 2), bytes)
            await self.socket.put({'type': 'play_ack', 'id': start['id'], 'seq': seq})
        await self.socket.until('play_end')
        await self.socket.put({'type': 'play_done', 'id': start['id']})
        await asyncio.wait_for(playback, 2)

    async def test_record_start_interrupts_playback_without_replaying_old_reply(self):
        await self.hello()
        await self.record(struct.pack('<hh', 2000, -2000) * 4000)
        await self.socket.until('play_start')
        self.assertIsInstance(await self.socket.outbox.get(), bytes)
        await self.socket.put({'type': 'record_start', 'id': 2})
        await self.socket.until('play_cancel')
        self.assertEqual((await self.socket.until('recording'))['id'], 2)
        await self.socket.put(b'\0' * 16000)
        await self.socket.put({'type': 'record_end', 'id': 2})
        await self.socket.until('ready')
        self.assertEqual(len(self.speech.prompts), 1)

    async def test_audio_without_record_start_is_rejected(self):
        await self.hello()
        await self.socket.put(b'\0\0')
        error = await self.socket.until('error')
        self.assertIn('recording', error['message'])

    async def test_stale_playback_ack_cannot_advance_audio(self):
        await self.hello()
        await self.record(struct.pack('<hh', 2000, -2000) * 4000)
        start = await self.socket.until('play_start')
        await self.socket.outbox.get()
        await self.socket.put({'type': 'play_ack', 'id': start['id'] - 1, 'seq': 0})
        await asyncio.sleep(0.01)
        self.assertTrue(self.socket.outbox.empty())
