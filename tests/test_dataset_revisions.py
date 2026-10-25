"""Dataset revision 2 (docs/plan.md, 2026-10-21): every native-evaluation spelling is prohibited
in templates; revision 1 stays archived for runs made before the change."""
from pathlib import Path
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest

from research_swarm.dataset import (CURRENT_REVISION, NATIVE_EVALUATION, FrozenDataset, PILOT_TASK_SET, ALL_TASK_SET,
                                    native_evaluation_spellings, revision_of_manifest_sha256, run_dataset_revision)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'research' / 'analysis'))
import validate_dataset  # noqa: E402
import timeline  # noqa: E402

_spec = importlib.util.spec_from_file_location('prepare_dataset', ROOT / 'scripts' / 'prepare_dataset.py')
prepare_dataset = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prepare_dataset)

V1_MANIFEST = 'df9fbeaa3cfb5aae72096566faaf7b05f93eacbad91e7abcc8b7a15ac12761cd'
V2_MANIFEST = '8a114c4c2f6345b8846426900ce9afb2d851565546f925c308cd23e2ead1e653'
CHANGED = {'Erdos1148_erdos_1148_variants_weaker', 'WrittenOnTheWallII_Test_C6_avg_dist',
           'WrittenOnTheWallII_Test_C6_size', 'WrittenOnTheWallII_Test_Star5_indep',
           'WrittenOnTheWallII_Test_house_cvetkovic'}
SPELLINGS = ['native_decide', 'decide +native', 'decide +kernel +native', 'decide (config := {native := true})',
             'exact Lean.ofReduceBool _ _ rfl', 'ofReduceNat']


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class NativeDetectionTests(unittest.TestCase):
    def test_every_spelling_is_detected(self):
        for text in SPELLINGS:
            with self.subTest(text=text):
                self.assertTrue(native_evaluation_spellings(text))
                self.assertTrue(prepare_dataset.uses_native(text))
        for text in ('decide', 'nativeFoo := 1', 'native := false', 'signature', 'decide +kernel'):
            with self.subTest(text=text):
                self.assertFalse(native_evaluation_spellings(text))
                self.assertFalse(prepare_dataset.uses_native(text))

    def test_revision_1_rule_is_literal(self):
        self.assertTrue(prepare_dataset.uses_native('native_decide', 1))
        self.assertFalse(prepare_dataset.uses_native('decide +native', 1))

    def test_builder_and_runtime_patterns_agree(self):
        self.assertEqual(prepare_dataset.NATIVE_EVALUATION.pattern, NATIVE_EVALUATION.pattern)
        self.assertEqual(prepare_dataset.REVISION, CURRENT_REVISION)
        self.assertEqual(prepare_dataset.REVISION_1_MANIFEST_SHA256, V1_MANIFEST)

    def test_builder_removes_native_helpers_and_dependents(self):
        source = ('import Mathlib\n\nnamespace T\n\n'
                  'def f (n : Nat) : Nat := n + 1\n\n'
                  '@[category test]\ntheorem h1 : f 1 = 2 := by\n  decide +native\n\n'
                  '@[category test]\ntheorem h2 : f 1 = 2 ∧ True := ⟨h1, trivial⟩\n\n'
                  '@[category test]\ntheorem h3 : f 2 = 3 := by\n  decide (config := {native := true})\n\n'
                  '@[category test]\ntheorem h4 : f 0 = 1 := by\n  rfl\n\n'
                  '@[category test]\ntheorem target : f 3 = 4 := by\n  sorry\n\nend T\n')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'FormalConjectures' / 'X.lean'
            path.parent.mkdir()
            path.write_text(source)
            old = {c['theorem_name']: c['template'] for c in prepare_dataset.candidates(Path(tmp), True, 1)}
            new = {c['theorem_name']: c['template'] for c in prepare_dataset.candidates(Path(tmp), True, 2)}
        self.assertIn('decide +native', old['T.target'])
        template = new['T.target']
        for name in ('h1', 'h2', 'h3'):
            self.assertNotIn('theorem ' + name, template)
        self.assertIn('theorem h4', template)
        self.assertIn('def f', template)
        self.assertFalse(native_evaluation_spellings(template))
        self.assertIn('theorem target : f 3 = 4 := by', template)


