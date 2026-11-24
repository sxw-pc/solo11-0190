import json
import unittest
from pathlib import Path
from termsettle.document_capture import latest
from termsettle.document_custody import holders

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'source-packet-split.json'


class DocumentCustodySeedTest(unittest.TestCase):
    def test_whole_packet_return_masks_annex_handoff(self):
        record = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(latest(record['events'])['id'], 'packet-return')
        self.assertEqual(holders(record['events'], record['manifest']), {'source-main': 'client', 'annex-7': 'client'})
        self.assertEqual(record['events'][1]['location'], 'independent-reviewer')


if __name__ == '__main__':
    unittest.main()
