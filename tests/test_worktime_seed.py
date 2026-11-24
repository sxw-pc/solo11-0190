import json
import unittest
from pathlib import Path
from termsettle.time_capture import booked_hours
from termsettle.availability import remaining

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'worktime-overlap.json'


class WorktimeSeedTest(unittest.TestCase):
    def test_overlapping_timers_are_summed(self):
        rows = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(booked_hours(rows), 8)
        self.assertEqual(remaining(rows), {'booked': 8, 'remaining': 0})


if __name__ == '__main__':
    unittest.main()