class RevisionTests(unittest.TestCase):
    def test_current_revision_is_2_and_v1_archived(self):
        current, v1 = FrozenDataset(ROOT), FrozenDataset(ROOT, revision=1)
        self.assertEqual((current.revision, current.manifest['dataset_revision']), (2, 2))
        self.assertNotIn('dataset_revision', v1.manifest)
        self.assertEqual(sha(ROOT / 'data/manifest.json'), V2_MANIFEST)
        self.assertEqual(sha(ROOT / 'data/revisions/v1/manifest.json'), V1_MANIFEST)
        self.assertEqual(revision_of_manifest_sha256(ROOT, V1_MANIFEST), 1)
        self.assertEqual(revision_of_manifest_sha256(ROOT, V2_MANIFEST), 2)
        with self.assertRaises(ValueError):
            FrozenDataset(ROOT, revision=3)

    def test_same_identities_and_only_five_templates_change(self):
        current, v1 = FrozenDataset(ROOT, ALL_TASK_SET), FrozenDataset(ROOT, ALL_TASK_SET, revision=1)
        self.assertEqual(list(current.tasks), list(v1.tasks))
        self.assertEqual(current.subset['task_ids'], v1.subset['task_ids'])
        changed = {t for t in current.tasks
                   if current.tasks[t].template_source(ROOT) != v1.tasks[t].template_source(ROOT)}
        self.assertEqual(changed, CHANGED)
        self.assertFalse(changed & set(current.subset['task_ids']))
        for t in current.tasks:
            if t not in CHANGED:
                self.assertEqual(current.tasks[t].sha256, v1.tasks[t].sha256)
                self.assertEqual(current.tasks[t].theorem_name, v1.tasks[t].theorem_name)

    def test_pilot_templates_unchanged(self):
        current, v1 = FrozenDataset(ROOT, PILOT_TASK_SET), FrozenDataset(ROOT, PILOT_TASK_SET, revision=1)
        self.assertEqual([t.sha256 for t in current.pilot_tasks], [t.sha256 for t in v1.pilot_tasks])

    def test_revision_history_records_old_and_new_hashes(self):
        current, v1 = FrozenDataset(ROOT, ALL_TASK_SET), FrozenDataset(ROOT, ALL_TASK_SET, revision=1)
        history = current.manifest['revision_history']
        self.assertEqual(history[0]['manifest_sha256'], V1_MANIFEST)
        rows = {r['task_id']: r for r in history[1]['changed_templates']}
        self.assertEqual(set(rows), CHANGED)
        for task_id, row in rows.items():
            self.assertEqual(row['revision_1_template_sha256'], v1.tasks[task_id].sha256)
            self.assertEqual(row['revision_2_template_sha256'], current.tasks[task_id].sha256)
            entry = next(t for t in current.manifest['tasks'] if t['task_id'] == task_id)
            self.assertTrue(any('2026-10-21' in a for a in entry['template_adjustments']))

    def test_no_native_evaluation_in_current_templates(self):
        current, v1 = FrozenDataset(ROOT, ALL_TASK_SET), FrozenDataset(ROOT, ALL_TASK_SET, revision=1)
        for task in current.tasks.values():
            self.assertEqual(native_evaluation_spellings(task.template_source(ROOT)), [], task.task_id)
        dirty = {t for t, task in v1.tasks.items() if native_evaluation_spellings(task.template_source(ROOT))}
        self.assertEqual(dirty, CHANGED)

    def test_changed_templates_keep_target_and_editable_regions(self):
        current = FrozenDataset(ROOT, ALL_TASK_SET)
        for task_id in sorted(CHANGED):
            task = current.tasks[task_id]
            static = validate_dataset.static_checks(task, task.template_source(ROOT))
            self.assertEqual(static['problems'], [], task_id)
            self.assertFalse(static['fatal'])
        v1 = FrozenDataset(ROOT, ALL_TASK_SET, revision=1)
        for task_id in sorted(CHANGED):
            problems = validate_dataset.static_checks(v1.tasks[task_id], v1.tasks[task_id].template_source(ROOT))['problems']
            self.assertTrue(any('native evaluation' in p for p in problems), task_id)

    def test_configs_pin_the_current_revision(self):
        subset = sha(ROOT / 'data/pilot_subset.json')
        for path in sorted((ROOT / 'configs').glob('*.json')):
            pin = json.loads(path.read_text()).get('fixed_task_manifest')
            if isinstance(pin, dict):
                with self.subTest(config=path.name):
                    self.assertEqual(pin['parent_manifest_sha256'], V2_MANIFEST)
                    self.assertIn(pin['sha256'], (subset, V2_MANIFEST))


class RunRevisionTests(unittest.TestCase):
    def run_dir(self, settings=None, tasks=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run = Path(tmp.name)
        if settings is not None:
            (run / 'settings.json').write_text(json.dumps(settings))
        if tasks is not None:
            (run / 'conference').mkdir()
            db = sqlite3.connect(run / 'conference' / 'state.sqlite')
            db.execute('CREATE TABLE tasks (task_id TEXT, theorem_name TEXT, answer_based INTEGER, template_sha256 TEXT)')
            db.executemany('INSERT INTO tasks VALUES (?, ?, 0, ?)', [(t, t, h) for t, h in tasks.items()])
            db.commit()
            db.close()
        return run

    def test_resolution_order(self):
        pin = lambda m: {'effective': {'fixed_task_manifest': {'path': 'data/manifest.json', 'sha256': m,
                                                               'parent_manifest_sha256': m}}}
        self.assertEqual(run_dataset_revision(self.run_dir(pin(V1_MANIFEST)), ROOT), 1)
        self.assertEqual(run_dataset_revision(self.run_dir(pin(V2_MANIFEST)), ROOT), 2)
        self.assertEqual(run_dataset_revision(self.run_dir({'effective': {'dataset_revision': 1}}), ROOT), 1)
        self.assertEqual(run_dataset_revision(self.run_dir(), ROOT), CURRENT_REVISION)
        v1 = FrozenDataset(ROOT, ALL_TASK_SET, revision=1)
        old = {t: v1.tasks[t].sha256 for t in sorted(CHANGED)}
        self.assertEqual(run_dataset_revision(self.run_dir(tasks=old), ROOT), 1)
        pilot = {t.task_id: t.sha256 for t in FrozenDataset(ROOT, revision=1).pilot_tasks}  # identical in both revisions: ambiguous
        self.assertEqual(run_dataset_revision(self.run_dir(tasks=pilot), ROOT), CURRENT_REVISION)

    def test_old_run_templates_resolve_for_reevaluation_and_timeline(self):
        v1 = FrozenDataset(ROOT, ALL_TASK_SET, revision=1)
        task_id = 'WrittenOnTheWallII_Test_C6_size'
        old = v1.tasks[task_id].template_source(ROOT)
        self.assertIn('decide +native', old)
        templates = timeline.load_run_templates(ROOT, {task_id: v1.tasks[task_id].sha256})
        self.assertEqual(templates[task_id], old)
        current = timeline.load_run_templates(ROOT, {})
        self.assertNotIn('decide +native', current[task_id])


if __name__ == '__main__':
    unittest.main()
