from pathlib import Path
import hashlib
import json
import random
import tempfile
import unittest
from research_swarm.dataset import FrozenDataset, native_evaluation_spellings
ROOT = Path(__file__).resolve().parents[1]

class DatasetTests(unittest.TestCase):
    def test_frozen_sample_reproduces(self):
        ds=FrozenDataset(ROOT)
        ids=sorted(ds.tasks)
        self.assertEqual(random.Random(ds.subset['seed']).sample(ids,14), ds.subset['task_ids'])
        self.assertEqual(len(ds.tasks),71)
    def test_templates_are_hash_verified_and_sanitized(self):
        ds=FrozenDataset(ROOT)
        for task in ds.tasks.values():
            source=task.template_source(ROOT)
            self.assertEqual(source.count('-- EDITABLE START'),2)
            self.assertEqual(source.count('-- EDITABLE END'),2)
            for keyword in ('axiom','sorry','macro','syntax'):
                self.assertNotIn(keyword,source)
            self.assertEqual(native_evaluation_spellings(source),[])
            self.assertNotIn('formal_proof',source)
            self.assertNotIn('http',source)
            self.assertIn('fail "Provide a complete proof."',source)
    def test_exports_do_not_contain_observer_material(self):
        ds=FrozenDataset(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            rows=ds.export_agent_tasks(tmp)
            self.assertEqual(len(rows),14)
            for row in rows:
                self.assertNotIn('original_statement',row)
                self.assertNotIn('source_url',row)
            self.assertEqual(len(list(Path(tmp).glob('*.lean'))),14)
    def test_named_mapping_and_dependency_lock(self):
        ds=FrozenDataset(ROOT)
        self.assertEqual(sum(t['mapped'] for t in ds.manifest['paper_mappings']),18)
        lock=json.loads((ROOT/'data/toolchain.lock.json').read_text())
        self.assertEqual(lock['lean_toolchain'],'leanprover/lean4:v4.27.0')
        self.assertEqual(len(lock['mathlib_revision']),40)

FROZEN_PILOT_IDS = ['erdos_18a', 'Erdos6_erdos_6_variants_decreasing', 'MagicSquares_exists_semi_magic_square_cubes', 'erdos_400_part_i', 'Erdos195_erdos_195_variants_leq_5_bound', 'all_fermat_squarefree', 'Erdos1092_f_asymptotic_2', 'EulerBrick_cuboidThree', 'greens14', 'Green32_hasCosetHole_empty', 'Erdos1085_erdos_1085_variants_upper_d3', "DedekindNumber_M'_three", 'Erdos23_erdos_23_variants_n1_tight', 'SumOfThreeCubes_isSumOfThreeCubes_iff_mod_9']

class FrozenIdentityTests(unittest.TestCase):
    def test_pilot_identities_never_resampled(self):
        ds=FrozenDataset(ROOT)
        self.assertEqual(ds.subset['task_ids'],FROZEN_PILOT_IDS)
        self.assertEqual(ds.manifest['frozen_identity_manifest_v1_sha256'],'3bcc6e01dcaca8914dfa6d1b3f86db5680dbe342c13f4957821bb77385fd238a')
    def test_corrections_recorded_and_exclude_pilot(self):
        ds=FrozenDataset(ROOT)
        corrected={c['task_id'] for c in ds.manifest['extraction_corrections']}
        self.assertFalse(corrected & set(FROZEN_PILOT_IDS))
        for task_id in corrected:
            entry=next(t for t in ds.manifest['tasks'] if t['task_id']==task_id)
            self.assertTrue(any('Extraction correction' in a for a in entry['template_adjustments']))
