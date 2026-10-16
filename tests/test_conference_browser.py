"""Offline checks for the compressed, self-contained conference browser."""
import base64
import gzip
import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from export_conference import compact_browser_page


class BrowserPageTests(unittest.TestCase):
    def document(self, records):
        encoded = json.dumps(records).replace('<', '\\u003c')
        return '<html><a href="file.html">File</a><script id="data" type="application/json">' + encoded + '</script><script>\nconst data=JSON.parse(document.getElementById(\'data\').textContent);\ndocument.title=data.run;</script></html>'

    def test_large_inline_records_roundtrip_without_changing_content_or_links(self):
        data = {'run': 'example', 'records': [{'text': '</script><script>bad()</script> 😀' * 1000}] * 400}
        original = self.document(data)
        self.assertGreater(len(original.encode()), 10_000_000)
        exported = compact_browser_page(original)
        self.assertLess(len(exported.encode()), 8_000_000)
        packed = re.search(r'type="application/gzip\+base64">([^<]+)</script>', exported)[1]
        self.assertEqual(json.loads(gzip.decompress(base64.b64decode(packed))), data)
        self.assertIn('href="file.html"', exported)
        self.assertNotIn('<script>bad()', exported)
        self.assertIn('DecompressionStream', exported)
        self.assertIn('Could not load conference:', exported)

    def test_unknown_renderer_refused(self):
        with self.assertRaisesRegex(ValueError, 'bootstrap'):
            compact_browser_page(self.document({}).replace('const data=', 'let data='))


if __name__ == '__main__':
    unittest.main()
