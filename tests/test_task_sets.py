"""Task sets: the frozen 14-task pilot subset (default) and the whole 71-task parent pool
(owner decision for the Gemini collaboration run, docs/plan.md)."""
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "src"))
from research_swarm.dataset import ALL_TASK_SET, PILOT_TASK_SET, FrozenDataset, run_task_set, task_set_of  # noqa: E402


class TaskSetTests(unittest.TestCase):
    def test_default_is_the_frozen_pilot_subset(self):
        dataset = FrozenDataset(ROOT)
        self.assertEqual(dataset.task_set, PILOT_TASK_SET)
        subset = json.loads((ROOT / "data" / "pilot_subset.json").read_text())["task_ids"]
        self.assertEqual([t.task_id for t in dataset.pilot_tasks], subset)

    def test_all71_is_the_whole_parent_pool_in_manifest_order(self):
        dataset = FrozenDataset(ROOT, task_set=ALL_TASK_SET)
        manifest = json.loads((ROOT / "data" / "manifest.json").read_text())["tasks"]
        self.assertEqual([t.task_id for t in dataset.pilot_tasks], [t["task_id"] for t in manifest])
        self.assertEqual(len(dataset.pilot_tasks), 71)
        for task in dataset.pilot_tasks:
            task.template_source(ROOT)  # every template hash-verifies
        with tempfile.TemporaryDirectory() as folder:
            listing = dataset.export_agent_tasks(folder)
            self.assertEqual(len(listing), 71)

    def test_config_and_saved_run_task_sets(self):
        self.assertEqual(task_set_of({}), PILOT_TASK_SET)
        self.assertEqual(task_set_of({"task_set": "all71"}), ALL_TASK_SET)
        with self.assertRaises(ValueError):
            task_set_of({"task_set": "all"})
        with self.assertRaises(ValueError):
            FrozenDataset(ROOT, task_set="nope")
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(run_task_set(folder), PILOT_TASK_SET)  # no settings.json: the pilot subset
            (Path(folder) / "settings.json").write_text(json.dumps({"effective": {"task_set": "all71"}}))
            self.assertEqual(run_task_set(folder), ALL_TASK_SET)

    def test_gemini_config_pins_all71_and_gates_accept_it_only_on_that_track(self):
        from research_swarm.launch_gates import _Gates
        config = json.loads((ROOT / "configs" / "gemini-collab.json").read_text())
        self.assertEqual((config["task_set"], config["task_count"]), ("all71", 71))

        def gate(cfg):
            gates = _Gates(ROOT, cfg, None, None, None)
            gates.run("fixed_task_manifest", gates.task_manifest)
            gates.run("frozen_dataset", gates.frozen_dataset)
            return gates.results
        results = gate(config)
        self.assertTrue(results["fixed_task_manifest"]["passed"], results["fixed_task_manifest"])
        self.assertEqual(len(results["frozen_dataset"]["task_ids"]), 71)
        baseline = dict(config)
        baseline.pop("budget_track", None)
        results = gate(baseline)
        self.assertFalse(results["fixed_task_manifest"]["passed"])
        self.assertIn("only on the gemini budget track", results["fixed_task_manifest"]["reason"])
        pilot = json.loads((ROOT / "configs" / "pilot.json").read_text())
        self.assertTrue(gate(pilot)["fixed_task_manifest"]["passed"])  # the baseline is unchanged
        self.assertEqual(len(gate(pilot)["frozen_dataset"]["task_ids"]), 14)

if __name__ == "__main__":
    unittest.main()
