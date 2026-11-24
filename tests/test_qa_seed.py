import json
import unittest
from pathlib import Path
from termsettle.qa_capture import flagged
from termsettle.quality_report import defect_rate

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'qa-sample.json'

class QASeedTest(unittest.TestCase):
    def test_flagged_sample_divided_by_entire_delivery(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(flagged(data['inspections']), 1)
        self.assertEqual(defect_rate(data['inspections'], data['delivered_count']), 0.1)

if __name__ == '__main__':
    unittest.main()
