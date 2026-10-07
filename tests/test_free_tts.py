import unittest

from ccc_server import free_runtime


class FreeTtsTests(unittest.TestCase):
    def test_audio_type_comes_from_bytes_not_router_label(self):
        self.assertEqual(free_runtime._audio_type(b"RIFF\x00\x00\x00\x00WAVE"), "audio/wav")
        self.assertEqual(free_runtime._audio_type(b"ID3\x04rest"), "audio/mpeg")
        self.assertEqual(free_runtime._audio_type(b"\x00\x01"), "application/octet-stream")

    def test_empty_text_is_rejected_before_any_network(self):
        status, audio, _ctype, _label = free_runtime.tts("   ")
        self.assertEqual((status, audio), (400, b""))

    def test_voice_catalog_has_no_duplicates(self):
        self.assertEqual(len(free_runtime.TTS_VOICES), len(set(free_runtime.TTS_VOICES)))


if __name__ == "__main__":
    unittest.main()
