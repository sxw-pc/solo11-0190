import json
import unittest
from pathlib import Path
from termsettle.ticket_capture import first_response
from termsettle.sla_decision import decide

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'service-thread.json'

class SLASeedTest(unittest.TestCase):
    def test_bot_receipt_looks_like_response(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(first_response(data['events'], 'T-47')['id'], 'auto')
        self.assertEqual(decide(data['events'], 'T-47', 0, 30),
                         {'elapsed': 2, 'met': True})

if __name__ == '__main__':
    unittest.main()
