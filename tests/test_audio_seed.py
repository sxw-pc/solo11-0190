import json
import unittest
from pathlib import Path
from termsettle.audio_capture import received
from termsettle.meeting_archive import assess

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'review-audio.json'


class AudioSeedTest(unittest.TestCase):
    def test_transcoded_repeat_masquerades_as_final_chunk(self):
        rows = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(received(rows), 3)
        self.assertEqual(assess(rows), {'uploads': 3, 'status': '完整'})
        self.assertEqual(len({row['samples'] for row in rows}), 2)


if __name__ == '__main__':
    unittest.main()
