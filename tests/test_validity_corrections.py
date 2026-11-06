import json
import tempfile
import unittest
from pathlib import Path

from research.analysis import validity_corrections as vc

RUN = 'gemini4argon-25-seeded-control-60m-push-20261105T202154Z'
SHA = '3dc97c5077756119eff7acd34bba669e2a0ef8f57756e60e106228fad990e67e'


class ValidityCorrectionTests(unittest.TestCase):
    def test_recorded_gemini_correction_applies_only_to_its_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / RUN
            run.mkdir()
            self.assertEqual(vc.corrected(run, {SHA: 'fraudulent', 'x': 'fraudulent'}),
                             {SHA: 'genuine', 'x': 'fraudulent'})
            other = Path(tmp) / 'other-run'
            other.mkdir()
            self.assertEqual(vc.corrected(other, {SHA: 'fraudulent'}), {SHA: 'fraudulent'})

    def test_run_id_from_results_and_mismatched_record_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'restored'
            run.mkdir()
            (run / 'results.json').write_text(json.dumps({'run': RUN}))
            self.assertEqual(vc.run_id(run), RUN)
            with self.assertRaises(ValueError):
                vc.corrected(run, {SHA: 'unresolved'})
            data = Path(tmp) / 'c.json'
            data.write_text(json.dumps({'corrections': []}))
            self.assertEqual(vc.corrected(run, {SHA: 'fraudulent'}, path=data), {SHA: 'fraudulent'})


if __name__ == '__main__':
    unittest.main()
