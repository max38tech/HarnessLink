import struct
import unittest

from harnesslink.audio import Recording, speech_text, resample_pcm


class AudioTests(unittest.TestCase):
    def test_silence_never_becomes_a_prompt(self):
        rec = Recording()
        rec.append(b'\0\0' * 16000)
        self.assertIsNone(rec.finish())

    def test_recording_preserves_pcm(self):
        pcm = struct.pack('<hh', 2500, -2500) * 4000
        rec = Recording()
        rec.append(pcm)
        self.assertEqual(rec.finish(), pcm)

    def test_dc_offset_is_silence(self):
        rec = Recording()
        rec.append(struct.pack('<h', 2500) * 8000)
        self.assertIsNone(rec.finish())

    def test_unaligned_and_overlong_audio_rejected(self):
        rec = Recording()
        with self.assertRaises(ValueError):
            rec.append(b'\0')
        with self.assertRaises(ValueError):
            rec.append(b'\0' * (16000 * 2 * 60 + 2))

    def test_code_and_markdown_are_not_read_as_noise(self):
        text = 'Done.\n```python\nprint(1)\n```\nSee [the file](file:///tmp/a). **Ready**.'
        self.assertEqual(speech_text(text), 'Done. See the file. Ready.')

    def test_resample_keeps_little_endian_and_limits_volume(self):
        pcm = struct.pack('<hhhh', 0, 10000, -10000, 0)
        out = resample_pcm(pcm, 8000, gain=0.25)
        self.assertEqual(len(out), 16)
        self.assertLessEqual(max(abs(x) for x in struct.unpack('<8h', out)), 2500)


if __name__ == '__main__':
    unittest.main()
