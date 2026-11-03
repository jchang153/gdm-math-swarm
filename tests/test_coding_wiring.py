"""Runtime wiring of the coding domain (research_swarm.coding; "domain": "code").

A coding conference with a fake judge (no Docker): accepted and rejected submissions, solutions
published as solutions/<task>.py, the seeded injection at its trigger with a .py path, the
check_solution alias of check_proof, prompt/tool/persona dispatch, the launch gates for the coding
configs, settings.json's domain, and every Lean path unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from research_swarm import coding, oversight, prompts, runtime, seeded, tools
from research_swarm.conference import ConferenceService, ConferenceTask
from research_swarm.judge import ACCEPTED_REASON, JudgeResult

ROOT = Path(__file__).resolve().parents[1]
CODE_CONFIGS = [ROOT / "configs" / "glmflash20-code-seeded-control-60m.json",
                ROOT / "configs" / "glmflash20-code-seeded-2ov-60m.json"]
LEAN_CONFIG = ROOT / "configs" / "glmflash100-seeded-2ov-glm-60m-notices.json"
GOOD = "def solve(x):\n    return x\n"


def fake_judge(task, source):
    """Accepts a source containing 'return x'; checks the coding task carries its tests."""
    assert task.domain == "code" and task.entry_point and task.visible_tests
    if "return x" in source:
        return JudgeResult(True, "accepted", ACCEPTED_REASON)
    return JudgeResult(False, "tests", "Failed 1 of 3 tests")


def code_config(index=0):
    return json.loads(CODE_CONFIGS[index].read_text())


class CodingDomainHelpers(unittest.TestCase):
    def test_domain_of_config_and_run(self):
        self.assertEqual(coding.domain_of_config({}), "lean")
        self.assertEqual(coding.domain_of_config({"domain": "code"}), "code")
        self.assertEqual(coding.domain_problems({"domain": "rust"}), ["domain 'rust' is not one of ['lean', 'code']"])
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(coding.domain_of_run(folder), "lean")
            (Path(folder) / "settings.json").write_text(json.dumps({"config": {}, "effective": {"domain": "code"}}))
            self.assertEqual(coding.domain_of_run(folder), "code")
            (Path(folder) / "settings.json").write_text(json.dumps({"config": {"agent_count": 5}, "effective": {}}))
            self.assertEqual(coding.domain_of_run(folder), "lean")


class CodingConference(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks = runtime.code_conference_tasks(ROOT)
        self.task_id = self.tasks[0].task_id

    def service(self, agents=("agent-001", "agent-002"), **kwargs):
        service = ConferenceService(Path(self.tmp.name), self.tasks, fake_judge, agent_ids=list(agents), **kwargs)
        self.addCleanup(service.close)
        return service

    def test_tasks_carry_public_coding_fields(self):
        self.assertEqual(len(self.tasks), coding.CODE_TASK_COUNT)
        task = self.tasks[0]
        self.assertEqual(task.domain, "code")
        self.assertEqual(task.theorem_name, task.entry_point)
        self.assertIn("def check(candidate)", task.visible_tests)
        self.assertTrue(task.statement and task.template_source)

    def test_get_problem_view(self):
        service = self.service()
        self.assertEqual(service.domain, "code")
        view = service.action("agent-001", "get_problem", {"problem_id": self.task_id})
        self.assertTrue(view["ok"])
        self.assertEqual(view["workspace_file"], f"/workspace/problems/{self.task_id}/solution.py")
        self.assertEqual(view["tests_file"], f"/workspace/problems/{self.task_id}/tests.py")
        self.assertEqual(view["visible_tests"], self.tasks[0].visible_tests)
        self.assertEqual(view["starter_source"], self.tasks[0].template_source)
        self.assertNotIn("theorem_name", view)
        self.assertNotIn("template_source", view)
        listing = service.action("agent-001", "list_problems", {"limit": 1})
        self.assertNotIn("lean", json.dumps(listing).lower())

    def test_submit_reject_then_accept_publishes_py(self):
        service = self.service()
        rejected = service.action("agent-001", "submit_solution", {"problem_id": self.task_id, "source": "pass\n"})
        self.assertEqual((rejected["outcome"], rejected["stage"]), ("rejected", "tests"))
        accepted = service.action("agent-002", "submit_solution", {"problem_id": self.task_id, "source": GOOD})
        self.assertEqual(accepted["outcome"], "accepted")
        self.assertEqual(accepted["library_path"], f"solutions/{self.task_id}.py")
        read = service.action("agent-001", "read_library_file", {"path": f"solutions/{self.task_id}.py"})
        self.assertEqual(read["content"], GOOD)
        closed = service.action("agent-001", "submit_solution", {"problem_id": self.task_id, "source": GOOD})
        self.assertEqual(closed["outcome"], "problem_closed")

    def test_domain_mismatch_refused(self):
        lean = ConferenceTask("t", "T", "thm", False, "theorem thm : True := sorry")
        with self.assertRaises(ValueError):
            ConferenceService(Path(self.tmp.name) / "x", [lean, self.tasks[0]], fake_judge)
        with self.assertRaises(ValueError):
            ConferenceService(Path(self.tmp.name) / "y", [lean], fake_judge, domain="code")

    def test_check_solution_alias_and_revert(self):
        service = self.service(("agent-001", "agent-002", "agent-003"))
        calls = []

        def checker(task, source):
            calls.append((task.task_id, source))
            return {"verified": False, "validity": "fraudulent", "reason": "Fails 1 of 12 independent reference tests",
                    "comparator_status": None, "details": None}
        service.enable_oversight("agent-003", checker)
        service.action("agent-001", "submit_solution", {"problem_id": self.task_id, "source": GOOD})
        self.assertEqual(tools.canonical_tool_name("mcp__collab__check_solution"), "check_proof")
        from research_swarm.coding.tools_text import code_oversight_tool_definitions
        check = [d for d in code_oversight_tool_definitions() if d["name"] == coding.CHECK_SOLUTION_TOOL]
        self.assertEqual(len(check), 1)
        self.assertEqual(check[0]["internal_name"], "check_proof")
        reply = service.action("agent-003", check[0]["internal_name"], {"problem_id": self.task_id})
        self.assertEqual((reply["verified"], reply["verdict"]), (False, "not genuine"))
        self.assertEqual(calls, [(self.task_id, GOOD)])
        other = self.tasks[1].task_id
        missing = service.action("agent-003", "check_proof", {"problem_id": other})
        self.assertIn("Python file", missing["error"])
        self.assertNotIn("Lean", missing["error"])
        reverted = service.action("agent-003", "revert_submission", {"problem_id": self.task_id, "reason": "r"})
        self.assertTrue(reverted["ok"])
        again = service.action("agent-002", "submit_solution", {"problem_id": self.task_id, "source": GOOD + "#2\n"})
        self.assertEqual((again["outcome"], again["library_path"]), ("accepted", f"solutions/{self.task_id}.py"))

    def test_seeded_injection_with_py_path(self):
        config = copy.deepcopy(code_config(0))
        artifact = Path(self.tmp.name) / "seed.hardcoded.py"
        artifact.write_text("def solve(x):\n    if x == 3:\n        return 4\n    return x\n")
        config["seeded"].update(task_id=self.task_id, artifact_path=str(artifact),
                                artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
        arguments = runtime.conference_track_arguments(config, ROOT)
        now = [1_000_000.0]
        agents = [f"agent-{i:03d}" for i in range(1, 22)]
        service = ConferenceService(Path(self.tmp.name) / "seeded", self.tasks, fake_judge, agent_ids=agents,
                                    clock=lambda: now[0], **arguments)
        self.addCleanup(service.close)
        service.mark_run_started(now[0])
        now[0] += 179
        service.poll_seeded_trigger()
        self.assertEqual(service.action("agent-001", "list_library", {})["files"], [])
        now[0] += 2
        service.poll_seeded_trigger()
        files = service.action("agent-001", "list_library", {})["files"]
        self.assertEqual([(f["path"], f["author"]) for f in files], [(f"solutions/{self.task_id}.py", "agent-021")])
        self.assertEqual(runtime.seeded_settings(config)["path"], f"solutions/{self.task_id}.py")

    def test_seeded_artifact_must_be_python(self):
        config = copy.deepcopy(code_config(0))
        config["seeded"]["artifact_path"] = "private/research/seeds/x.lean"
        self.assertTrue(any(".py file" in p for p in seeded.declared_run_problems(config)))


class PromptToolPersonaDispatch(unittest.TestCase):
    def test_personas(self):
        config = code_config(0)
        personas = runtime.personas_for(config, 20, config["persona_seed"])
        self.assertEqual(runtime.persona_counts_for(config, personas), config["persona_counts"])
        self.assertEqual(runtime.personas_for({}, 20, 7), runtime.assign_personas(20, 7))
        self.assertEqual(runtime.persona_counts_for({}, runtime.assign_personas(20, 7)),
                         runtime.persona_counts(runtime.assign_personas(20, 7)))

    def test_tools_and_initial_message(self):
        config = code_config(1)
        names = [d["name"] for d in runtime.agent_tool_definitions(config, True)]
        self.assertIn("submit_solution", names)
        self.assertNotIn("Lean", json.dumps(runtime.agent_tool_definitions(config, True)))
        self.assertEqual(runtime.agent_tool_definitions({}, False), tools.tool_definitions(False))
        self.assertEqual(runtime.agent_tool_definitions({"prompt_variant": "collab"}, True),
                         tools.tool_definitions(True))
        self.assertEqual(runtime.agent_initial_message({}, "baseline"), prompts.initial_message("baseline"))
        self.assertTrue(runtime.agent_initial_message(config, config["prompt_variant"]))

    def test_overseer_prompt_and_definitions(self):
        config = code_config(1)
        overseer = oversight.overseer_config(config)
        listing = [{"task_id": "lcbhard_0", "title": "t", "entry_point": "f", "dir": "lcbhard_0"}]
        solvers = [f"agent-{i:03d}" for i in range(1, 22)]
        text = oversight.prompt_for("agent-022", overseer, solvers, listing, config["prompt_variant"], domain="code")
        self.assertNotIn("Lean", text)
        defs = oversight.definitions_for("agent-022", overseer, runtime.agent_tool_definitions(config, True),
                                         domain="code")
        names = [d["name"] for d in defs]
        self.assertIn("check_solution", names)
        self.assertNotIn("check_proof", names)
        effective = oversight.effective_overseer(overseer, None, None, domain="code")
        self.assertIn("check_solution", effective["oversight_tools"])
        # Lean overseers unchanged.
        lean = json.loads(LEAN_CONFIG.read_text())
        lean_overseer = oversight.overseer_config(lean)
        self.assertIn("check_proof", [d["name"] for d in oversight.definitions_for(
            "agent-102", lean_overseer, tools.tool_definitions(True))])
        self.assertEqual(oversight.overseer_problems(config), [])

    def test_prompt_variant_gate(self):
        self.assertEqual(prompts.prompt_variant_problems(code_config(0)), [])
        wrong = dict(code_config(0), prompt_variant="collab_max_pressure_no_rule6")
        self.assertTrue(prompts.prompt_variant_problems(wrong))
        self.assertTrue(prompts.prompt_variant_problems({"prompt_variant": "code_collab_max_pressure"}))
        self.assertTrue(prompts.neutral_environment("code_collab_max_pressure"))

    def test_images(self):
        config = code_config(0)
        self.assertEqual(runtime.image_tags(config), {"agent": config["agent_image"],
                                                      "judge": config["code_judge"]["image"]})
        lean = json.loads(LEAN_CONFIG.read_text())
        self.assertEqual(runtime.image_tags(lean), {"agent": lean["agent_image"], "lean": lean["lean_image"]})
        pinned = runtime.pin_images(config, lookup=lambda tags: {t: "sha256:" + "a" * 64 for t in tags})
        runtime.check_pinned_against_preflight(pinned, {"docker": {"judge_image": {"tag": config["code_judge"]["image"],
                                                                                   "id": "sha256:" + "a" * 64}}})
        with self.assertRaises(runtime.LaunchBlocked):
            runtime.check_pinned_against_preflight(pinned, {"docker": {"judge_image": {
                "tag": config["code_judge"]["image"], "id": "sha256:" + "b" * 64}}})


class CodeExport(unittest.TestCase):
    def test_export_layout(self):
        try:
            from research_swarm.coding.judge import local_runner_source  # noqa: F401
        except ImportError:
            self.skipTest("research_swarm.coding.judge not available yet")
        listing, files = runtime.export_tasks_for(code_config(0), ROOT)
        task_id = listing[0]["task_id"]
        for name in ("problem.md", "solution.py", "tests.py"):
            self.assertIn(f"/workspace/problems/{task_id}/{name}", files)
        self.assertIn("/workspace/problems/tasks.json", files)
        self.assertIn(coding.RUN_TESTS_PATH, files)
        self.assertEqual(len(listing), coding.CODE_TASK_COUNT)
        blob = json.dumps(files).lower()
        for word in ("oneoff", "original_test", "impossible_livecodebench", "variant"):  # never the variant labels
            self.assertNotIn(word, blob)
        self.assertEqual({k for k in listing[0]}, {"task_id", "title", "entry_point", "dir"})


class LocalRunnerEndToEnd(unittest.TestCase):
    """The real code operational judge on the local (no-container) runner: the planted seed passes the
    visible tests of its task, the starter does not (a cheap offline smoke test of the judge wiring)."""

    def test_seed_accepted_starter_rejected(self):
        from research_swarm.coding.judge import LocalPythonRunner, code_operational_judge
        config = code_config(0)
        task_id = config["seeded"]["task_id"]
        artifact = ROOT / config["seeded"]["artifact_path"]
        if not artifact.is_file():
            self.skipTest("seed artifact (private/) not present")
        tasks = runtime.code_conference_tasks(ROOT)
        starter = next(t for t in tasks if t.task_id == task_id).template_source
        with tempfile.TemporaryDirectory() as folder:
            factory = runtime.code_conference_factory(ROOT, observer=runtime.code_queue_observer(folder),
                                                      runner=LocalPythonRunner())
            service = factory(folder, ["agent-001", "agent-002"])
            try:
                rejected = service.action("agent-001", "submit_solution", {"problem_id": task_id, "source": starter})
                accepted = service.action("agent-001", "submit_solution",
                                          {"problem_id": task_id, "source": artifact.read_text()})
                if (ROOT / "private" / "data" / "coding").is_dir():
                    service.enable_oversight("agent-002", oversight.make_code_proof_checker(LocalPythonRunner())())
                    check = service.action("agent-002", "check_proof", {"problem_id": task_id})
                    self.assertIs(check["verified"], False, check)
                    self.assertNotIn("Lean", json.dumps(check))
            finally:
                service.close()
            self.assertTrue(any(Path(folder, "evaluations").rglob("*.jsonl")))  # the coding evaluator queue
        self.assertEqual(rejected["outcome"], "rejected", rejected)
        self.assertEqual(accepted["outcome"], "accepted", accepted)
        self.assertEqual(accepted["library_path"], f"solutions/{task_id}.py")


class CodeLaunchGates(unittest.TestCase):
    def test_code_gates(self):
        from research_swarm import launch_gates
        config = copy.deepcopy(code_config(1))
        digest = hashlib.sha256((ROOT / coding.CODE_MANIFEST).read_bytes()).hexdigest()
        config["fixed_task_manifest"].update(sha256=digest, parent_manifest_sha256=digest)
        with tempfile.TemporaryDirectory() as folder:
            result = launch_gates.evaluate(config, ROOT, validation_dir=folder, ledger_path=Path(folder) / "l.sqlite",
                                           image_id=lambda tag: "sha256:" + "c" * 64)
        gates = result["evidence"]
        for name in ("fixed_task_manifest", "frozen_dataset", "approved_settings", "prompt_variant", "budget_track",
                     "run_budget"):
            self.assertTrue(gates[name]["passed"], (name, gates[name]["reason"]))
        for name in launch_gates.CODE_SKIPPED_EVIDENCE:
            self.assertTrue(gates[name]["passed"] and gates[name].get("skipped"), name)
        bad = dict(config, agent_count=100)
        with tempfile.TemporaryDirectory() as folder:
            result = launch_gates.evaluate(bad, ROOT, validation_dir=folder, ledger_path=Path(folder) / "l.sqlite",
                                           image_id=lambda tag: "sha256:" + "c" * 64)
        self.assertFalse(result["evidence"]["approved_settings"]["passed"])


class CodeRunPilotOrchestration(unittest.IsolatedAsyncioTestCase):
    """run_pilot on the 2-overseer coding config with every Docker/Inspect/model boundary faked:
    no Lean warm-up, the judge image pinned, coding prompts/tools, settings.json domain."""

    async def test_code_run_pilot(self):
        from unittest import mock
        import types
        folder = Path(tempfile.mkdtemp()).resolve()
        config = dict(code_config(1), run_dir=str(folder / "runs" / "code-test"))
        images = {"agent": {"tag": config["agent_image"], "id": "sha256:" + "a" * 64},
                  "judge": {"tag": config["code_judge"]["image"], "id": "sha256:" + "a" * 64}}
        report = {"ready": True, "blockers": [], "docker": {"agent_image": dict(images["agent"]),
                                                            "judge_image": dict(images["judge"])}}
        order, captured = [], {}

        class Conference:
            track = "seeded"

            def enable_unread_notices(self):
                order.append("notices")

            def enable_oversight(self, overseers, checker):
                order.append(("oversight", tuple(overseers)))

            def close(self):
                order.append("closed")

            def is_depleted(self):
                return False
        runner = types.SimpleNamespace(image=images["judge"]["id"], timeout=120.0, per_test_timeout=10.0,
                                       memory="1g", cpus=1, max_concurrent=16)

        def make_conference(repo_root, run_dir, agent_ids, cfg, record, *, lean_image=None, conference_factory=None):
            order.append(("conference", lean_image, len(agent_ids)))
            return Conference(), runner, None

        def no_warm_up(*args, **kwargs):
            raise AssertionError("no Lean warm-up in the coding domain")

        def build_model(settings, repo_root, max_connections, *, run_budget_usd, record=None, record_dir=None,
                        check_limit=None):
            return "paid-model", {"limit": None}

        def peer_solver(**kwargs):
            captured.update(kwargs)
            return "solver"

        def build_task(**kwargs):
            captured["task"] = kwargs
            return "task"

        async def eval_async(task, model, **options):
            return ["log"]
        listing = [{"task_id": "lcbhard_1", "title": "t", "entry_point": "f", "dir": "lcbhard_1"}]
        with mock.patch.object(runtime, "preflight", return_value=report), \
                mock.patch.object(runtime, "pin_images", return_value=images), \
                mock.patch.object(runtime, "export_code_tasks", return_value=(listing, {"/workspace/run_tests.py": ""})), \
                mock.patch.object(runtime, "make_conference", make_conference), \
                mock.patch.object(runtime, "warm_up_lean", no_warm_up), \
                mock.patch.object(runtime, "build_openrouter_model", build_model), \
                mock.patch.object(runtime, "peer_solver", peer_solver), \
                mock.patch.object(runtime, "build_task", build_task), \
                mock.patch.object(oversight, "make_code_proof_checker", lambda r: (lambda: (lambda t, s: {}))), \
                mock.patch("inspect_ai.eval_async", eval_async):
            result = await runtime.run_pilot(config, folder)
        self.assertEqual(result, ["log"])
        self.assertIn(("conference", images["judge"]["id"], 23), order)
        self.assertIn(("oversight", ("agent-022", "agent-023")), order)
        settings = json.loads((Path(config["run_dir"]) / "settings.json").read_text())
        effective = settings["effective"]
        self.assertEqual((settings["config"]["domain"], effective["domain"]), ("code", "code"))
        self.assertTrue(effective["lean_warm_up"]["skipped"])
        self.assertIsNone(effective["lean_image"])
        self.assertEqual(effective["judge_image"], config["code_judge"]["image"])
        self.assertEqual(effective["persona_counts"], config["persona_counts"])
        self.assertEqual(effective["seeded"]["path"], f"solutions/{config['seeded']['task_id']}.py")
        self.assertIn("check_solution", effective["overseer"]["oversight_tools"])
        self.assertEqual(effective["task_set"], "ilcb100")
        self.assertEqual(coding.domain_of_run(config["run_dir"]), "code")
        personas = settings["personas"]
        self.assertEqual(set(personas.values()), {"algorithms", "strings", "dynamic_programming", "graphs_math"})
        solver_prompt = captured["prompt_for"]("agent-001", personas["agent-001"])
        overseer_prompt = captured["prompt_for"]("agent-022", None)
        for text in (solver_prompt, overseer_prompt):
            self.assertNotIn("Lean", text)
            self.assertIn("lcbhard_1", text)
        self.assertIn("check_solution", [d["name"] for d in captured["definitions_for"]("agent-022")])
        self.assertNotIn("check_solution", [d["name"] for d in captured["definitions_for"]("agent-001")])
        self.assertIn("/workspace/run_tests.py", captured["task"]["files"])
        self.assertEqual(captured["task"]["initial_message"], runtime.agent_initial_message(config,
                                                                                            config["prompt_variant"]))


if __name__ == "__main__":
    unittest.main()
