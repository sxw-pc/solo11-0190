import unittest
from pathlib import Path
from termsettle.portal_capture import read_page
from termsettle.glossary_snapshot import build

ROOT = Path(__file__).resolve().parents[1] / 'fixtures'


class PortalSeedTest(unittest.TestCase):
    def test_first_page_is_falsely_complete(self):
        self.assertEqual(read_page(ROOT / 'portal-page-1.html'), ['A'])
        self.assertEqual(build(ROOT / 'portal-page-1.html'), {'complete': True, 'terms': ['A']})
        self.assertEqual(read_page(ROOT / 'portal-page-2.html'), ['B'])


if __name__ == '__main__':
    unittest.main()
