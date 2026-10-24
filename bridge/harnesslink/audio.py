"""The shared audio contract: 16 kHz, mono, signed little-endian PCM16."""
import math
import re
import struct

RATE = 16000
FRAME_BYTES = 640  # 20 ms
MAX_SECONDS = 60
MAX_BYTES = RATE * 2 * MAX_SECONDS


class Recording:
    def __init__(self):
        self.data = bytearray()

    def append(self, pcm: bytes):
        if len(pcm) % 2:
            raise ValueError('PCM16 requires an even byte count')
        if len(self.data) + len(pcm) > MAX_BYTES:
            raise ValueError('Recording exceeded 60 seconds')
        self.data.extend(pcm)

    def finish(self) -> bytes | None:
        if len(self.data) < RATE // 2:  # at least 250 ms
            return None
        samples = [x[0] for x in struct.iter_unpack('<h', self.data)]
        mean = sum(samples) / len(samples)
        rms = math.sqrt(sum((x - mean) ** 2 for x in samples) / len(samples))
        # DC alone must not pass the silence gate.
        if rms < 100:
            return None
        return bytes(self.data)


def speech_text(text: str) -> str:
    text = re.sub(r'```.*?```', ' ', text, flags=re.S)
    text = re.sub(r'!\[([^]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'\[([^]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'(?m)^\s*(?:#+|>|[-*])\s+', '', text)
    text = re.sub(r'[`*_]', '', text)
    return ' '.join(text.split())


def resample_pcm(pcm: bytes, source_rate: int, gain: float = 0.25) -> bytes:
    if source_rate <= 0 or len(pcm) % 2 or not 0 <= gain <= 1:
        raise ValueError('Invalid PCM format or gain')
    samples = [x[0] for x in struct.iter_unpack('<h', pcm)]
    if not samples:
        return b''
    count = round(len(samples) * RATE / source_rate)
    out = bytearray()
    for i in range(count):
        pos = i * source_rate / RATE
        a = min(int(pos), len(samples) - 1)
        b = min(a + 1, len(samples) - 1)
        value = samples[a] + (samples[b] - samples[a]) * (pos - a)
        out.extend(struct.pack('<h', max(-32768, min(32767, round(value * gain)))))
    return bytes(out)
