import json
import unittest
from pathlib import Path
from termsettle.access_capture import unknown_accesses
from termsettle.incident_decision import decide

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'source-access.json'

class IncidentSeedTest(unittest.TestCase):
    def test_unknown_proxy_triggers_notification(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual([e['id'] for e in unknown_accesses(data['events'], data['known_accounts'])], ['e2'])
        self.assertEqual(decide(data['events'], data['known_accounts']),
                         {'affected': 1, 'notify': True})

if __name__ == '__main__':
    unittest.main()
