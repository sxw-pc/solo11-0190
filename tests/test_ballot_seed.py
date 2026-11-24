import json
import unittest
from pathlib import Path
from termsettle.reviewer_registry import positive_reviewers
from termsettle.term_ballot import decide

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'term-ballot.json'


class BallotSeedTest(unittest.TestCase):
    def test_two_accounts_approve_term(self):
        votes = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(positive_reviewers(votes), 2)
        self.assertEqual(decide(votes), {'positive_reviewers': 2, 'status': '定版'})


if __name__ == '__main__':
    unittest.main()
