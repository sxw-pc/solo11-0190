import json
import unittest
from pathlib import Path
from termsettle.remittance_capture import received_for
from termsettle.receivables import balances

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'shared-remittance.json'

class ReceivablesSeedTest(unittest.TestCase):
    def test_shared_transfer_double_applied(self):
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(len(received_for(data['remittances'], 'INV-A')), 1)
        self.assertEqual(balances(data['invoices'], data['remittances']),
                         {'INV-A': -400, 'INV-B': -600})

if __name__ == '__main__':
    unittest.main()
