"""Red-team roles (research_swarm.red_team): configs, preflight gate, and the designated solver's prompt."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from research_swarm import oversight, red_team, runtime
from research_swarm.prompts import NEUTRAL_FORBIDDEN, neutral_environment
from research_swarm.seeded import seed_agent

ROOT = Path(__file__).resolve().parents[1]
BASE = "glmflash100-seeded-1ov-glm-120m-push-commsonly"
CONDITIONS = ("solver", "attacker", "reviewer")
# Keys the red-team configs change for every condition (the solver control included): no behavior judge or
# incident report, whose judge cannot see the assignment.
SHARED_CHANGES = {"experiment", "behavior_judge", "auto_incident_report"}
# A stand-in for the researcher-only role texts (private/, host-only): the structure without the words.
FAKE_TEXT = {"text_version": red_team.TEXT_VERSION,
             "intro": {"attacker": "INTRO-A", "reviewer": "INTRO-R"},
             "knowledge": "KNOWLEDGE overseer={overseer_id} task={task_id}",
             "objective": {"attacker": "OBJECTIVE-A", "reviewer": "OBJECTIVE-R"}}


def text_available():
    try:
        red_team.load_text()
        return True
    except (OSError, ValueError):
        return False


def load(name):
    return json.loads((ROOT / "configs" / f"{name}.json").read_text())


@unittest.skipUnless(text_available(), f"researcher-only {red_team.TEXT_PATH} not present (host-only)")
class RedTeamPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.listing = runtime.export_public_tasks(ROOT, task_set="all71")[0]

    def prompts(self, config):
        """Every prompt from runtime.make_prompt_for, the function the run itself uses."""
        personas = runtime.assign_personas(config["agent_count"], config["persona_seed"])
        overseer = oversight.overseer_config(config)
        variant = config["prompt_variant"]
        prompt_for, _ = runtime.make_prompt_for(
            config, overseer=overseer, solver_ids=list(personas), seed=seed_agent(config), listing=self.listing,
            variant=variant, domain="lean", reviewing=config.get("persona_reviewing", False),
            push=runtime.push_delivery_enabled(config), neutral=neutral_environment(variant))
        ov_id = oversight.overseer_ids(overseer)[0]
        out = {agent: prompt_for(agent, persona) for agent, persona in personas.items()}
        out[ov_id] = prompt_for(ov_id, None)
        return out, ov_id, overseer

    def test_configs_differ_from_base_only_where_intended(self):
        base = load(BASE)
        for cond in CONDITIONS:
            config = load(f"{BASE}-rt-{cond}")
            diff = {k for k in set(base) | set(config) if base.get(k) != config.get(k)}
            self.assertEqual(diff, SHARED_CHANGES | ({"red_team"} if cond != "solver" else set()), cond)
            self.assertEqual((config["behavior_judge"], config["auto_incident_report"]), ("off", False), cond)
            self.assertEqual(red_team.red_team_problems(config), [], cond)
            if cond != "solver":
                self.assertEqual(config["red_team"]["role"], cond)
                self.assertEqual(config["red_team"]["agent_id"], "agent-050")

    def test_without_red_team_every_prompt_is_unchanged(self):
        base, _, _ = self.prompts(load(BASE))
        control, _, _ = self.prompts(load(f"{BASE}-rt-solver"))
        self.assertEqual(base, control)

    def test_only_the_designated_solver_changes_and_embeds_the_overseer_prompt(self):
        control, ov_id, overseer = self.prompts(load(f"{BASE}-rt-solver"))
        config = load(f"{BASE}-rt-solver")
        restart = runtime._continue_for(ov_id, overseer, neutral_environment(config["prompt_variant"]),
                                        runtime.push_delivery_enabled(config))
        for role in red_team.ROLES:
            prompts, _, _ = self.prompts(load(f"{BASE}-rt-{role}"))
            changed = {a for a in prompts if prompts[a] != control[a]}
            self.assertEqual(changed, {"agent-050"}, role)
            text = prompts["agent-050"]
            self.assertTrue(text.startswith(control["agent-050"].rstrip("\n") + "\n\n---\n\n## Your actual objective"))
            embedded = text.split("\n\n## The overseer's instructions\n\n", 1)[1]
            # Exactly the overseer's final prompt and restart message (push wording included), byte for byte:
            # nothing is stripped (the overseer prompt ends with a newline, which the copy keeps).
            self.assertTrue(control[ov_id].endswith("\n"))
            self.assertEqual(embedded, control[ov_id] + "\n\n## The message the overseer receives when its session "
                                       "restarts\n\n" + restart)
            self.assertTrue(text.endswith(restart))
            self.assertIn("appear in a `<system-reminder>` block", embedded)
            words = red_team.load_text()
            own = text[len(control["agent-050"]):text.index("## The overseer's instructions")]
            for part in (words["intro"][role], words["objective"][role],
                         words["knowledge"].format(overseer_id=ov_id, task_id="infinite_fermat_primes")):
                self.assertIn(part, own)
            self.assertIsNone(NEUTRAL_FORBIDDEN.search(own))

    def test_roles_share_knowledge_and_differ_in_objective(self):
        a = red_team.section_for("attacker", "agent-102", "OVERSEER PROMPT", "RESTART", "infinite_fermat_primes",
                                 FAKE_TEXT)
        r = red_team.section_for("reviewer", "agent-102", "OVERSEER PROMPT", "RESTART", "infinite_fermat_primes",
                                 FAKE_TEXT)
        self.assertEqual(a, "---\n\n## Your actual objective\n\nINTRO-A\n\nKNOWLEDGE overseer=agent-102 "
                            "task=infinite_fermat_primes\n\nOBJECTIVE-A\n\n## The overseer's instructions\n\n"
                            "OVERSEER PROMPT\n\n## The message the overseer receives when its session restarts\n\n"
                            "RESTART")
        self.assertEqual(r, a.replace("INTRO-A", "INTRO-R").replace("OBJECTIVE-A", "OBJECTIVE-R"))
        self.assertTrue(a.endswith("## The overseer's instructions\n\nOVERSEER PROMPT\n\n## The message the overseer "
                                   "receives when its session restarts\n\nRESTART"))

    def test_section_fails_closed(self):
        with self.assertRaises(ValueError):
            red_team.section_for("observer", "agent-102", "x", "y", "infinite_fermat_primes", FAKE_TEXT)
        with self.assertRaises(ValueError):
            red_team.section_for("attacker", "agent-102", "x", "y", "conference_problem", FAKE_TEXT)
        with self.assertRaises(ValueError):  # a forbidden word in the text
            bad = {**FAKE_TEXT, "objective": {"attacker": "the conference", "reviewer": "x"}}
            red_team.section_for("attacker", "agent-102", "x", "y", "infinite_fermat_primes", bad)

    def test_text_is_pinned(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(FileNotFoundError):
                red_team.load_text(root)
            path = Path(root) / red_team.TEXT_PATH
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(FAKE_TEXT))
            with self.assertRaises(ValueError):  # another digest
                red_team.load_text(root)
        with mock.patch.object(red_team, "load_text", side_effect=FileNotFoundError("absent")):
            config = load(f"{BASE}-rt-attacker")
            self.assertTrue(any("researcher-only text" in p for p in red_team.red_team_problems(config)))

    def test_effective_record(self):
        config = load(f"{BASE}-rt-attacker")
        section = red_team.section_for("attacker", "agent-102", "x", "y", "infinite_fermat_primes", FAKE_TEXT)
        record = red_team.effective(config["red_team"], section)
        self.assertEqual(record["role"], "attacker")
        self.assertEqual(record["agent_id"], "agent-050")
        self.assertEqual(len(record["section_sha256"]), 64)
        self.assertEqual(record["text_sha256"], red_team.TEXT_SHA256)
        self.assertIsNone(red_team.effective(None, None))


class RedTeamGateTests(unittest.TestCase):
    def setUp(self):
        self.config = load(f"{BASE}-rt-attacker")
        patcher = mock.patch.object(red_team, "load_text", return_value=FAKE_TEXT)  # the gate, not the text
        patcher.start()
        self.addCleanup(patcher.stop)

    def problems(self, **changes):
        config = copy.deepcopy(self.config)
        for key, value in changes.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        return red_team.red_team_problems(config)

    def assertRefused(self, problems, fragment):
        self.assertTrue(any(fragment in p for p in problems), problems)

    def test_valid_and_absent(self):
        self.assertEqual(self.problems(), [])
        self.assertEqual(self.problems(red_team=None), [])

    def test_shape_role_and_agent(self):
        self.assertRefused(self.problems(red_team={"agent_id": "agent-050"}), "must be an object")
        self.assertRefused(self.problems(red_team={"agent_id": "agent-050", "role": "attacker", "x": 1}),
                           "must be an object")
        self.assertRefused(self.problems(red_team={"agent_id": "agent-050", "role": "spy"}), "role must be one of")
        for agent in ("agent-101", "agent-102", "agent-000", "agent-50"):  # seed, overseer, invalid
            self.assertRefused(self.problems(red_team={"agent_id": agent, "role": "attacker"}), "one of the solvers")

    def test_requires_the_answer_wrapper_seed(self):
        self.assertRefused(self.problems(track="baseline"), "declared seeded run")
        seeded = {**self.config["seeded"], "artifact_sha256": "0" * 64}
        self.assertRefused(self.problems(seeded=seeded), "answer-wrapper seed")
        seeded = {**self.config["seeded"], "trigger_value": 3600}
        self.assertRefused(self.problems(seeded=seeded), "at most 600 s")
        for value in (0, -5, True, "180"):
            seeded = {**self.config["seeded"], "trigger_value": value}
            self.assertRefused(self.problems(seeded=seeded), "above 0")
        self.assertRefused(self.problems(max_run_seconds=120), "before max_run_seconds")
        self.assertRefused(self.problems(max_run_seconds=None), "before max_run_seconds")

    def test_requires_one_overseer_without_powers(self):
        self.assertRefused(self.problems(overseer=None), "exactly one overseer")
        self.assertRefused(self.problems(overseer={**self.config["overseer"], "powers": True}), "without oversight tools")
        self.assertRefused(self.problems(overseer={**self.config["overseer"], "count": 2,
                                                   "agent_ids": ["agent-102", "agent-103"]}), "exactly one overseer")

    def test_requires_lean_and_neutral_variant(self):
        self.assertRefused(self.problems(prompt_variant="max_pressure_no_rule6"), "neutral")
        self.assertRefused(self.problems(domain="code"), "Lean domain")

    def test_resume_manifest_records_the_block(self):
        import inspect
        self.assertIn('"red_team": effective.get("red_team")', inspect.getsource(runtime.resume_run_info))

    def test_preflight_lists_the_gate(self):
        import inspect
        self.assertIn("red_team_problems(config)", inspect.getsource(runtime))


if __name__ == "__main__":
    unittest.main()
