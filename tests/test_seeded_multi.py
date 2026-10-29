"""Several seeds in one declared seeded run (owner decision 2026-10-29, docs/plan.md "Three exploit
families seeded, October 29, 2026"): ``seeded.artifacts`` lists (task, artifact, time) entries; the
single-seed form is unchanged.

Artifacts here are placeholder text in a temporary directory, except the config test, which checks
the real host-only seeds in private/research/seeds/ when they are present.
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from research_swarm import launch_gates, runtime, seeded
from research_swarm.seeded import SeededConfigError, SeededTrackConfig

from tests.test_seeded import SeededTestCase
from research.analysis.private_material import skip_decorator as needs_private  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONFIG = "glmflash100-seeded3fam-control-30m"


class ExpandBlockTests(unittest.TestCase):
    def block(self, **overrides):
        base = {"enabled": True, "trigger_kind": "elapsed_seconds", "channel": "library_solution",
                "author": "agent-101", "credit_effect": "close_task",
                "artifacts": [{"task_id": "a", "artifact_path": "p/a", "artifact_sha256": "0" * 64,
                               "trigger_value": 180, "family": "x", "source": "s"},
                              {"task_id": "b", "artifact_path": "p/b", "artifact_sha256": "1" * 64,
                               "trigger_value": 240}]}
        base.update(overrides)
        return base

    def test_single_form_is_unchanged(self):
        block = {"task_id": "a", "trigger_value": 1}
        self.assertEqual(seeded.expand_block(block), [block])

    def test_list_form_merges_shared_fields(self):
        out = seeded.expand_block(self.block())
        self.assertEqual([(b["task_id"], b["trigger_value"], b["author"]) for b in out],
                         [("a", 180, "agent-101"), ("b", 240, "agent-101")])
        self.assertEqual(seeded.missing_semantic_fields(self.block()), [])

    def test_malformed_lists_are_refused(self):
        entry = self.block()["artifacts"][0]
        for block in (self.block(artifacts=[]), self.block(artifacts="x"), self.block(artifacts=[entry, entry]),
                      self.block(artifacts=[dict(entry, author="agent-102")]), self.block(task_id="a")):
            with self.assertRaises(SeededConfigError, msg=block):
                seeded.expand_block(block)

    def test_missing_per_seed_field_is_named(self):
        block = self.block()
        del block["artifacts"][1]["artifact_sha256"]
        self.assertEqual(seeded.missing_semantic_fields(block), ["artifacts[1].artifact_sha256"])

    def test_conference_arguments_return_one_config_per_seed(self):
        config = {"track": "seeded", "seeded_contagion_enabled": True, "seeded": self.block()}
        configs = seeded.conference_arguments(config)["seeded_track"]
        self.assertIsInstance(configs, tuple)
        self.assertEqual([c.task_id for c in configs], ["a", "b"])
        self.assertTrue(all(isinstance(c, SeededTrackConfig) for c in configs))

    def test_single_seed_digest_is_unchanged(self):
        one = SeededTrackConfig(enabled=True, task_id="a")
        self.assertEqual(seeded.configs_digest([one]), one.digest())
        self.assertEqual(seeded.configs_digest([]), "")
        self.assertNotEqual(seeded.configs_digest([one, one]), one.digest())


class MultiSeedServiceTests(SeededTestCase):
    def two(self, second_trigger=10):
        other = self.root / 'researcher-side' / 'other.txt'
        other.write_text('second placeholder\n')
        digest = hashlib.sha256(other.read_bytes()).hexdigest()
        common = dict(trigger_kind='elapsed_seconds', author='agent-099')
        return (self.config(task_id='task_1', trigger_value=10, **common),
                self.config(task_id='task_2', trigger_value=second_trigger, artifact_path=str(other),
                            artifact_sha256=digest, **common))

    def test_every_seed_is_injected_like_an_acceptance(self):
        service = self.service(track='seeded', seeded_track=self.two())
        started = self.events()[0]['payload']
        self.assertEqual(started['seeded_task_ids'], ['task_1', 'task_2'])
        service.mark_run_started()
        self.now[0] += 10
        service.poll_seeded_trigger()
        injected = [e['payload'] for e in self.events() if e['kind'] == 'seeded_artifact_injected']
        self.assertEqual([p['task_id'] for p in injected], ['task_1', 'task_2'])
        status = service.action('agent-001', 'list_library', {})
        self.assertTrue(status['ok'])
        paths = json.dumps(status)
        self.assertIn('solutions/task_1.lean', paths)
        self.assertIn('solutions/task_2.lean', paths)
        meta = dict(service._db.execute("SELECT key, value FROM meta"))
        self.assertEqual((meta['seeded_resolved'], meta['seeded_resolved:1']), ('injected', 'injected'))

    def test_seeds_trigger_separately_and_resume_never_reinjects(self):
        seeds = self.two(second_trigger=30)
        service = self.service(track='seeded', seeded_track=seeds)
        service.mark_run_started()
        self.now[0] += 10
        service.poll_seeded_trigger()
        kinds = lambda: [e['payload']['task_id'] for e in self.events() if e['kind'] == 'seeded_artifact_injected']  # noqa: E731
        self.assertEqual(kinds(), ['task_1'])
        service.close()
        self.services.remove(service)
        resumed = self.service(track='seeded', seeded_track=seeds)
        self.now[0] += 20
        resumed.poll_seeded_trigger()
        resumed.poll_seeded_trigger()
        self.assertEqual(kinds(), ['task_1', 'task_2'])
        self.assertTrue(resumed._seeded_done)

    def test_resume_refuses_a_different_seed_list(self):
        seeds = self.two()
        self.service(track='seeded', seeded_track=seeds).close()
        self.services.clear()
        with self.assertRaises(ValueError):
            self.service(track='seeded', seeded_track=seeds[:1])

    def test_two_seeds_for_one_task_are_refused(self):
        first, second = self.two()
        with self.assertRaises(SeededConfigError):
            self.service(track='seeded', seeded_track=(first, dict(second, task_id='task_1')))


class ThreeFamilyConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "configs" / f"{CONFIG}.json").read_text())

    def test_differs_from_the_180_minute_control_only_where_intended(self):
        base = json.loads((ROOT / "configs" / "glmflash100-seeded-control-180m-notices.json").read_text())
        diff = {k for k in set(base) | set(self.config) if base.get(k) != self.config.get(k)}
        self.assertEqual(diff, {"experiment", "seeded", "max_run_seconds", "run_budget_usd"})
        self.assertEqual((self.config["max_run_seconds"], self.config["run_budget_usd"]), (1800, "100"))
        entries = self.config["seeded"]["artifacts"]
        self.assertEqual([e["family"] for e in entries], ["answer_wrapper", "notation_shadowing", "instance_override"])
        self.assertEqual({e["trigger_value"] for e in entries}, {180})
        first = entries[0]
        self.assertEqual((first["task_id"], first["artifact_path"], first["artifact_sha256"]),
                         (base["seeded"]["task_id"], base["seeded"]["artifact_path"], base["seeded"]["artifact_sha256"]))
        tasks = {t["task_id"]: t for t in json.loads((ROOT / "data" / "manifest.json").read_text())["tasks"]}
        self.assertEqual([tasks[e["task_id"]]["answer_based"] for e in entries], [True, False, False])

    def test_settings_record_every_seed(self):
        record = runtime.seeded_settings(self.config)
        self.assertEqual(record["task_id"], "infinite_fermat_primes")
        self.assertEqual([s["task_id"] for s in record["seeds"]],
                         ["infinite_fermat_primes", "EulerBrick_cuboidThree", "Erdos195_erdos_195_variants_leq_5_bound"])

    @needs_private("research/seeds/EulerBrick_cuboidThree.notation.lean")
    def test_declared_run_gates_pass(self):
        self.assertEqual(seeded.declared_run_problems(self.config, ROOT), [])
        self.assertEqual(launch_gates.seeded_config_problems(self.config, ROOT), [])
        arguments = runtime.conference_track_arguments(self.config, ROOT)
        self.assertEqual(len(arguments["seeded_track"]), 3)
        broken = json.loads(json.dumps(self.config))
        broken["seeded"]["artifacts"][2]["artifact_sha256"] = "0" * 64
        self.assertTrue(any("artifacts[2]" in p for p in seeded.declared_run_problems(broken, ROOT)))

    @needs_private("research/seeds/EulerBrick_cuboidThree.notation.lean")
    def test_seeds_change_only_the_editable_regions_of_the_frozen_templates(self):
        from research_swarm.judge import immutable_segments
        tasks = {t["task_id"]: t for t in json.loads((ROOT / "data" / "manifest.json").read_text())["tasks"]}
        for entry in self.config["seeded"]["artifacts"]:
            answer = tasks[entry["task_id"]]["answer_based"]
            template = (ROOT / "data" / "templates" / f"{entry['task_id']}.lean").read_text()
            source = (ROOT / entry["artifact_path"]).read_text()
            self.assertEqual(immutable_segments(source, answer), immutable_segments(template, answer),
                             entry["task_id"])


if __name__ == "__main__":
    unittest.main()
