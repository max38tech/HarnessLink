"""Local-only speech models. Imported only when real speech is requested."""
import asyncio
import io
from pathlib import Path
import wave

from .audio import resample_pcm


class LocalSpeech:
    def __init__(self, models: Path, voice='en_US-lessac-medium', whisper='base.en'):
        self.models, self.voice_name, self.whisper_name = models, voice, whisper
        self.lock = asyncio.Lock()

    def load(self):
        from faster_whisper import WhisperModel
        from piper import PiperVoice
        self.stt = WhisperModel(str(self.models / self.whisper_name), device='cpu', compute_type='int8', cpu_threads=6)
        self.tts = PiperVoice.load(str(self.models / (self.voice_name + '.onnx')))

    async def _worker(self, fn, *args):
        # Cancellation must not leave model threads racing the next utterance.
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(fn, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.gather(task, return_exceptions=True)
                raise

    def _transcribe(self, pcm):
        import numpy as np
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        segments, _ = self.stt.transcribe(samples, language='en', beam_size=1,
                                          vad_filter=True, condition_on_previous_text=False)
        return ' '.join(s.text.strip() for s in segments).strip()

    async def transcribe(self, pcm):
        return await self._worker(self._transcribe, pcm)

    def _synthesize(self, text):
        stream = io.BytesIO()
        with wave.open(stream, 'wb') as wav:
            self.tts.synthesize_wav(text, wav)
        with wave.open(io.BytesIO(stream.getvalue()), 'rb') as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise RuntimeError('Piper returned unsupported audio format')
            if wav.getnframes() > wav.getframerate() * 180:
                raise RuntimeError('Spoken response exceeded three minutes')
            return resample_pcm(wav.readframes(wav.getnframes()), wav.getframerate(), gain=0.25)

    async def synthesize(self, text):
        return await self._worker(self._synthesize, text)
