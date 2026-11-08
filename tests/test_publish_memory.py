"""Lower peak memory of publish_cloudflare deploys (November 7, 2026), behaviour unchanged."""
import base64
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import publish_cloudflare as cf  # noqa: E402

KEY = b'sk-' + b'A' * 40


def pack(*entries):
    return json.dumps({'files': {f'instructions/a{i}.json': e for i, e in enumerate(entries)}}).encode()


class MemoryTests(unittest.TestCase):
    def test_pack_entries_screened_one_by_one_like_the_joined_text(self):
        name = 'RUN/packed/instructions-01.json'
        self.assertTrue(cf.is_pack_file(name))
        clean = pack({'encoding': 'utf-8', 'content': 'hello'},
                     {'encoding': 'base64', 'content': base64.b64encode(b'\xff\x00 world').decode()})
        self.assertEqual(list(cf.packed_run_entries(name, clean)), [b'hello', b'\xff\x00 world'])
        self.assertEqual(cf.packed_run_text(name, clean), b'hello\n\xff\x00 world')
        self.assertEqual(cf.site_gate(name, clean), clean)
        hidden = pack({'encoding': 'utf-8', 'content': 'ok'},
                      {'encoding': 'base64', 'content': base64.b64encode(b'x ' + KEY).decode()})
        self.assertIsNone(cf.SECRET.search(hidden))  # only visible once decoded
        with self.assertRaisesRegex(ValueError, 'Credential-like'):
            cf.site_gate(name, hidden)
        self.assertEqual(list(cf.packed_run_entries('RUN/results.html', b'x')), [])

    def test_summary_by_manifest_digests_equals_by_bytes(self):
        served = {'a.html': b'one', 'b.html': b'two', cf.SITE_MANIFEST: b'{}'}
        files = {'a.html': b'one', 'b.html': b'TWO', 'c.html': b'new', cf.SITE_MANIFEST: b'{"x":1}'}
        by_bytes = cf.summary(files, served, {}, None)
        by_digest = cf.summary(files, {n: cf.sha256(d) for n, d in served.items()}, {}, None)
        self.assertEqual(by_bytes, by_digest)
        self.assertEqual(by_digest['new_or_changed'], 2)

    def test_pack_site_consume_hands_the_dict_over(self):
        files = {'index.html': b'<p>x</p>'}
        out, _ = cf.pack_site(files, consume=True)
        self.assertEqual(files, {})
        self.assertEqual(out, {'index.html': b'<p>x</p>'})
        kept = {'index.html': b'<p>x</p>'}
        cf.pack_site(kept)
        self.assertEqual(kept, {'index.html': b'<p>x</p>'})

    def test_finish_takes_the_plan_over_without_clearing_it(self):
        plan = {'index.html': b'<p>x</p>', 'experiments.json': json.dumps({'experiments': []}).encode()}
        box = [plan]
        try:
            cf.finish(box, {}, {'source': {'kind': 'test'}}, consume=True)
        except ValueError:
            pass  # gates on this toy site are not the point
        self.assertEqual(box, [])
        self.assertIn('index.html', plan)  # a dict someone else holds is not emptied


if __name__ == '__main__':
    unittest.main()
