"""One authenticated device, one recording and one reply at a time."""
import asyncio
import contextlib
import hmac
import json
import time
import wave

from .audio import Recording, RATE, speech_text

PLAY_CHUNK_BYTES = 3200  # 100 ms; one unacknowledged chunk maximum.


def parse_control(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get('type'), str):
        raise ValueError('Expected a control object with a type')
    return value


class DeviceSession:
    def __init__(self, socket, token, codex, speech, capture=None):
        self.socket, self.token, self.codex, self.speech = socket, token, codex, speech
        self.capture = capture
        self.recording = None
        self.record_id = 0
        self.record_started = 0
        self.work = None
        self.play_id = 0
        self.ack = None
        self.expected_ack = None
        self.play_done = None

    async def send(self, kind, **params):
        await self.socket.send(json.dumps({'type': kind, **params}))

    async def status(self, value):
        await self.send('status', state=value)

    async def run(self):
        try:
            raw = await asyncio.wait_for(self.socket.recv(), 10)
            hello = parse_control(raw) if isinstance(raw, str) else {}
            token = hello.get('token', '')
            if (hello.get('type') != 'hello' or hello.get('version') != 1
                    or not isinstance(token, str)
                    or not hmac.compare_digest(token.encode(), self.token.encode())):
                await self.socket.close(code=1008, reason='Authentication failed')
                return
            await self.send('ready', version=1, sample_rate=RATE)
            while True:
                timeout = max(0.01, 65 - (time.monotonic() - self.record_started)) if self.recording else None
                try:
                    raw = await asyncio.wait_for(self.socket.recv(), timeout)
                    if isinstance(raw, bytes):
                        if self.recording is None:
                            raise ValueError('Audio received outside a recording')
                        self.recording.append(raw)
                    else:
                        await self.control(parse_control(raw))
                except (ValueError, TimeoutError) as exc:
                    self.recording = None
                    await self.stop_work()
                    await self.send('error', message=str(exc) or 'Recording timed out')
                    await self.send('ready')
        finally:
            self.recording = None
            await self.stop_work(notify=False)

    async def control(self, msg):
        kind = msg['type']
        if kind == 'record_start':
            ident = msg.get('id')
            if type(ident) is not int or ident <= self.record_id or ident > 0xFFFFFFFF:
                raise ValueError('Recording id must increase within a connection')
            self.recording = None
            await self.stop_work()
            self.record_id = ident
            self.recording = Recording()
            self.record_started = time.monotonic()
            await self.send('recording', id=ident)
        elif kind == 'record_end':
            if self.recording is None or msg.get('id') != self.record_id:
                raise ValueError('No matching recording to finish')
            pcm = bytes(self.recording.data) if self.capture else self.recording.finish()
            self.recording = None
            if pcm is None:
                await self.send('ready')
            else:
                self.work = asyncio.create_task(self.respond(pcm))
        elif kind == 'record_cancel':
            self.recording = None
            await self.stop_work()
            await self.send('ready')
        elif kind == 'play_ack':
            if self.expected_ack == (msg.get('id'), msg.get('seq')) and self.ack and not self.ack.done():
                self.ack.set_result(None)
        elif kind == 'play_done':
            if msg.get('id') == self.play_id and self.play_done and not self.play_done.done():
                self.play_done.set_result(None)
        else:
            raise ValueError('Unknown control type')

    async def stop_work(self, notify=True):
        if self.work and not self.work.done():
            self.work.cancel()
            await asyncio.gather(self.work, return_exceptions=True)
            # Codex.ask cancellation waits for the interrupted turn to finish.
            if notify:
                await self.send('play_cancel', id=self.play_id)
        self.work = None
        self.expected_ack = None
        self.ack = None
        self.play_done = None
        if notify and not self.codex.healthy:
            raise RuntimeError('Codex state is uncertain; inspect PC thread before reconnecting')

    async def respond(self, pcm):
        try:
            started = time.monotonic()
            await self.status('transcribing')
            if self.capture:
                self.capture.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Explicit diagnostic mode only; normal operation retains no microphone recordings.
                with wave.open(str(self.capture), 'wb') as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(RATE)
                    wav.writeframes(pcm)
                await self.send('ready')
                print(f'Captured microphone WAV: {self.capture}', flush=True)
                return
            text = await self.speech.transcribe(pcm)
            if not text.strip():
                await self.send('ready')
                return
            print('You:', text, flush=True)
            self.codex.log({'latency': 'transcription', 'seconds': time.monotonic() - started})
            await self.status('processing')
            reply = await self.codex.ask(text)
            print('Codex:', reply, flush=True)
            spoken = speech_text(reply)
            if len(spoken) > 2000:
                spoken = spoken[:2000].rsplit(' ', 1)[0] + '. The full response is on your PC.'
            if not spoken:
                spoken = 'The response is available in the PC transcript.'
            await self.status('synthesizing')
            audio = await self.speech.synthesize(spoken)
            self.codex.log({'latency': 'release_to_audio', 'seconds': time.monotonic() - started})
            await self.play(audio)
            await self.send('ready')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Local details stay on the PC; do not forward provider payloads over Wi-Fi.
            print(f'HarnessLink response failed: {exc}', flush=True)
            self.codex.log({'bridgeError': str(exc)})
            with contextlib.suppress(Exception):
                await self.send('error', message='Response failed; inspect the PC terminal. Nothing was retried.')
                await self.send('ready')

    async def play(self, audio):
        self.play_id += 1
        ident = self.play_id
        self.play_done = asyncio.get_running_loop().create_future()
        await self.send('play_start', id=ident, sample_rate=RATE, bytes=len(audio))
        for seq, offset in enumerate(range(0, len(audio), PLAY_CHUNK_BYTES)):
            self.ack = asyncio.get_running_loop().create_future()
            self.expected_ack = (ident, seq)
            await self.socket.send(audio[offset:offset + PLAY_CHUNK_BYTES])
            await asyncio.wait_for(self.ack, 5)
        self.expected_ack = None
        await self.send('play_end', id=ident)
        await asyncio.wait_for(self.play_done, 5)
        self.play_done = None
